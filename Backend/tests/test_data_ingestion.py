"""
test_data_ingestion.py - Verification suite for the BlockFlow ingestion layer

Covers the four things the jury is expected to attack:

1.  **439-point waypoint database** - count, chainage closure on 59.980 km,
    29 stations, monotonic WGS84 snapping, per-running-line lateral offsets.
2.  **The frozen Pydantic v2 contract** - ``[asset_id, dept, km_start, km_end,
    aci]``, silo alias normalisation, and hard rejection of out-of-corridor /
    inverted / malformed chainage.
3.  **Statutory invariants** - ACTM Vol II Para 204 ``+15 / +15`` earthing buffer
    arithmetic and IRSEM Para 22 Form T/351 / T/352 generation.
4.  **Phase-4 stress behaviour** - the 50-requisition reference feed ingests
    cleanly (or is quarantined with a reason), and the malformed corpus never
    leaks a bad row into the HiGHS simplex solver.

Runs both ways::

    python Backend/tests/test_data_ingestion.py     # [PASS] report
    pytest Backend/tests/test_data_ingestion.py     # standard discovery
"""

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from Backend.data_ingestion import (  # noqa: E402
    ACTM_PARA_204_EARTHING_BUFFER_MINS,
    CONTRACT_FIELDS,
    BlockRequisition,
    coa_data,
    ingest_corridor,
    ingest_feed,
    load_malformed_cases,
    safety,
    schema,
    spatial,
    to_optimizer_payload,
    waypoints,
)
from Backend.data_ingestion.pipeline import load_raw_requisitions  # noqa: E402

#: Mumbai suburban bounding box (sanity guard on every emitted coordinate).
MUMBAI_BBOX = {"lon_min": 72.70, "lon_max": 72.95, "lat_min": 18.85, "lat_max": 19.55}


def _in_bbox(lon, lat) -> bool:
    return (
        MUMBAI_BBOX["lon_min"] <= lon <= MUMBAI_BBOX["lon_max"]
        and MUMBAI_BBOX["lat_min"] <= lat <= MUMBAI_BBOX["lat_max"]
    )


# --------------------------------------------------------------------------- #
#  1. Waypoint database + chainage snapping
# --------------------------------------------------------------------------- #
def test_waypoint_database():
    assert len(waypoints.WAYPOINTS) == 439, "the surveyed database is 439 waypoints"
    assert len(waypoints.STATION_GROUND_CONTROL) == 29
    assert waypoints.CORRIDOR_LENGTH_KM == 59.98

    first, last = waypoints.WAYPOINTS[0], waypoints.WAYPOINTS[-1]
    assert first["km"] == 0.0 and first["station_code"] == "CCG"
    assert last["km"] == 59.98 and last["station_code"] == "VR"
    assert abs(first["lon"] - 72.82721) < 1e-4 and abs(last["lon"] - 72.81223) < 1e-4

    # Chainage must advance monotonically and stay inside the corridor.
    previous = -1.0
    for waypoint in waypoints.WAYPOINTS:
        assert waypoint["km"] > previous or waypoint["km"] == 59.98
        previous = waypoint["km"]
        assert waypoints.validate_chainage(waypoint["km"])
        assert _in_bbox(waypoint["lon"], waypoint["lat"])

    # The corridor runs north: latitude must increase overall and never bounce.
    lats = [waypoint["lat"] for waypoint in waypoints.WAYPOINTS]
    assert lats[-1] > lats[0]
    assert all(b - a > -0.001 for a, b in zip(lats, lats[1:]))

    # Sectional chainage must tile the whole corridor with no gap or overlap.
    total = 0.0
    cursor = 0.0
    for section in waypoints.CORRIDOR_SECTIONS:
        assert abs(section["km_start"] - cursor) < 1e-6
        total += section["km_end"] - section["km_start"]
        cursor = section["km_end"]
    assert abs(total - waypoints.CORRIDOR_LENGTH_KM) < 1e-6
    assert abs(cursor - 59.98) < 1e-6


