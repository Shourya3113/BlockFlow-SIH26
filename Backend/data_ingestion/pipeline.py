"""
pipeline.py - Unified LRS Ingestion Pipeline (CRIS silos -> one contract)

The single entry point that turns heterogeneous departmental payloads into
validated, linear-referenced, safety-cleared requisitions that the HiGHS
optimizer can consume without a single runtime surprise.

    CRIS TMS / SMMS / TDMS / COA payloads
              |
              v
    normalise_raw_requisition()   heterogenous keys, encodings, bool spellings
              |
              v
    BlockRequisition (Pydantic v2)  frozen 5-field contract + LRS span + invariants
              |                 (corridor_id, line_id, km_start, km_end)
              v
    project_display()              439-point waypoint table -> read-only [lat, lon]
              |                 (display enrichment ONLY - never a solver input)
              v
    safety.evaluate_safety_invariants()   ACTM 203/204, IRSEM T/351, PSR
              |
              v
    IngestReport  -> accepted | rejected (with reasons) | GeoJSON | verdicts

Linear in, geographic out
-------------------------
The normaliser's *job* is to produce a 1D span, because that is the shape the
solver reasons about: two requisitions conflict iff their chainage intervals
collide on the same running line
(:func:`Backend.data_ingestion.lrs.has_overlap`). Latitude and longitude are
attached afterwards, as an inert projection for the frontend, and no constraint
or invariant reads them back.

Why silo payloads collapse so cleanly
-------------------------------------
CRIS exports use dozens of spellings for the same physical fact
(``KM_FROM``/``CHAINAGE_FROM``, ``TRACK``/``RUNNING_LINE``, ``Y``/``YES``/
``1``/``TRUE``). :data:`FIELD_ALIASES` is the one place that mapping lives, and
it is applied case-insensitively to every silo, so adding a fifth feed later is
a data change rather than a code change.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import ValidationError

from . import lrs, safety, waypoints
from .lrs import CORRIDOR_ID
from .schema import (
    DEPT_ALIASES,
    BlockRequisition,
    IngestReport,
    RawFeed,
    RejectedRequisition,
)

#: Canonical contract field -> accepted silo spellings (matched case-insensitively).
FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "asset_id": (
        "asset_id", "assetid", "requisition_no", "requisition_id", "req_no",
        "defect_id", "request_id", "work_order_no", "notice_no",
    ),
    "dept": (
        "dept", "department", "directorate", "wing", "maintainer",
        "discipline", "branch",
    ),
    "km_start": (
        "km_start", "km_from", "from_km", "chainage_from", "km_start_chainage",
        "km_from_chainage", "start_km",
    ),
    "km_end": (
        "km_end", "km_to", "to_km", "chainage_to", "km_end_chainage",
        "km_to_chainage", "end_km", "chainage",
    ),
    "corridor_id": (
        "corridor_id", "corridor", "corridor_code", "zone", "route", "route_id",
    ),
    "line_id": (
        "line_id", "line", "track", "track_id", "running_line", "line_no", "road",
        "track_no", "up_dn",
    ),
    #: On-track time the field unit is asking for. Distinct from the duration the
    #: optimizer later allocates, which is why the contract name says "requested".
    "requested_duration_mins": (
        "requested_duration_mins", "duration_mins", "duration", "duration_minutes",
        "allotted_mins", "minutes", "block_duration", "time_required", "demand_minutes",
    ),
    "urgency": (
        "urgency", "severity", "priority", "criticality", "class", "grade",
    ),
    "aci": (
        "aci", "aci_score", "aci_value", "criticality_index", "risk_score",
    ),
    #: A statutory form number a silo happened to cite. Recorded as provenance and
    #: nothing else - see Backend.data_ingestion.permits for why memos cannot be
    #: validated as inputs.
    "referenced_form": (
        "referenced_form", "statutory_form", "form", "form_no", "disconnection_form",
        "ptw_form", "irsem_form", "actm_form",
    ),
    "requires_power_block": (
        "requires_power_block", "power_block", "ohe_power_block", "ohe_required",
        "needs_power_block", "isolation_required", "power_off_required",
    ),
    "requires_traffic_block": (
        "requires_traffic_block", "traffic_block", "block_required",
        "needs_traffic_block", "traffic_stop_required",
    ),
    "requires_disconnection": (
        "requires_disconnection", "disconnection", "gear_disconnection",
        "disconnection_required", "snt_disconnection",
    ),
    "system": ("system", "source_system", "silo", "source"),
    #: Engineering attributes: what work is being asked for, and its coded fault.
    "work_type": (
        "work_type", "defect_type", "flaw_type", "maintenance_type",
        "activity", "nature_of_defect", "job_type",
    ),
    "fault_code": (
        "fault_code", "flaw_code", "defect_code", "failure_code", "fm_code",
    ),
    "description": ("description", "remarks", "observation", "details", "note"),
    "safety_weight": ("safety_weight", "safety_factor", "risk_weight"),
    "speed_restriction_psr": (
        "speed_restriction_psr", "psr_speed_kmph", "psr_speed", "psr",
        "speed_restriction", "temporary_speed", "psr_kmph",
    ),
    "days_overdue": ("days_overdue", "overdue_days", "ageing_days", "delay_days"),
    "target_completion_days": (
        "target_completion_days", "target_days", "sla_days", "due_days",
    ),
    "gmt": ("gmt", "annual_gmt", "traffic_density", "gmt_mgt"),
    "hotspot_temp_c": (
        "hotspot_temp_c", "hotspot_temp", "temperature_c", "thermovision_temp_c",
        "temp_c",
    ),
    "feeding_post": ("feeding_post", "fp", "feeding_station", "substation_code"),
    "isolator": ("isolator", "isolator_no", "isolator_id"),
    "gear_id": ("gear_id", "point_no", "point_machine", "equipment_id", "asset_gear_id"),
    "reported_date": ("reported_date", "report_date", "date", "booked_on", "notice_date"),
    "work_start": ("work_start", "block_start", "start_time", "from_time", "dept_time"),
    "status": ("status", "state", "work_status"),
}

#: Truthy spellings found in CRIS exports.
TRUE_TOKENS = {"1", "Y", "YES", "T", "TRUE", "REQ", "REQUIRED", "ON", "APPLICABLE"}
FALSE_TOKENS = {"0", "N", "NO", "F", "FALSE", "NA", "N/A", "NIL", "NONE", "NOT REQ", "OFF"}

#: Default silo -> source-system label when the payload does not carry one.
SOURCE_LABELS = {
    "CIVIL": "TMS", "SNT": "SMMS", "TRD": "TDMS", "COA": "COA",
}

DEFAULT_FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


# --------------------------------------------------------------------------- #
#  Raw-field extraction helpers
# --------------------------------------------------------------------------- #
def _index_keys(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Case/whitespace-insensitive view of a raw payload's keys."""
    return {str(k).strip().upper().replace(" ", "_"): v for k, v in raw.items()}


