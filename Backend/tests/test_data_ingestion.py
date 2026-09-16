"""
test_data_ingestion.py - Verification suite for the BlockFlow ingestion layer

Covers the five things the jury is expected to attack:

1.  **1D Linear Referencing System** - the ``has_overlap`` interval algebra that
    is the whole of the spatial solver constraint, including the half-open
    adjacency rule and the corridor/running-line scoping.
2.  **439-point projection table** - count, chainage closure on 59.980 km, 29
    stations, monotonic projection, per-running-line lateral offsets, and the
    ``[lat, lon]`` read-only display payload.
3.  **The frozen Pydantic v2 contract** - ``[asset_id, dept, km_start, km_end,
    aci]`` on an LRS span, silo alias normalisation, and hard rejection of
    out-of-corridor / inverted / malformed chainage.
4.  **Statutory invariants** - ACTM Vol II Para 204 ``+15 / +15`` earthing buffer
    arithmetic, IRSEM Para 22 Form T/351 / T/352 generation, and 1D chainage
    exclusivity of the scheduled plan.
5.  **Phase-4 stress behaviour** - the 50-requisition reference feed ingests
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
    LRS_FIELDS,
    BlockRequisition,
    LinearSpan,
    coa_data,
    gap_km,
    has_overlap,
    ingest_corridor,
    ingest_feed,
    load_malformed_cases,
    lrs,
    merge_spans,
    overlap_length_km,
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
#  0. The 1D Linear Referencing System (LRS)
# --------------------------------------------------------------------------- #
def _span(km_start, km_end, line_id="DN_FAST", corridor_id=None):
    return LinearSpan(
        corridor_id=corridor_id or lrs.CORRIDOR_ID,
        line_id=line_id,
        km_start=km_start,
        km_end=km_end,
    )


def test_lrs_interval_algebra():
    """The entire spatial solver constraint, expressed in one dimension."""
    a = _span(19.0, 21.0)

    # ---- overlap forms ---------------------------------------------------- #
    assert has_overlap(a, _span(20.0, 22.0)), "partial overlap"
    assert has_overlap(a, _span(19.5, 20.5)), "containment"
    assert has_overlap(a, _span(18.0, 25.0)), "superset"
    assert has_overlap(a, _span(19.0, 21.0)), "identical"
    assert overlap_length_km(a, _span(20.0, 23.0)) == 1.0

    # ---- half-open: touching end-to-end is not a collision ----------------- #
    assert not has_overlap(a, _span(21.0, 23.0))
    assert not has_overlap(_span(17.0, 19.0), a)
    assert gap_km(a, _span(21.0, 23.0)) == 0.0, "but they are exactly adjacent"

    # ---- disjoint -------------------------------------------------------- #
    assert not has_overlap(a, _span(30.0, 31.0))
    assert gap_km(a, _span(30.0, 31.0)) == 9.0
    assert overlap_length_km(a, _span(30.0, 31.0)) == 0.0

    # ---- scoping: a different running line is a different space ----------- #
    assert not has_overlap(a, _span(20.0, 22.0, line_id="UP_SLOW"))
    assert gap_km(a, _span(20.0, 22.0, line_id="UP_SLOW")) == float("inf")

    # ---- scoping: so is a different corridor ----------------------------- #
    assert not has_overlap(a, _span(20.0, 22.0, corridor_id="WR-OTHER-CORRIDOR"))

    # ---- the relation is intransitive, so O(n^2) is the honest scan -------- #
    collisions = lrs.find_collisions([_span(0.0, 2.0), _span(1.0, 3.0), _span(2.5, 4.0)])
    assert [(i, j) for i, j, _ in collisions] == [(0, 1), (1, 2)]
    assert not has_overlap(_span(0.0, 2.0), _span(2.5, 4.0))

    # ---- coalescing into minimal linear extents --------------------------- #
    merged = merge_spans([
        _span(1.0, 2.0), _span(1.8, 2.4), _span(2.4, 3.0),  # -> one extent
        _span(10.0, 11.0),                                  # -> separate
        _span(5.0, 6.0, line_id="UP_FAST"),                 # -> other line
    ])
    assert (lrs.CORRIDOR_ID, "DN_FAST", 1.0, 3.0) in merged
    assert (lrs.CORRIDOR_ID, "DN_FAST", 10.0, 11.0) in merged
    assert (lrs.CORRIDOR_ID, "UP_FAST", 5.0, 6.0) in merged
    assert len(merged) == 3

    # ---- unresolvable lines are conservative, never silently disjoint ----- #
    unknown = has_overlap(
        {"km_start": 1.0, "km_end": 2.0}, {"km_start": 1.5, "km_end": 2.5}
    )
    assert unknown, "two records with no line id must still be treated as colliding"

    # ---- legacy optimizer rows work too (track_id spelling) --------------- #
    assert has_overlap(
        {"track_id": "DN_FAST", "km_start": 1.0, "km_end": 2.0},
        {"track_id": "DN_FAST", "km_start": 1.5, "km_end": 2.5},
    )
    assert not has_overlap(
        {"track_id": "DN_FAST", "km_start": 1.0, "km_end": 2.0},
        {"track_id": "DN_SLOW", "km_start": 1.5, "km_end": 2.5},
    )


def test_lrs_replaces_section_proximity():
    """
    A section code is a reporting bookmark, not a geometry.

    Before the LRS refactor, conflict detection was keyed on
    ``(section, track_id)``, so two jobs in the same 7 km section were treated as
    colliding no matter how far apart they were. In 1D they simply are not -
    and two jobs genuinely on top of each other still are.
    """
    def requisition(asset_id, km_start, km_end):
        return BlockRequisition(
            asset_id=asset_id, dept="CIVIL", line_id="DN_FAST",
            km_start=km_start, km_end=km_end, duration_mins=60,
        )

    near = requisition("TMS-ENG-0001", 19.40, 19.60)
    far = requisition("TMS-ENG-0002", 21.00, 21.20)
    on_top = requisition("TMS-ENG-0003", 19.50, 19.90)

    # Same section, 1.4 km of clear track between them.
    assert near.section == far.section == "BA-AND"
    assert not has_overlap(near, far)
    assert gap_km(near, far) == 1.4

    # Same section, genuinely overlapping chainage.
    assert has_overlap(near, on_top)
    assert overlap_length_km(near, on_top) == 0.1

    # Different running lines at the same chainage never collide.
    opposite = BlockRequisition(
        asset_id="TMS-ENG-0004", dept="CIVIL", line_id="UP_SLOW",
        km_start=19.40, km_end=19.60, duration_mins=60,
    )
    assert not has_overlap(near, opposite)


def test_lrs_span_contract():
    """``LinearSpan`` is the primary spatial key, and it is validated hard."""
    span = _span(19.4, 21.2)
    assert span.corridor_id == "WR-MUMBAI-CCG-VR"
    assert span.line_id == "DN_FAST"
    assert span.span_km == 1.8
    assert span.lrs_key == ("WR-MUMBAI-CCG-VR", "DN_FAST")
    assert span.interval == (19.4, 21.2)
    assert set(LRS_FIELDS) <= set(span.to_lrs_dict())

    # Corridor/line spellings fold onto the canonical ids.
    folded = LinearSpan(corridor="Churchgate - Virar", track="DN FAST",
                        km_from="20400 M", km_to=21.2)
    assert folded.corridor_id == lrs.CORRIDOR_ID
    assert folded.line_id == "DN_FAST"
    assert folded.km_start == 20.4, "bare metre figures are a real CRIS quirk"

    # The 1D invariants still reject unsafe geometry.
    for patch, label in (
        ({"km_start": 25.0, "km_end": 18.0}, "inverted chainage"),
        ({"km_end": 62.4}, "outside the corridor"),
        ({"km_start": -2.5}, "negative chainage"),
        ({"line_id": "DN_UP"}, "unknown running line"),
    ):
        base = {"line_id": "DN_FAST", "km_start": 10.0, "km_end": 11.0} | patch
        try:
            LinearSpan(**base)
        except Exception:
            continue
        raise AssertionError(f"LinearSpan accepted {label}")


# --------------------------------------------------------------------------- #
#  1. Waypoint projection table
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

    # The projection table exists only to emit `[lat, lon]` display payloads.
    projected = waypoints.project_km(21.35, "DN_FAST")
    assert projected["coordinates"] == [projected["lat"], projected["lon"]]
    assert projected["projection_only"] is True
    assert projected["corridor_id"] == lrs.CORRIDOR_ID
    assert projected["line_id"] == "DN_FAST"
    assert _in_bbox(projected["lon"], projected["lat"])
    assert projected["coordinates"] == waypoints.project_km(21.35, "DN_FAST")["coordinates"]
    # A centreline projection (no line given) sits between the two outer lines.
    centreline = waypoints.project_km(21.35)["coordinates"]
    assert fast["lateral_offset_m"] == 6.0 and centreline != projected["coordinates"]


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
    assert requisition.urgency is schema.Severity.CRITICAL
    assert requisition.duration_mins == 180
    assert requisition.requires_power_block is True
    assert requisition.requires_traffic_block is True  # forced on by power isolation
    assert requisition.section == "BA-AND"  # km 19.40 sits in Bandra - Andheri
    assert requisition.span_km == 1.8
    assert requisition.aci is None, "aci stays null until the ACI engine scores it"

    # ---- the LRS primary key --------------------------------------------- #
    assert requisition.corridor_id == lrs.CORRIDOR_ID
    assert requisition.line_id == "DN_FAST", "line_id is the canonical LRS field"
    assert requisition.line is schema.Line.DN_FAST, "Line enum stays as a convenience"
    assert requisition.lrs_key == (lrs.CORRIDOR_ID, "DN_FAST")
    assert isinstance(requisition, LinearSpan)

    contract = requisition.to_contract_dict()
    for field in CONTRACT_FIELDS:
        assert field in contract
    for field in LRS_FIELDS:
        assert field in contract, f"contract lost the LRS field {field}"

    legacy = requisition.to_legacy_dict()
    assert legacy["track_id"] == "DN_FAST" and legacy["line_id"] == "DN_FAST"
    assert legacy["corridor_id"] == lrs.CORRIDOR_ID

    # ---- the projection is display-only, and explicitly ordered ---------- #
    assert requisition.display_start is None
    requisition.project_display()
    start, end = requisition.display_start, requisition.display_end
    assert start is not None and end is not None
    assert _in_bbox(start.lon, start.lat)
    assert start.nearest_waypoint_id.startswith("WP-")
    assert start.coordinates == [start.lat, start.lon], "display order is [lat, lon]"
    assert start.geojson_coordinates == [start.lon, start.lat], "GeoJSON is [lon, lat]"
    assert start.read_only is True
    assert requisition.geo_start is start, "geo_start is a read-only alias"
    assert requisition.to_display_payload()["projection_only"] is True

    # The projection must never leak into the solver payload.
    assert "display_start" not in legacy and "lat" not in legacy


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
    """The plan guardrail is a 1D chainage-collision test, not a section test."""
    def block(block_id, start_time, km_start, km_end, line_id="DN_FAST", **extra):
        return {
            "block_id": block_id, "date": "2026-09-20", "start_time": start_time,
            "corridor_id": lrs.CORRIDOR_ID, "line_id": line_id,
            "km_start": km_start, "km_end": km_end, "span_km": round(km_end - km_start, 3),
            "section": "DDR-BA", "track_id": line_id,
            "allocated_duration_mins": 180, "slot_window_mins": 240,
            "tasks_bundled": [{
                "asset_id": block_id, "duration_mins": 180,
                "requires_power_block": True,
            }],
            **extra,
        }

    # Colliding kilometre intervals in an overlapping time window -> violation.
    colliding = safety.validate_plan([
        block("IR-BLK-T1", "01:30", 19.0, 21.0),
        block("IR-BLK-T2", "02:00", 20.0, 22.0),
    ])
    assert not colliding["passed"]
    codes = {violation.split(":")[0] for violation in colliding["violations"]}
    assert "CHAINAGE_INTERVAL_EXCLUSIVITY" in codes
    exclusive = [c for c in colliding["checks"] if c["code"] == "CHAINAGE_INTERVAL_EXCLUSIVITY"]
    assert exclusive and exclusive[0]["status"] == "FAIL"
    assert "1.000 km" in exclusive[0]["detail"]

    # Same section and same line, but 4 km of clear track apart -> no conflict,
    # even in an overlapping time window. The pre-LRS section-keyed check
    # wrongly forbade this.
    separated = safety.validate_plan([
        block("IR-BLK-T3", "01:30", 19.0, 20.0),
        block("IR-BLK-T4", "02:00", 24.0, 25.0),
    ])
    assert separated["passed"], separated["violations"]
    assert not any(v.startswith("CHAINAGE_INTERVAL_EXCLUSIVITY") for v in separated["violations"])

    # Overlapping chainage on a different running line -> no conflict either:
    # the quad corridor has four independent spaces.
    other_line = safety.validate_plan([
        block("IR-BLK-T5", "01:30", 19.0, 21.0),
        block("IR-BLK-T6", "02:00", 19.0, 21.0, line_id="UP_SLOW"),
    ])
    assert other_line["passed"], other_line["violations"]

    # A block carrying no chainage at all cannot be assessed in 1D and must be
    # reported, not silently blessed.
    unresolved = safety.validate_plan([
        {"block_id": "IR-BLK-T7", "date": "2026-09-20", "start_time": "01:30",
         "section": "DDR-BA", "track_id": "DN_FAST", "allocated_duration_mins": 60},
        {"block_id": "IR-BLK-T8", "date": "2026-09-20", "start_time": "01:30",
         "section": "DDR-BA", "track_id": "DN_FAST", "allocated_duration_mins": 60},
    ])
    assert not unresolved["passed"]
    assert any(v.startswith("LRS_EXTENT_RESOLVED") for v in unresolved["violations"])

    # The duration/envelope arithmetic still behaves as before.
    under_budget = safety.validate_plan([block(
        "IR-BLK-T9", "01:30", 19.0, 21.0, allocated_duration_mins=60, slot_window_mins=60,
    )])
    codes = {violation.split(":")[0] for violation in under_budget["violations"]}
    assert "DURATION_CAPACITY" in codes or "POWER_ISOLATION_ENVELOPE" in codes


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
        assert requisition.display_start is not None and requisition.display_end is not None
        assert _in_bbox(requisition.display_start.lon, requisition.display_start.lat)
        assert requisition.corridor_id == lrs.CORRIDOR_ID
        assert requisition.line_id in lrs.LINE_IDS
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
    assert geojson["properties"]["corridor_id"] == lrs.CORRIDOR_ID
    assert geojson["properties"]["projection_only"] is True

    linestrings = [f for f in geojson["features"] if f["geometry"]["type"] == "LineString"]
    points = [f for f in geojson["features"] if f["geometry"]["type"] == "Point"]
    assert len(linestrings) == 1 and len(points) == 2

    feature = linestrings[0]
    props = feature["properties"]
    # The authoritative identity is 1D and lives in the properties.
    for field in LRS_FIELDS:
        assert field in props, f"GeoJSON lost the LRS field {field}"
    assert props["corridor_id"] == lrs.CORRIDOR_ID
    assert props["line_id"] == "DN_FAST"
    assert props["lrs_key"] == [lrs.CORRIDOR_ID, "DN_FAST"]
    assert props["asset_id"] == "TMS-ENG-1006"
    assert props["dept"] == "CIVIL" and props["line"] == "DN_FAST"
    assert props["actm_para_204_buffer_mins"] == 15
    assert props["total_possession_mins"] == 210
    assert props["start_waypoint_id"].startswith("WP-")
    assert props["projection_only"] is True
    assert all(p["properties"]["projection_only"] for p in points)
    assert all(p["properties"]["line_id"] == "DN_FAST" for p in points)

    coords = feature["geometry"]["coordinates"]
    assert len(coords) >= 5
    for lon, lat in coords:
        assert _in_bbox(lon, lat), f"coordinate {lon},{lat} left the Mumbai bbox"

    # The first emitted vertex must equal the projected start point (own line).
    projection = report.accepted[0].display_start
    assert abs(coords[0][0] - projection.lon) < 1e-5
    assert abs(coords[0][1] - projection.lat) < 1e-5

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
    # NOTE: the optimizer now reasons purely in 1D - it clusters demands by
    # chainage proximity and forbids colliding kilometre intervals from sharing
    # a slot - so the guardrail below is the same interval test it solved with.
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

    # Every scheduled block states its possession in 1D.
    for block in result["scheduled_blocks"]:
        for field in LRS_FIELDS:
            assert field in block, f"scheduled block lost the LRS field {field}"
        assert block["line_id"] in lrs.LINE_IDS
        assert block["km_start"] <= block["km_end"]
        assert block["span_km"] == round(block["km_end"] - block["km_start"], 3)

    # The solver's own spatial statement was "no two colliding chainage intervals
    # share a slot", so an independent 1D re-check must find nothing to object to.
    for i in range(len(result["scheduled_blocks"])):
        for j in range(i + 1, len(result["scheduled_blocks"])):
            a, b = result["scheduled_blocks"][i], result["scheduled_blocks"][j]
            if (a["date"], a["start_time"]) != (b["date"], b["start_time"]):
                continue
            assert not has_overlap(a, b), (
                f"{a['block_id']} and {b['block_id']} share a slot with colliding chainage"
            )

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
        ("LRS: 1D interval algebra (has_overlap/gap/merge, corridor+line scoped)", test_lrs_interval_algebra),
        ("LRS: section codes are bookmarks, chainage decides a conflict", test_lrs_replaces_section_proximity),
        ("LRS: LinearSpan primary key + span invariants", test_lrs_span_contract),
        ("Projection table: 439 points / 29 stations / 59.98 km closure", test_waypoint_database),
        ("Chainage projection + quad-track lateral offsets", test_chainage_snapping_and_offsets),
        ("Frozen contract + LRS identity + silo alias normalisation", test_contract_fields_and_aliases),
        ("Contract rejects out-of-corridor / malformed chainage", test_contract_rejects_unsafe_chainage),
        ("ACTM Para 204: 15-minute earthing buffer arithmetic", test_actm_para_204_earthing_buffer),
        ("IRSEM Para 22: Form T/351 + T/352 generation", test_irsem_form_t351_disconnection),
        ("Plan guardrail: 1D chainage exclusivity + power isolation envelope", test_plan_guardrail_detects_conflicts),
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
    print(f"LRS collisions   : {len(report.chainage_conflict_pairs())} candidate pair(s) to bundle")
    print("-" * 68)
    if failures:
        print(f"{failures} TEST(S) FAILED")
        return 1
    print("ALL DATA-INGESTION TESTS PASSED - CONTRACT, WAYPOINTS & STATUTORY CHECKS VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