def test_chainage_snapping_and_offsets():
    low = waypoints.waypoint_at_km(12.00)
    high = waypoints.waypoint_at_km(30.00)
    assert high["lat"] > low["lat"], "chainage increases northbound"
    assert low["section"] == "DDR-BA"

    # Out-of-range chainage clamps instead of raising (used by the 3D twin).
    assert waypoints.validate_chainage(0.0) and waypoints.validate_chainage(59.98)
    assert not waypoints.validate_chainage(-0.01)
    assert not waypoints.validate_chainage(59.981)

    # The four running lines must not collapse onto one another (quad corridor).
    fast = waypoints.track_geometry(21.35, "DN_FAST")
    slow = waypoints.track_geometry(21.35, "UP_SLOW")
    separation_m = spatial.haversine_km((fast["lon"], fast["lat"]), (slow["lon"], slow["lat"])) * 1000.0
    assert fast["lateral_offset_m"] == 6.0 and slow["lateral_offset_m"] == -2.0
    assert 6.0 < separation_m < 10.0, f"expected an 8 m quad separation, got {separation_m:.2f} m"

    geojson = waypoints.waypoints_geojson()
    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) == 439
    assert geojson["properties"]["waypoint_count"] == 439


# --------------------------------------------------------------------------- #
#  2. Frozen contract
# --------------------------------------------------------------------------- #
def test_contract_fields_and_aliases():
    assert CONTRACT_FIELDS == ["asset_id", "dept", "km_start", "km_end", "aci"]

    requisition = BlockRequisition(
        asset_id="tms-eng-1006",
        DEPARTMENT="P.WAY",           # silo spelling -> CIVIL
        KM_FROM="19.40",              # string chainage
        KM_TO=21.20,
        TRACK="DN FAST",              # spaced running line
        DURATION_MINS="3:00",         # HH:MM encoding
        SEVERITY="P1",
        REQUIRES_POWER_BLOCK="Y",
    )
    assert requisition.asset_id == "TMS-ENG-1006"
    assert requisition.dept is schema.Department.CIVIL
    assert requisition.line is schema.Line.DN_FAST
    assert requisition.urgency is schema.Severity.CRITICAL
    assert requisition.duration_mins == 180
    assert requisition.requires_power_block is True
    assert requisition.requires_traffic_block is True  # forced on by power isolation
    assert requisition.section == "BA-AND"  # km 19.40 sits in Bandra - Andheri
    assert requisition.span_km == 1.8
    assert requisition.aci is None, "aci stays null until the ACI engine scores it"

    contract = requisition.to_contract_dict()
    for field in CONTRACT_FIELDS:
        assert field in contract

    requisition.with_spatial_fix()
    assert requisition.geo_start is not None and requisition.geo_end is not None
    assert _in_bbox(requisition.geo_start.lon, requisition.geo_start.lat)
    assert requisition.geo_start.nearest_waypoint_id.startswith("WP-")
    assert requisition.to_legacy_dict()["track_id"] == "DN_FAST"


def test_contract_rejects_unsafe_chainage():
    base = {
        "asset_id": "TMS-ENG-9999",
        "dept": "CIVIL",
        "km_start": 10.0,
        "km_end": 11.0,
        "line": "UP_FAST",
        "duration_mins": 60,
    }
    assert BlockRequisition(**base).span_km == 1.0

    hostile = [
        ({"km_end": 62.4}, "beyond the corridor"),
        ({"km_start": -2.5}, "negative chainage"),
        ({"km_start": 25.0, "km_end": 18.0}, "inverted chainage"),
        ({"km_start": 5.0, "km_end": 40.0}, "single possession span"),
        ({"dept": "MECHANICAL"}, "unknown directorate"),
        ({"line": "DN_UP"}, "unknown running line"),
        ({"duration_mins": 0}, "zero duration"),
        ({"duration_mins": "whenever"}, "unparseable duration"),
        ({"aci": 150}, "aci outside 0-100"),
        ({"asset_id": "'; DROP TABLE assets; --"}, "unsafe asset id"),
    ]
    for patch, label in hostile:
        payload = {**base, **patch}
        try:
            BlockRequisition(**payload)
        except Exception:
            continue
        raise AssertionError(f"contract accepted hostile payload: {label}")