def _pick(raw: Mapping[str, Any], aliases: Sequence[str]) -> Optional[Any]:
    """First non-empty value among ``aliases`` (case-insensitive)."""
    indexed = _index_keys(raw)
    for alias in aliases:
        key = alias.strip().upper().replace(" ", "_")
        if key in indexed:
            value = indexed[key]
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            return value
    return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce CRIS boolean spellings (``'Y'``, ``'Yes'``, ``1``, ``'TRUE'``)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value).strip().upper()
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    return default


def infer_source(raw: Mapping[str, Any], source: Optional[str] = None) -> Optional[str]:
    """
    Resolve which CRIS silo sent a payload.

    Explicit feed declaration wins, then the payload's own system field, then a
    department token (``ENGINEERING`` -> TMS, ``S&T`` -> SMMS,
    ``ELECTRICAL_TRD`` -> TDMS, ...) via the shared department alias table.
    """
    if source:
        return str(source).strip().upper()
    for candidate in (
        _pick(raw, FIELD_ALIASES["system"]),
        _pick(raw, FIELD_ALIASES["dept"]),
    ):
        if candidate is None:
            continue
        token = str(candidate).strip().upper()
        if token in ("TMS", "SMMS", "TDMS", "COA"):
            return token
        department = DEPT_ALIASES.get(token)
        if department is not None:
            return SOURCE_LABELS.get(department.value)
    return None


