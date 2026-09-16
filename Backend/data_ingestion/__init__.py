"""
data_ingestion - Unified CRIS Spatial Ingestion Layer (BlockFlow / IR-ABPS)

Turns four siloed Indian Railways data sources into one validated, spatially
snapped, statutorily-safe stream of block requisitions.

Modules
-------
``spatial``     WGS84 geodesy, chainage maths, lateral track offsets, GeoJSON.
``waypoints``   The 29-station / **439-waypoint** Churchgate-Virar chainage
                database used to snap statutory kilometerage to coordinates.
``schema``      The frozen Pydantic v2 contract ``[asset_id, dept, km_start,
                km_end, aci]`` plus feed/ingestion report models.
``safety``      ACTM Vol II Para 203/204 (15-minute earthing buffers), IRSEM
                Para 22 (Form T/351 / T/352), IRPWM 268(b) PSR invariants.
``pipeline``    The normaliser that collapses heterogeneous silo payloads into
                the contract and emits PM Gati Shakti GeoJSON.
``tms_data``    Track Management System silo (P-Way, USFD flaws, geometry).
``smms_data``   Signal Maintenance Management System silo (point machines,
                MSDAC axle counters, Form T/351 disconnections).
``tdms_data``   Traction Distribution Management System silo (25 kV OHE
                hotspots, isolator / feeding-post boundaries).
``coa_data``    Control Office Application silo (block windows, train paths).

Typical use
-----------
>>> from Backend.data_ingestion import ingest_corridor, to_optimizer_payload
>>> report = ingest_corridor()                 # 50 reference CRIS requisitions
>>> rows = to_optimizer_payload(report)        # safety-cleared, contract-shaped
>>> print(report.summary()["by_department"])
{'CIVIL': 20, 'SNT': 15, 'TRD': 15}
"""

from . import coa_data, pipeline, safety, schema, smms_data, spatial, tdms_data, tms_data, waypoints
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
    BlockRequisition,
    Department,
    IngestReport,
    Line,
    Severity,
    SpatialFix,
    StatutoryForm,
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
    "pipeline",
    "safety",
    "schema",
    "smms_data",
    "spatial",
    "tdms_data",
    "tms_data",
    "waypoints",
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
    "BlockRequisition",
    "Department",
    "IngestReport",
    "Line",
    "Severity",
    "SpatialFix",
    "StatutoryForm",
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