# --------------------------------------------------------------------------- #
#  3. Statutory invariants
# --------------------------------------------------------------------------- #
def test_actm_para_204_earthing_buffer():
    assert ACTM_PARA_204_EARTHING_BUFFER_MINS == 15

    window = safety.compute_earthing_window("01:30", 180, power_isolation_required=True)
    assert window["earthing_buffer_mins"] == 15
    assert window["earthing_before"]["minutes"] == 15
    assert window["earthing_after"]["minutes"] == 15
    assert window["work_duration_mins"] == 180
    assert window["total_possession_mins"] == 210
    assert window["duration_with_buffer_expr"].startswith("180 + 15 + 15 = 210 min")

    from datetime import datetime

    power_off = datetime.fromisoformat(window["power_off_at"])
    work_start = datetime.fromisoformat(window["work_start"])
    work_end = datetime.fromisoformat(window["work_end"])
    restored = datetime.fromisoformat(window["power_restored_at"])
    assert (work_start - power_off).total_seconds() == 900
    assert (restored - work_end).total_seconds() == 900

    # No live OHE exposure -> no buffer, possession is exactly the work duration.
    dry = safety.compute_earthing_window("01:30", 90, power_isolation_required=False)
    assert dry["total_possession_mins"] == 90 and dry["earthing_applied"] is False

    verdict = safety.evaluate_safety_invariants({
        "asset_id": "TDMS-TRD-3001", "dept": "TRD", "line": "DN_FAST",
        "km_start": 19.0, "km_end": 22.0, "duration_mins": 60,
        "requires_power_block": True, "requires_traffic_block": True,
        "hotspot_temp_c": 84.5, "work_start": "02:00",
    })
    assert verdict["passed"], verdict["violations"]
    by_code = {check["code"]: check for check in verdict["checks"]}
    assert by_code["ACTM_204_EARTHING_BUFFER"]["status"] == "PASS"
    assert verdict["earthing_window"]["total_possession_mins"] == 90


def test_irsem_form_t351_disconnection():
    snT = {
        "asset_id": "SMMS-SNT-2011", "dept": "SNT", "line": "DN_SLOW",
        "km_start": 33.95, "km_end": 34.15, "duration_mins": 60,
        "requires_disconnection": True, "statutory_form": "T/351",
        "requires_traffic_block": True,
    }
    verdict = safety.evaluate_safety_invariants(snT)
    assert verdict["passed"], verdict["violations"]
    assert set(verdict["statutory_forms_required"]) >= {"T_351", "T_352"}

    forms = {memo["form_no"]: memo for memo in verdict["memos"]}
    assert "IRSEM Para 22" in forms["T_351"]["statutory_reference"]
    assert forms["T_351"]["km_range"] == "33.95 - 34.15"
    assert forms["T_352"]["valid_from"] == verdict["earthing_window"]["power_restored_at"]

    # A disconnection job with no Form T/351 must be blocked, not waved through.
    missing = {**snT, "statutory_form": "NONE"}
    blocked = safety.evaluate_safety_invariants(missing)
    assert not blocked["passed"]
    assert "IRSEM_22_T351_DISCONNECTION" in blocked["violations"]

    # A civil tamping job is not an IRSEM disconnection job.
    civil = safety.evaluate_safety_invariants({
        "asset_id": "TMS-ENG-1006", "dept": "CIVIL", "line": "DN_FAST",
        "km_start": 19.4, "km_end": 21.2, "duration_mins": 180,
        "requires_power_block": True, "requires_traffic_block": True,
    })
    assert civil["passed"]
    assert "T_351" not in civil["statutory_forms_required"]
    assert "ACTM_PTW" in civil["statutory_forms_required"]


def test_plan_guardrail_detects_conflicts():
    plan = [
        {
            "block_id": "IR-BLK-TEST-1", "date": "2026-09-20", "start_time": "01:30",
            "section": "DDR-BA", "track_id": "DN_FAST", "allocated_duration_mins": 180,
            "slot_window_mins": 195,
            "tasks_bundled": [{"asset_id": "TMS-ENG-1006", "duration_mins": 180, "requires_power_block": True}],
        },
        {
            "block_id": "IR-BLK-TEST-2", "date": "2026-09-20", "start_time": "02:00",
            "section": "DDR-BA", "track_id": "DN_FAST", "allocated_duration_mins": 120,
            "slot_window_mins": 120,
            "tasks_bundled": [{"asset_id": "SMMS-SNT-2001", "duration_mins": 120, "requires_power_block": True}],
        },
    ]
    result = safety.validate_plan(plan)
    assert not result["passed"]
    codes = {violation.split(":")[0] for violation in result["violations"]}
    assert "PHYSICAL_EXCLUSIVITY" in codes
    assert "POWER_ISOLATION_ENVELOPE" in codes or "DURATION_CAPACITY" in codes