def _source_date(raw: Mapping[str, Any]) -> Optional[str]:
    """Normalise the reported date to ISO where it is recognisable."""
    value = _pick(raw, FIELD_ALIASES["reported_date"])
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


# --------------------------------------------------------------------------- #
#  Raw -> contract
# --------------------------------------------------------------------------- #
def pick_field(raw: Mapping[str, Any], field: str) -> Optional[Any]:
    """Public wrapper: first non-empty silo spelling for a contract field."""
    return _pick(raw, FIELD_ALIASES.get(field, ()))


def pick_any(raw: Mapping[str, Any], aliases: Sequence[str]) -> Optional[Any]:
    """Public wrapper: first non-empty value among arbitrary aliases."""
    return _pick(raw, aliases)


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Public wrapper around the CRIS boolean coercion."""
    return _coerce_bool(value, default)


def normalize_legacy_defect(defect: Mapping[str, Any], source: Optional[str] = None) -> BlockRequisition:
    """
    Contract-ise the *existing* synthetic generator output.

    Lets the already-wired TMS/SMMS/TDMS generators flow through the same
    validation, waypoint snapping and safety gate without changing them.
    """
    return normalize_raw_requisition(defect, source=source or defect.get("system"))


def normalize_raw_requisition(
    raw: Mapping[str, Any],
    source: Optional[str] = None,
    index: int = 0,
) -> BlockRequisition:
    """
    Collapse a silo payload into a validated :class:`BlockRequisition`.

    Raises :class:`pydantic.ValidationError` (contract breach) or
    :class:`ValueError` (unmappable payload). Callers that must not abort a whole
    feed should use :func:`ingest_feed`, which captures both.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(f"requisition #{index} is {type(raw).__name__}, expected an object")

    payload: Dict[str, Any] = {}
    for field, aliases in FIELD_ALIASES.items():
        if field in ("system", "reported_date", "work_start"):
            continue  # handled explicitly below
        value = _pick(raw, aliases)
        if value is not None:
            payload[field] = value

    # ---- source silo / system label -------------------------------------- #
    resolved_source = infer_source(raw, source)
    payload["system"] = str(_pick(raw, FIELD_ALIASES["system"]) or resolved_source or "UNKNOWN").upper()

    # ---- dept: fall back to the feed's own silo label --------------------- #
    if "dept" not in payload and resolved_source in SOURCE_LABELS:
        payload["dept"] = SOURCE_LABELS[resolved_source]
    if "dept" not in payload:
        raise ValueError(
            f"requisition #{index} carries no department/directorate field and the "
            "feed does not declare one"
        )

    # ---- booleans: explicit, otherwise derived from the work type --------- #
    power = _pick(raw, FIELD_ALIASES["requires_power_block"])
    if power is None and resolved_source == "TDMS":
        power = True  # every OHE job needs the TPC isolation (ACTM Para 203)
    payload["requires_power_block"] = _coerce_bool(power, default=False)

    traffic = _pick(raw, FIELD_ALIASES["requires_traffic_block"])
    payload["requires_traffic_block"] = _coerce_bool(
        traffic, default=payload["requires_power_block"]
    )

    disconnection = _pick(raw, FIELD_ALIASES["requires_disconnection"])
    if disconnection is None and resolved_source == "SMMS":
        disconnection = True  # S&T gear work is a Form T/351 disconnection by default
    payload["requires_disconnection"] = _coerce_bool(disconnection, default=False)

    # ---- LRS identity ---------------------------------------------------- #
    # A single-corridor deployment lets the corridor default in; the running
    # line never does, because a job on the wrong line is a different job.
    payload.setdefault("corridor_id", CORRIDOR_ID)
    if payload.get("corridor_id") in (None, ""):
        payload["corridor_id"] = CORRIDOR_ID

    # ---- provenance + bookkeeping ---------------------------------------- #
    payload["reported_date"] = _source_date(raw)
    payload["defect_id"] = str(payload.get("asset_id")) if payload.get("asset_id") else None
    payload["status"] = str(_pick(raw, FIELD_ALIASES["status"]) or "PENDING").upper()

    work_start = _pick(raw, FIELD_ALIASES["work_start"])
    if work_start is not None:
        payload["work_start"] = work_start

    return BlockRequisition(**payload)


