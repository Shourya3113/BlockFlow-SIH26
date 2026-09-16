"""
data_ingestion - Unified CRIS LRS Ingestion Layer (BlockFlow / IR-ABPS)

Turns four siloed Indian Railways data sources into one validated,
linear-referenced, statutorily-safe stream of block requisitions.

Linear referencing is the core abstraction
------------------------------------------
A track, a defect and a possession are all identified the way Indian Railways
identifies them - ``(corridor_id, line_id, km_start, km_end)``. Collision is a
closed-form 1D interval test (:func:`lrs.has_overlap`) on those four values;
there is no polygon, no buffer radius and no geometric intersection anywhere in
the scheduling path. WGS84 coordinates exist only as a read-only projection for
rendering.

Modules
-------
``lrs``         The 1D linear referencing system: corridor/line ids, the
                ``LinearSpan`` contract and the interval-collision algebra.
``spatial``     WGS84 geodesy, chainage maths, lateral track offsets, GeoJSON.
``waypoints``   The 29-station / **439-waypoint** Churchgate-Virar projection
                table: kilometre post -> ``[lat, lon]``, for display only.
``schema``      The frozen Pydantic v2 contract ``[asset_id, dept, km_start,
                km_end, aci]`` on an LRS span, plus report models.
``safety``      ACTM Vol II Para 203/204 (15-minute earthing buffers), IRSEM
                Para 22 (Form T/351 / T/352), IRPWM 268(b) PSR invariants, and
                the 1D chainage-exclusivity plan guardrail.
``permits``     The **downstream** statutory paperwork. Derives which
                instruments a block needs from its engineering attributes, and
                prefills Form T/351, Form T/352, the ACTM Permit-to-Work and
                the IRPWM 284 caution order once a window has been allocated.
                Memos are outputs, never input validation gates.
``pipeline``    The normaliser that collapses heterogeneous silo payloads into
                the LRS contract and emits PM Gati Shakti GeoJSON.
``tms_data``    Track Management System silo (P-Way, USFD flaws, geometry).
``smms_data``   Signal Maintenance Management System silo (point machines,
                MSDAC axle counters, Form T/351 disconnections).
``tdms_data``   Traction Distribution Management System silo (25 kV OHE
                hotspots, isolator / feeding-post boundaries).
``coa_data``    Control Office Application silo (block windows, train paths).

Typical use
-----------
>>> from Backend.data_ingestion import ingest_corridor, to_optimizer_payload, has_overlap
>>> report = ingest_corridor()                 # 50 reference CRIS requisitions
>>> [r.line_id for r in report.accepted[:2]]   # LRS identity, not lat/lon
['DN_FAST', 'UP_SLOW']
>>> rows = to_optimizer_payload(report)        # safety-cleared, LRS-shaped
>>> print(report.summary()["by_department"])
{'CIVIL': 20, 'SNT': 15, 'TRD': 15}
>>> has_overlap(rows[0], rows[1])              # the whole spatial solver test
False

Statutory memos are generated, not required
-------------------------------------------
A requisition carries engineering attributes only. Once the optimizer allocates
a window, :func:`Backend.data_ingestion.permits.build_permits_for_block`
prefills the IRSEM Form T/351 / T/352, the ACTM Permit-to-Work with its Para 204
earthing wrap, and the caution order - unsigned, for the competent authority to
release.

>>> from Backend.data_ingestion import build_permits_for_block
>>> permit = build_permits_for_block(scheduled_block)
>>> permit.memo_index
{'T_351': 'IR-DGP-IR-BLK-2026001/T351', 'T_352': '.../T352', 'ACTM_PTW': '.../PTW'}
"""

from . import coa_data, lrs, permits, pipeline, safety, schema, smms_data, spatial, tdms_data, tms_data, waypoints
from .permits import (
    ACTMPermitToWork,
    AffectedSignallingGear,
    Authority,
    AuthorizationSignature,
    DigitalGrantPermit,
    EarthingBuffer,
    FormCode,
    FormT351Notice,
    FormT352Notice,
    GearAction,
    GearType,
    IRPWM284CautionOrder,
    PermitRequirements,
    PermitStatus,
    SignatureStatus,
    build_permits_for_block,
    build_permits_for_requisition,
    derive_permit_requirements,
)
from .lrs import (
    CORRIDOR_ID,
    LINE_IDS,
    LinearSpan,
    LineId,
    conflicting_indices,
    find_collisions,
    gap_km,
    has_overlap,
    merge_spans,
    overlap_length_km,
)
from .pipeline import (
    build_requisition_geojson,
    ingest_corridor,
    ingest_feed,
    load_malformed_cases,
    load_raw_requisitions,
    merge_reports,
    to_optimizer_payload,
    write_geojson,
)
from .schema import (
    CONTRACT_FIELDS,
    LRS_FIELDS,
    BlockRequisition,
    Department,
    DisplayProjection,
    IngestReport,
    Line,
    Severity,
    SpatialFix,
)
from .safety import (
    ACTM_PARA_204_EARTHING_BUFFER_MINS,
    evaluate_safety_invariants,
    generate_statutory_memos,
    safety_summary,
    validate_plan,
)
from .waypoints import CORRIDOR, CORRIDOR_SECTIONS, STATION_GROUND_CONTROL, WAYPOINTS, waypoint_at_km

__all__ = [
    "coa_data",
    "lrs",
    "permits",
    "pipeline",
    "safety",
    "schema",
    "smms_data",
    "spatial",
    "tdms_data",
    "tms_data",
    "waypoints",
    # lrs
    "CORRIDOR_ID",
    "LINE_IDS",
    "LineId",
    "LinearSpan",
    "conflicting_indices",
    "find_collisions",
    "gap_km",
    "has_overlap",
    "merge_spans",
    "overlap_length_km",
    # pipeline
    "build_requisition_geojson",
    "ingest_corridor",
    "ingest_feed",
    "load_malformed_cases",
    "load_raw_requisitions",
    "merge_reports",
    "to_optimizer_payload",
    "write_geojson",
    # schema
    "CONTRACT_FIELDS",
    "LRS_FIELDS",
    "BlockRequisition",
    "Department",
    "DisplayProjection",
    "IngestReport",
    "Line",
    "Severity",
    "SpatialFix",
    # permits
    "ACTMPermitToWork",
    "AffectedSignallingGear",
    "Authority",
    "AuthorizationSignature",
    "DigitalGrantPermit",
    "EarthingBuffer",
    "FormCode",
    "FormT351Notice",
    "FormT352Notice",
    "GearAction",
    "GearType",
    "IRPWM284CautionOrder",
    "PermitRequirements",
    "PermitStatus",
    "SignatureStatus",
    "build_permits_for_block",
    "build_permits_for_requisition",
    "derive_permit_requirements",
    # safety
    "ACTM_PARA_204_EARTHING_BUFFER_MINS",
    "evaluate_safety_invariants",
    "generate_statutory_memos",
    "safety_summary",
    "validate_plan",
    # waypoints
    "CORRIDOR",
    "CORRIDOR_SECTIONS",
    "STATION_GROUND_CONTROL",
    "WAYPOINTS",
    "waypoint_at_km",
]