# --------------------------------------------------------------------------- #
#  4. Phase-4 stress behaviour
# --------------------------------------------------------------------------- #
def test_reference_feed_ingests_cleanly():
    report = ingest_corridor()
    summary = report.summary()

    assert summary["received"] == 50, "50 real-world Western Railway requisitions"
    assert summary["accepted"] == 50, f"rejections: {[r.errors for r in report.rejected]}"
    assert summary["by_department"] == {"CIVIL": 20, "SNT": 15, "TRD": 15}

    # Exactly one record (TDMS-TRD-3005: power isolation with no traffic block)
    # is a deliberate data-entry error the guardrail must catch.
    assert summary["safety_failed"] == 1
    blocked = [v["asset_id"] for v in report.safety if not v["passed"]]
    assert blocked == ["TDMS-TRD-3005"]

    rows = to_optimizer_payload(report)
    assert len(rows) == 49, "the safety-blocked row never reaches the optimizer"
    for row in rows:
        for key in ("defect_id", "department", "track_id", "km_start", "km_end", "duration_mins"):
            assert key in row, f"optimizer row missing {key}"


def test_malformed_corpus_never_leaks():
    cases = load_malformed_cases()
    assert len(cases) >= 20

    report = ingest_feed({
        "feed_id": "STRESS-MALFORMED-001",
        "requisitions": [case["payload"] for case in cases],
    })
    verdicts = {verdict["asset_id"]: verdict for verdict in report.safety}

    assert report.rejected_count == len(report.rejected) >= 15
    assert len(report.accepted) == len(cases) - len(report.rejected)

    # Every rejection must carry a human-readable reason and map to a REJECT case.
    assert all(rejection.errors for rejection in report.rejected)
    for rejection in report.rejected:
        assert cases[rejection.index]["expect"] == "REJECT", (
            f"{cases[rejection.index]['case_id']} was rejected but declares "
            f"{cases[rejection.index]['expect']}"
        )

    # Whatever survived must be a fully snapped, in-corridor contract object.
    for requisition in report.accepted:
        assert isinstance(requisition, BlockRequisition)
        assert waypoints.validate_chainage(requisition.km_start)
        assert waypoints.validate_chainage(requisition.km_end)
        assert requisition.km_start <= requisition.km_end
        assert requisition.span_km <= 19.98
        assert requisition.geo_start is not None and requisition.geo_end is not None
        assert _in_bbox(requisition.geo_start.lon, requisition.geo_start.lat)
        assert requisition.asset_id in verdicts, "every accepted record gets a safety verdict"


def test_malformed_corpus_expectations():
    """Index-aligned assertions: each case must produce exactly its declared outcome."""
    cases = load_malformed_cases()
    report = ingest_feed({"requisitions": [case["payload"] for case in cases]})
    rejected_by_index = {rejection.index: rejection for rejection in report.rejected}
    accepted_in_order = report.accepted
    verdicts = {verdict["asset_id"]: verdict for verdict in report.safety}

    accepted_cursor = 0
    for index, case in enumerate(cases):
        expected = case["expect"]
        if expected == "REJECT":
            assert index in rejected_by_index, f"{case['case_id']} should be REJECT"
            assert rejected_by_index[index].errors
            continue
        assert index not in rejected_by_index, f"{case['case_id']} should be accepted"
        requisition = accepted_in_order[accepted_cursor]
        accepted_cursor += 1
        verdict = verdicts[requisition.asset_id]
        assert verdict["passed"] == (expected == "ACCEPT"), (
            f"{case['case_id']} expected {expected}, violations={verdict['violations']}"
        )

    assert accepted_cursor == report.accepted_count