# --------------------------------------------------------------------------- #
#  GeoJSON emission
# --------------------------------------------------------------------------- #
def requisition_geojson_feature(requisition: BlockRequisition) -> Dict[str, Any]:
    """
    One requisition as a GeoJSON Feature, for **rendering only**.

    This is the downstream half of the linear-in/geographic-out rule: the LRS
    identity (``corridor_id``/``line_id``/``km_start``/``km_end``) is written
    into the feature properties as the authoritative identity, and the
    LineString coordinates are the projected *display* of it. Nothing consumes
    this feature to decide a schedule or to test a collision - the optimizer
    and the guardrail both work off the LRS fields directly.
    """
    from .spatial import (
        TRACK_LATERAL_OFFSET_M,
        geojson_linestring,
        polyline_between,
        shift_polyline,
    )

    if requisition.display_start is None or requisition.display_end is None:
        requisition.project_display()

    coords = polyline_between(
        waypoints.CENTERLINE,
        waypoints.CENTERLINE_CHAINAGE,
        requisition.km_start,
        requisition.km_end,
        step_km=0.25,
    )
    offset_m = TRACK_LATERAL_OFFSET_M.get(requisition.line_id, 0.0)
    line_coords = shift_polyline(coords, offset_m)

    start_fix = requisition.display_start
    window = safety.compute_earthing_window(
        None, requisition.requested_duration_mins, requisition.power_isolation_required
    )
    properties = {
        # ---- authoritative 1D identity ---------------------------------- #
        **requisition.to_lrs_dict(),
        "lrs_key": list(requisition.lrs_key),
        # ---- contract / provenance ------------------------------------- #
        "asset_id": requisition.asset_id,
        "dept": requisition.dept.value,
        "system": requisition.system,
        "line": requisition.line_id,
        "section": requisition.section,
        "section_name": requisition.section_name,
        "requested_duration_mins": requisition.requested_duration_mins,
        "work_type": requisition.work_type,
        "fault_code": requisition.fault_code,
        "speed_restriction_psr": requisition.speed_restriction_psr,
        "urgency": requisition.urgency.value,
        "priority_tier": requisition.priority_tier,
        "aci": requisition.aci,
        # Provenance: what the silo cited, if anything. The instruments that must
        # exist are generated downstream by permits.build_permits_for_block.
        "referenced_form": requisition.referenced_form.value if requisition.referenced_form else None,
        "requires_power_block": requisition.requires_power_block,
        "requires_traffic_block": requisition.requires_traffic_block,
        "requires_disconnection": requisition.requires_disconnection,
        "actm_para_204_buffer_mins": safety.ACTM_PARA_204_EARTHING_BUFFER_MINS,
        "total_possession_mins": window["total_possession_mins"],
        # ---- projected display extras ---------------------------------- #
        "start_station": start_fix.station_code if start_fix else None,
        "start_waypoint_id": start_fix.nearest_waypoint_id if start_fix else None,
        "end_station": requisition.display_end.station_code if requisition.display_end else None,
        "track_offset_m": offset_m,
        "lateral_offset_applied": True,
        "projection_only": True,
    }
    feature = geojson_linestring(line_coords, properties)
    feature["id"] = requisition.asset_id
    feature["geometry"]["type"] = "LineString"
    # Anchor point at the start of the possession for map labels.
    feature["geometry_start"] = (
        {"type": "Point", "coordinates": start_fix.geojson_coordinates}
        if start_fix else None
    )
    return feature