def test_geojson_output_for_gati_shakti():
    report = ingest_feed([load_raw_requisitions()[5]], source="TMS")
    geojson = report.geojson
    assert geojson["type"] == "FeatureCollection"

    linestrings = [f for f in geojson["features"] if f["geometry"]["type"] == "LineString"]
    points = [f for f in geojson["features"] if f["geometry"]["type"] == "Point"]
    assert len(linestrings) == 1 and len(points) == 2

    feature = linestrings[0]
    props = feature["properties"]
    assert props["asset_id"] == "TMS-ENG-1006"
    assert props["dept"] == "CIVIL" and props["line"] == "DN_FAST"
    assert props["actm_para_204_buffer_mins"] == 15
    assert props["total_possession_mins"] == 210
    assert props["start_waypoint_id"].startswith("WP-")

    coords = feature["geometry"]["coordinates"]
    assert len(coords) >= 5
    for lon, lat in coords:
        assert _in_bbox(lon, lat), f"coordinate {lon},{lat} left the Mumbai bbox"

    # The first emitted vertex must equal the snapped start fix (on its own line).
    fix = report.accepted[0].geo_start
    assert abs(coords[0][0] - fix.lon) < 1e-5 and abs(coords[0][1] - fix.lat) < 1e-5

    # JSON round-trip: the artefact handed to PM Gati Shakti must be serialisable.
    json.dumps(geojson)


def test_coa_timetable_ingestion():
    report = coa_data.parse_coa_timetable({
        "feed_id": "COA-SAMPLE",
        "slots": coa_data.CORRIDOR_SLOTS,
        "train_paths": coa_data.SAMPLE_TRAIN_PATHS,
    })
    assert report["rejected_count"] == 0, report["rejected"]
    assert len(report["slots"]) == len(coa_data.CORRIDOR_SLOTS)
    assert len(report["train_paths"]) == len(coa_data.SAMPLE_TRAIN_PATHS)

    night = report["slots"][0]
    assert night["duration_mins"] == night["window_mins_derived"] == 195
    assert night["slot_type"] == "NIGHT_MAJOR"
    assert "SUN" in night["allowed_days"]

    # A window that claims more minutes than it holds must be clamped, not trusted.
    clamped = coa_data.normalize_corridor_slot({
        "slot_id": "slot-lying", "from_time": "01:00", "to_time": "02:00", "duration_mins": 300,
    })
    assert clamped["duration_mins"] == 60

    # A malformed window is quarantined with a reason.
    bad = coa_data.parse_coa_timetable({"slots": [{"slot_id": "slot-broken", "start_time": "01:00"}]})
    assert bad["rejected_count"] == 1 and not bad["slots"]


def test_chainage_snap_is_deterministic():
    """Same chainage -> same coordinate, on every run (zero-hallucination claim)."""
    assert waypoints.waypoint_at_km(21.35) == waypoints.waypoint_at_km(21.35)

    # km 0.000 must resolve exactly onto the surveyed Churchgate control point.
    zero = waypoints.waypoint_at_km(0.0)
    churchgate = waypoints.CENTERLINE[0]
    assert abs(waypoints.STATION_CHAINAGE["CCG"]) < 1e-12
    assert abs(zero["lon"] - churchgate[0]) < 1e-9
    assert abs(zero["lat"] - churchgate[1]) < 1e-9
    assert waypoints.WAYPOINTS[0]["lon"] == round(churchgate[0], 6)
    assert waypoints.WAYPOINTS[0]["lat"] == round(churchgate[1], 6)

    # A snapped fix must reproduce both the chainage and the exact geometry it
    # was derived from (to the 6-decimal publish precision of the database).
    for km in (3.42, 10.232, 21.35, 34.156, 55.75):
        fixed = waypoints.waypoint_at_km(km)
        exact = spatial.point_at_chainage(waypoints.CENTERLINE, waypoints.CENTERLINE_CHAINAGE, km)
        assert abs(fixed["km"] - km) < 1e-6
        assert fixed["lon"] == round(exact[0], 6)
        assert fixed["lat"] == round(exact[1], 6)