def build_requisition_geojson(
    requisitions: Iterable[BlockRequisition],
    name: str = "WR_MAINTENANCE_REQUISITIONS",
) -> Dict[str, Any]:
    """
    FeatureCollection of every accepted requisition (PM Gati Shakti export).

    An **auxiliary display artefact**. It is produced from the LRS spans but is
    never fed back into the optimizer, the safety invariants or any collision
    test - those all read ``corridor_id``/``line_id``/``km_start``/``km_end``.
    """
    from .spatial import geojson_feature_collection, geojson_point

    features: List[Dict[str, Any]] = []
    for requisition in requisitions:
        features.append(requisition_geojson_feature(requisition))
        for label in ("display_start", "display_end"):
            projection = getattr(requisition, label, None)
            if projection is not None:
                features.append(
                    geojson_point(
                        projection.lon,
                        projection.lat,
                        {
                            "corridor_id": requisition.corridor_id,
                            "line_id": requisition.line_id,
                            "asset_id": requisition.asset_id,
                            "dept": requisition.dept.value,
                            "marker": label,
                            "km": projection.km,
                            "section": projection.section,
                            "station_code": projection.station_code,
                            "waypoint_id": projection.nearest_waypoint_id,
                            "projection_only": True,
                        },
                    )
                )
    return geojson_feature_collection(
        features,
        name=name,
        extra_properties={
            "corridor_id": CORRIDOR_ID,
            "corridor": waypoints.CORRIDOR["name"],
            "length_km": lrs.CORRIDOR_LENGTH_KM,
            "requisition_count": len(features),
            "datum": "WGS84",
            "projection_only": True,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


# --------------------------------------------------------------------------- #
#  Feed ingestion
# --------------------------------------------------------------------------- #
def _validation_errors(exc: Exception, index: int) -> List[str]:
    """Human-readable, jury-friendly strings from a pydantic/ValueError."""
    if isinstance(exc, ValidationError):
        messages: List[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
            messages.append(f"{location or 'record'}: {error.get('msg')}")
        return messages
    return [f"record {index}: {exc}"]


def ingest_feed(
    payload: Any,
    source: Optional[str] = None,
    scorer: Optional[Any] = None,
    build_geojson: bool = True,
) -> IngestReport:
    """
    Ingest one CRIS feed and return the full acceptance / rejection report.

    ``payload`` may be a :class:`RawFeed`, a ``{"requisitions": [...]}`` mapping,
    a bare list of raw records, or a single raw record. Nothing raises for bad
    data - malformed requisitions are quarantined with their reasons so the
    optimizer still receives every valid job.

    ``scorer`` is an optional callable ``(list[dict]) -> list[dict]`` (the
    existing :class:`AssetCriticalityPrioritizer.rank_maintenance_demands`) used
    to back-fill the contract's ``aci`` field.
    """
    feed_id: Optional[str] = None
    declared_source: Optional[str] = source
    records: List[Any]

    if isinstance(payload, RawFeed):
        feed = payload
        feed_id, declared_source = feed.feed_id, declared_source or feed.source
        records = list(feed.requisitions)
    elif isinstance(payload, Mapping):
        feed_id = str(payload.get("feed_id") or "") or None
        declared_source = declared_source or (str(payload.get("source")) if payload.get("source") else None)
        candidate = payload.get("requisitions", payload.get("records", payload.get("data")))
        if candidate is None:
            records = [payload]
        elif isinstance(candidate, Mapping):
            records = [candidate]
        else:
            records = list(candidate)
    elif isinstance(payload, (list, tuple)):
        records = list(payload)
    elif payload is None:
        records = []
    else:
        records = [payload]

    declared_source = declared_source.upper() if declared_source else None

    # ---- corridor identity ------------------------------------------------ #
    # A feed may declare its corridor; a per-record field always wins over it.
    declared_corridor = CORRIDOR_ID
    raw_corridor = (
        payload.get("corridor") if isinstance(payload, Mapping)
        else getattr(payload, "corridor", None)
    )
    if raw_corridor:
        declared_corridor = lrs.coerce_corridor_id(raw_corridor)

    accepted: List[BlockRequisition] = []
    rejected: List[RejectedRequisition] = []
    warnings: List[str] = []

    for index, raw in enumerate(records):
        raw_mapping = raw if isinstance(raw, Mapping) else {"value": raw}
        if isinstance(raw_mapping, Mapping) and _pick(raw_mapping, FIELD_ALIASES["corridor_id"]) is None:
            raw_mapping = {**raw_mapping, "corridor_id": declared_corridor}
        try:
            requisition = normalize_raw_requisition(raw_mapping, source=declared_source, index=index)
            # Read-only display projection: attached after validation, never
            # consulted by it.
            requisition.project_display()
        except (ValidationError, ValueError, TypeError) as exc:
            rejected.append(
                RejectedRequisition(
                    index=index,
                    source=declared_source,
                    asset_id=_pick(raw_mapping, FIELD_ALIASES["asset_id"]) if isinstance(raw_mapping, Mapping) else None,
                    dept=_pick(raw_mapping, FIELD_ALIASES["dept"]) if isinstance(raw_mapping, Mapping) else None,                        km_start=_pick(raw_mapping, FIELD_ALIASES["km_start"]) if isinstance(raw_mapping, Mapping) else None,
                        km_end=_pick(raw_mapping, FIELD_ALIASES["km_end"]) if isinstance(raw_mapping, Mapping) else None,
                        line_id=_pick(raw_mapping, FIELD_ALIASES["line_id"]) if isinstance(raw_mapping, Mapping) else None,
                        corridor_id=declared_corridor,
                        errors=_validation_errors(exc, index),
                    raw=dict(raw_mapping) if isinstance(raw_mapping, Mapping) else {"value": repr(raw)},
                )
            )
            continue
        accepted.append(requisition)

    # ---- ACI back-fill (optional, keeps the contract's aci column honest) -- #
    if scorer is not None and accepted:
        try:
            scored = scorer([r.to_legacy_dict() for r in accepted])
            scores = {str(item.get("asset_id") or item.get("defect_id")): item.get("aci_score") for item in scored}
            for requisition in accepted:
                score = scores.get(requisition.asset_id)
                if score is not None:
                    requisition.aci = round(float(score), 1)
        except Exception as exc:  # scoring must never block ingestion
            warnings.append(f"ACI scorer unavailable, aci column left null: {exc}")

    # ---- statutory safety ledger ------------------------------------------ #
    verdicts = [safety.evaluate_safety_invariants(r.model_dump(mode="json")) for r in accepted]
    blocked = [v["asset_id"] for v in verdicts if not v["passed"]]
    if blocked:
        warnings.append(
            f"{len(blocked)} requisition(s) carry a statutory violation and are flagged "
            f"BLOCKED_BY_SAFETY_INVARIANT: {', '.join(blocked)}"
        )

    report = IngestReport(
        feed_id=feed_id,
        source=declared_source,
        corridor_id=declared_corridor,
        corridor=str(waypoints.CORRIDOR["name"]),
        received=len(records),
        accepted_count=len(accepted),
        rejected_count=len(rejected),
        acceptance_pct=round(len(accepted) / len(records) * 100.0, 1) if records else 100.0,
        accepted=accepted,
        rejected=rejected,
        safety=verdicts,
        warnings=warnings,
    )
    if build_geojson:
        report.geojson = build_requisition_geojson(accepted)
    return report


def merge_reports(reports: Sequence[IngestReport]) -> IngestReport:
    """Combine the three directorate feeds into one corridor-wide report."""
    accepted: List[BlockRequisition] = []
    rejected: List[RejectedRequisition] = []
    verdicts: List[Dict[str, Any]] = []
    warnings: List[str] = []
    received = 0
    for report in reports:
        accepted.extend(report.accepted)
        rejected.extend(report.rejected)
        verdicts.extend(report.safety)
        warnings.extend(w for w in report.warnings if w not in warnings)
        received += report.received
    merged = IngestReport(
        feed_id="+".join(filter(None, (r.feed_id for r in reports))) or None,
        source="+".join(filter(None, (r.source for r in reports))) or None,
        corridor_id=CORRIDOR_ID,
        corridor=str(waypoints.CORRIDOR["name"]),
        received=received,
        accepted_count=len(accepted),
        rejected_count=len(rejected),
        acceptance_pct=round(len(accepted) / received * 100.0, 1) if received else 100.0,
        accepted=accepted,
        rejected=rejected,
        safety=verdicts,
        warnings=warnings,
    )
    merged.geojson = build_requisition_geojson(accepted)
    return merged


# --------------------------------------------------------------------------- #
#  Fixture loading + one-shot corridor ingestion
# --------------------------------------------------------------------------- #
def fixture_path(name: str = "raw_cris_requisitions.json") -> str:
    return os.path.join(DEFAULT_FIXTURE_DIR, name)


def load_fixture(name: str = "raw_cris_requisitions.json") -> Dict[str, Any]:
    with open(fixture_path(name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_raw_requisitions() -> List[Dict[str, Any]]:
    """The 50 Western Railway defect requisitions used for Phase-2 ingestion."""
    payload = load_fixture("raw_cris_requisitions.json")
    return list(payload.get("requisitions", []))


def load_malformed_cases() -> List[Dict[str, Any]]:
    """The hostile/malformed payload corpus with its expected outcomes."""
    payload = load_fixture("malformed_payloads.json")
    return list(payload.get("cases", []))


def ingest_corridor(scorer: Optional[Any] = None) -> IngestReport:
    """
    Ingest the three siloed reference feeds over the whole corridor.

    Falls back to the packaged fixtures when a live CRIS feed is unavailable -
    the offline-demo requirement of the hackathon brief.
    """
    total = load_raw_requisitions()
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for raw in total:
        label = infer_source(raw) or "TMS"
        by_source.setdefault(label, []).append(raw)
    return merge_reports(
        [ingest_feed(records, source=label, scorer=scorer) for label, records in sorted(by_source.items())]
    )


def to_optimizer_payload(report: IngestReport) -> List[Dict[str, Any]]:
    """
    Bridge to the existing HiGHS optimizer / ACI engine.

    Only safety-cleared requisitions are handed over, so a statutory violation
    can never reach the solver - the guardrail is upstream of the mathematics.
    """
    blocked = {v["asset_id"] for v in report.safety if not v["passed"]}
    return [r.to_legacy_dict() for r in report.accepted if r.asset_id not in blocked]


def write_geojson(report: IngestReport, path: str) -> str:
    """Persist an ingestion report's GeoJSON for PM Gati Shakti / CesiumJS."""
    if report.geojson is None:
        report.geojson = build_requisition_geojson(report.accepted)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.geojson, handle, indent=2)
    return path


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    corridor = ingest_corridor()
    summary = corridor.summary()
    print(f"Feed            : {corridor.feed_id or 'ALL SILOS'}")
    print(f"Received        : {summary['received']}")
    print(f"Accepted        : {summary['accepted']}  ({summary['acceptance_pct']}%)")
    print(f"Rejected        : {summary['rejected']}")
    print(f"By department   : {summary['by_department']}")
    print(f"Safety          : {summary['safety_passed']} pass / {summary['safety_failed']} fail")
    for warning in summary["warnings"]:
        print(f"  ! {warning}")
    if corridor.accepted:
        first = corridor.accepted[0]
        print(
            f"\nSample          : {first.asset_id} [{first.dept.value}] "
            f"{first.corridor_id} / {first.line_id} "
            f"km {first.km_start}-{first.km_end} (span {first.span_km} km)"
        )
        print(f"LRS span        : {first.to_lrs_dict()}")
        print(
            f"Display         : {first.display_start.coordinates} ({first.display_start.nearest_waypoint_id}) "
            "[read-only projection, not a solver input]"
        )
        colliding = corridor.chainage_conflict_pairs()
        print(f"1D collisions   : {len(colliding)} candidate pair(s) to bundle")
        print(f"Optimizer rows  : {len(to_optimizer_payload(corridor))}")