def test_highs_solver_handoff():
    """Feed the safety-cleared requisitions straight into the HiGHS MILP engine."""
    try:
        from Backend.ai_engine.block_optimizer import IntegratedBlockOptimizer
    except ImportError as exc:  # scipy not installed in a bare environment
        print(f"  [SKIP] HiGHS hand-off ({exc})")
        return

    optimizer = IntegratedBlockOptimizer()
    report = ingest_corridor()
    rows = to_optimizer_payload(report)

    # Every row must carry the three-duration envelope so the ACTM Para 204
    # footprint can never be misread downstream.
    for row in rows:
        assert row["possession_duration_mins"] == row["work_duration_mins"] + 2 * row["earthing_buffer_mins"]
        assert row["possession_duration_mins"] >= row["duration_mins"]

    # The solver must consume the feed without a runtime error.
    result = optimizer.optimize_blocks(rows, horizon_days=7)
    assert "scheduled_blocks" in result and "metrics" in result
    assert result["metrics"]["total_demands"] == len(rows)
    assert result["metrics"]["tasks_scheduled"] >= 1

    # Independent guardrail. The current optimizer budgets only the working
    # duration, so planning against `duration_mins` short-changes the 15-minute
    # earthing wrap on long possessions - the guardrail must say so out loud
    # rather than silently blessing the plan.
    guardrail = safety.validate_plan(result["scheduled_blocks"])
    assert guardrail["blocks_checked"] == len(result["scheduled_blocks"])
    assert guardrail["checks_run"] > 0
    assert not guardrail["passed"], (
        "expected the guardrail to report ACTM 204 under-budgeting; it passed the plan"
    )
    assert any(
        violation.startswith("DURATION_CAPACITY") for violation in guardrail["violations"]
    ), guardrail["violations"]

    # And the same feed planned against the real possession footprint validates
    # clean - i.e. the ingestion layer *can* hand the solver a compliant problem.
    possession_rows = [dict(row, duration_mins=row["possession_duration_mins"]) for row in rows]
    compliant = optimizer.optimize_blocks(possession_rows, horizon_days=7)
    compliant_guardrail = safety.validate_plan(compliant["scheduled_blocks"])
    assert compliant_guardrail["passed"], compliant_guardrail["violations"]
    assert len(compliant["scheduled_blocks"]) >= 1


def main():  # pragma: no cover - CLI report runner
    tests = [
        ("Waypoint database: 439 WGS84 points / 29 stations / 59.98 km closure", test_waypoint_database),
        ("Chainage snapping + quad-track lateral offsets", test_chainage_snapping_and_offsets),
        ("Frozen contract + silo alias normalisation", test_contract_fields_and_aliases),
        ("Contract rejects out-of-corridor / malformed chainage", test_contract_rejects_unsafe_chainage),
        ("ACTM Para 204: 15-minute earthing buffer arithmetic", test_actm_para_204_earthing_buffer),
        ("IRSEM Para 22: Form T/351 + T/352 generation", test_irsem_form_t351_disconnection),
        ("Plan guardrail: exclusivity + power isolation envelope", test_plan_guardrail_detects_conflicts),
        ("Ingestion layer: 50 CRIS requisitions validated", test_reference_feed_ingests_cleanly),
        ("Malformed payloads quarantined, never leaked", test_malformed_corpus_never_leaks),
        ("Malformed corpus outcomes match declared expectations", test_malformed_corpus_expectations),
        ("GeoJSON export for PM Gati Shakti / CesiumJS twin", test_geojson_output_for_gati_shakti),
        ("COA timetable windows normalised and clamped", test_coa_timetable_ingestion),
        ("Chainage snap is deterministic (zero hallucination)", test_chainage_snap_is_deterministic),
        ("HiGHS simplex hand-off on safety-cleared feed", test_highs_solver_handoff),
    ]
    failures = 0
    for label, test in tests:
        try:
            test()
            print(f"[PASS] {label}")
        except Exception as exc:  # noqa: BLE001 - CLI harness
            failures += 1
            print(f"[FAIL] {label}\n       {type(exc).__name__}: {exc}")

    report = ingest_corridor()
    summary = report.summary()
    print("-" * 68)
    print(
        f"Feed: {summary['received']} requisitions | accepted {summary['accepted']} "
        f"({summary['acceptance_pct']}%) | rejected {summary['rejected']}"
    )
    print(f"By department: {summary['by_department']}")
    print(
        f"Safety: {summary['safety_passed']} pass / {summary['safety_failed']} blocked "
        f"| ACTM Para 204 buffer {summary and ACTM_PARA_204_EARTHING_BUFFER_MINS} min"
    )
    print(f"Optimizer-ready rows: {len(to_optimizer_payload(report))}")
    print("-" * 68)
    if failures:
        print(f"{failures} TEST(S) FAILED")
        return 1
    print("ALL DATA-INGESTION TESTS PASSED - CONTRACT, WAYPOINTS & STATUTORY CHECKS VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
