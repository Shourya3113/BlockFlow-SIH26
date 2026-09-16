"""
schema.py - BlockFlow Unified Ingestion Contract (Pydantic v2)

The **day-1 frozen contract** that lets three siloed CRIS directorates speak one
language. Every requisition that reaches the HiGHS optimizer - whether it came
from TMS (Civil/P-Way), SMMS (Signal & Telecom) or TDMS (Traction Distribution)
- must first become a :class:`BlockRequisition`.

LRS-primary by construction
---------------------------
:class:`BlockRequisition` inherits :class:`~Backend.data_ingestion.lrs.LinearSpan`,
so every requisition *is* a 1D linear-referencing span:

    (corridor_id, line_id, km_start, km_end)

That tuple is the primary spatial key. ``km_start``/``km_end`` are kilometre
posts on a named running line - the units Indian Railways actually stores in
TMS / SMMS / TDMS / COA. Collision between two possessions is then a closed-form
interval comparison (:func:`Backend.data_ingestion.lrs.has_overlap`) on four
floats, with no projection, no polygon and no tolerance parameter.

Geography - ``[lat, lon]`` - is attached *afterwards*, strictly as a read-only
display payload (:class:`DisplayProjection`) for the frontend and the 3D twin.
No solver constraint and no validation invariant ever reads it.

Core contract (frozen)
----------------------
``[asset_id, dept, km_start, km_end, aci]``

Everything else is either provenance (which silo sent it), physical attributes
(duration, whether a 25 kV power block is needed) or enrichment (the derived
section, the display projection). Extra keys from a legacy silo are *kept*, never
silently dropped, so the audit trail from raw CRIS payload to optimiser input
is complete.

Design rules
------------
1.  **Coerce, don't guess.** CRIS feeds are heterogeneous: chainage arrives as
    ``"19.40"``, department as ``"ENGINEERING"``, duration as ``"1:30"``. We
    normalise those deterministically and record what we changed.
2.  **Fail loudly on safety numbers.** A chainage outside the 59.98 km corridor,
    an inverted ``km_start > km_end`` or a zero-duration block is *rejected*,
    never clamped - a silently clamped kilometre is how a tamper lands on the
    wrong track.
3.  **No wall-clock or random state** in validation, so a payload validates
    identically on the jury laptop and on the divisional workstation.
4.  **Linear in, geographic out.** Nothing that decides a schedule may consume
    a latitude.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from . import waypoints
from .lrs import (
    CORRIDOR_ID,
    CORRIDOR_LENGTH_KM,
    LINE_IDS,
    LineId,
    LinearSpan,
    find_collisions,
)
from .safety import ACTM_PARA_204_EARTHING_BUFFER_MINS

#: The frozen five-field contract other modules may rely on.
CONTRACT_FIELDS: List[str] = ["asset_id", "dept", "km_start", "km_end", "aci"]

#: The LRS primary spatial key. ``corridor_id`` + ``line_id`` + a half-open
#: kilometre interval identify a possession uniquely in one dimension; every
#: spatial question in BlockFlow is answered by comparing exactly these values.
LRS_FIELDS: List[str] = ["corridor_id", "line_id", "km_start", "km_end"]

#: Minimum silo spellings the contract itself absorbs (matched case-insensitively)
#: so the schema stays usable standalone - by the FastAPI gateway, the optimizer
#: or a notebook - without importing the full pipeline alias table.
#: The long tail of CRIS spellings lives in ``pipeline.FIELD_ALIASES``.
CONTRACT_ALIASES: Dict[str, Tuple[str, ...]] = {
    "asset_id": ("requisition_no", "req_no", "assetid"),
    "dept": ("department", "directorate", "wing"),
    "corridor_id": ("corridor", "corridor_code", "zone_corridor"),
    "line_id": ("line", "track", "track_id", "running_line", "road"),
    "km_start": ("km_from", "from_km"),
    "km_end": ("km_to", "to_km"),
    "duration_mins": ("duration", "minutes"),
    "urgency": ("severity", "priority"),
    "statutory_form": ("form",),
    "aci": ("aci_score",),
}

#: Minimum permitted duration for any on-track possession (minutes).
MIN_DURATION_MINS = 15
#: Practical maximum single possession (minutes) - a single block window.
MAX_DURATION_MINS = 480

#: Longest single possession on the corridor (km). A span longer than this is
#: not one engineering possession but a corridor-wide shutdown, which the
#: planner must decompose into section-wise blocks.
MAX_POSSESSION_SPAN_KM = 19.98

_ASSET_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.&/-]{2,31}$")
_HHMM_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")
_MINUTES_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:min|mins|minutes|m)?$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
#  Enumerations + silo alias maps
# --------------------------------------------------------------------------- #
class Department(str, Enum):
    """Engineering directorate that owns the asset."""

    CIVIL = "CIVIL"   # TMS - P-Way, USFD rail flaws, track geometry
    SNT = "SNT"       # SMMS - signals, point machines, MSDAC axle counters
    TRD = "TRD"       # TDMS - 25 kV OHE, isolators, feeding posts


class Line(str, Enum):
    """
    One of the four running lines of the Mumbai suburban quad corridor.

    The values are exactly the :data:`~Backend.data_ingestion.lrs.LineId`
    literals, so ``Line`` and ``LineId`` are interchangeable at runtime. The
    enum survives only as a convenience for callers that want a named member;
    the canonical contract field is the plain ``line_id`` string.
    """

    UP_FAST = "UP_FAST"
    UP_SLOW = "UP_SLOW"
    DN_SLOW = "DN_SLOW"
    DN_FAST = "DN_FAST"

    @classmethod
    def of(cls, value: Any) -> "Line":
        """Resolve any silo running-line spelling onto a member."""
        from .lrs import coerce_line_id

        return cls(coerce_line_id(value))


class Severity(str, Enum):
    """SLA class used by the ACI engine."""

    CRITICAL = "CRITICAL"   # P1 - USFD IMR, OHE hotspot, point machine dead
    URGENT = "URGENT"       # P2
    ROUTINE = "ROUTINE"     # P3


class StatutoryForm(str, Enum):
    """Railway statutory paperwork referenced by a requisition."""

    T_351 = "T_351"            # IRSEM - S&T disconnection notice
    T_352 = "T_352"            # IRSEM - S&T reconnection notice
    ACTM_203 = "ACTM_203"      # ACTM Vol II Para 203 - TPC isolation
    ACTM_204 = "ACTM_204"      # ACTM Vol II Para 204 - 15-min earthing buffer
    IRPWM_268B = "IRPWM_268B"  # Permanent speed restriction
    IRPWM_284 = "IRPWM_284"    # P-Way work under traffic
    IRSEM_22 = "IRSEM_22"      # Form T/351 disconnection
    NONE = "NONE"


#: Silo spellings -> canonical department. Keys are upper-cased before lookup.
DEPT_ALIASES: Dict[str, Department] = {
    "CIVIL": Department.CIVIL, "ENGINEERING": Department.CIVIL,
    "ENGG": Department.CIVIL, "PWAY": Department.CIVIL,
    "P-WAY": Department.CIVIL, "P.WAY": Department.CIVIL,
    "TRACK": Department.CIVIL, "TMS": Department.CIVIL,
    "SNT": Department.SNT, "S&T": Department.SNT,
    "SIGNAL": Department.SNT, "SIGNALLING": Department.SNT,
    "S&T-SIGNAL": Department.SNT, "TELECOM": Department.SNT,
    "SMMS": Department.SNT,
    "TRD": Department.TRD, "ELECTRICAL": Department.TRD,
    "ELECTRICAL_TRD": Department.TRD, "ELECTRICALS": Department.TRD,
    "OHE": Department.TRD, "TDMS": Department.TRD,
}

#: Silo running-line spellings are folded onto the canonical LRS line ids by
#: :func:`Backend.data_ingestion.lrs.coerce_line_id`, which is the single source
#: of truth for that mapping (``"DN FAST"`` / ``"UF"`` / ``"SLOW DN"`` ->
#: ``"DN_FAST"``). Keeping one table rather than two is what stops a contract
#: field and a solver field from ever disagreeing about which line a job is on.

SEVERITY_ALIASES: Dict[str, Severity] = {
    "CRITICAL": Severity.CRITICAL, "P1": Severity.CRITICAL,
    "P1_CRITICAL": Severity.CRITICAL, "HIGH": Severity.CRITICAL,
    "IMMEDIATE": Severity.CRITICAL, "IMR": Severity.CRITICAL,
    "URGENT": Severity.URGENT, "P2": Severity.URGENT,
    "P2_URGENT": Severity.URGENT, "MEDIUM": Severity.URGENT,
    "OBS": Severity.URGENT,
    "ROUTINE": Severity.ROUTINE, "P3": Severity.ROUTINE,
    "P3_ROUTINE": Severity.ROUTINE, "LOW": Severity.ROUTINE,
    "NORMAL": Severity.ROUTINE, "PLANNED": Severity.ROUTINE,
}

FORM_ALIASES: Dict[str, StatutoryForm] = {
    "T_351": StatutoryForm.T_351, "T/351": StatutoryForm.T_351,
    "T351": StatutoryForm.T_351, "FORM T/351": StatutoryForm.T_351,
    "FORM T-351": StatutoryForm.T_351, "IRSEM_22": StatutoryForm.IRSEM_22,
    "IRSEM 22": StatutoryForm.IRSEM_22,
    "T_352": StatutoryForm.T_352, "T/352": StatutoryForm.T_352,
    "T352": StatutoryForm.T_352, "FORM T/352": StatutoryForm.T_352,
    "ACTM_203": StatutoryForm.ACTM_203, "ACTM 203": StatutoryForm.ACTM_203,
    "ACTM203": StatutoryForm.ACTM_203, "ACTM_PARA_203": StatutoryForm.ACTM_203,
    "ACTM_204": StatutoryForm.ACTM_204, "ACTM 204": StatutoryForm.ACTM_204,
    "ACTM204": StatutoryForm.ACTM_204, "ACTM_PARA_204": StatutoryForm.ACTM_204,
    "IRPWM_268B": StatutoryForm.IRPWM_268B, "IRPWM 268B": StatutoryForm.IRPWM_268B,
    "IRPWM_284": StatutoryForm.IRPWM_284, "IRPWM 284": StatutoryForm.IRPWM_284,
    "": StatutoryForm.NONE, "NONE": StatutoryForm.NONE, "NA": StatutoryForm.NONE,
    "N/A": StatutoryForm.NONE, "NOT REQUIRED": StatutoryForm.NONE,
}


def _lookup(alias_map: Dict[str, Any], value: Any, label: str) -> Any:
    """Resolve a silo spelling against an alias map (already-valued passthrough)."""
    if isinstance(value, Enum):
        return value
    if value is None:
        raise ValueError(f"{label} is required")
    key = str(value).strip().upper().replace("  ", " ")
    if key in alias_map:
        return alias_map[key]
    raise ValueError(
        f"unrecognised {label} {value!r}; expected one of "
        f"{sorted({str(v.value) for v in alias_map.values()})}"
    )


def _to_minutes(value: Any) -> int:
    """Coerce CRIS duration encodings (``90``, ``'90'``, ``'1:30'``, ``'1h30'``)."""
    if isinstance(value, bool):
        raise ValueError("duration_mins must be a number, not a boolean")
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    text = str(value).strip()
    if not text:
        raise ValueError("duration_mins is empty")
    hhmm = _HHMM_RE.match(text)
    if hhmm:
        return int(hhmm.group(1)) * 60 + int(hhmm.group(2))
    hours = re.match(r"^(\d+(?:\.\d+)?)\s*h(?:rs?|ours?)?\s*(\d{1,2})?\s*(?:min|m)?$", text, re.I)
    if hours:
        total = float(hours.group(1)) * 60.0
        if hours.group(2):
            total += float(hours.group(2))
        return int(round(total))
    plain = _MINUTES_RE.match(text)
    if plain:
        return int(round(float(plain.group(1))))
    raise ValueError(f"unparseable duration_mins {value!r}")


# --------------------------------------------------------------------------- #
#  Display projection (read-only rendering payload)
# --------------------------------------------------------------------------- #
class DisplayProjection(BaseModel):
    """
    A kilometre post projected onto the map, for rendering only.

    This is the entire role of the 439-point waypoint table
    (:mod:`Backend.data_ingestion.waypoints`): turn
    ``(corridor_id, line_id, km)`` into something a map can draw. The object is
    deliberately inert - it carries no authority to decide whether a possession
    is legal, schedulable or in conflict. Those questions are answered in 1D by
    :func:`Backend.data_ingestion.lrs.has_overlap`.

    Axis order is explicit and there are two of them, because there are two
    consumers: ``[lat, lon]`` for the CesiumJS twin and the map components
    (:attr:`coordinates`), ``[lon, lat]`` for RFC 7946 GeoJSON
    (:attr:`geojson_coordinates`).
    """

    model_config = ConfigDict(extra="ignore")

    corridor_id: str = CORRIDOR_ID
    line_id: str = Field(default="UP_FAST", description="Running line this point was offset onto")
    km: float = Field(description="Statutory chainage in km from Churchgate")
    lon: float = Field(ge=-180.0, le=180.0)
    lat: float = Field(ge=-90.0, le=90.0)
    section: str
    station_code: str
    station_name: str
    station_km: float
    nearest_waypoint_id: str
    lateral_offset_m: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def coordinates(self) -> List[float]:
        """Display axis order ``[lat, lon]``."""
        return [round(self.lat, 6), round(self.lon, 6)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def geojson_coordinates(self) -> List[float]:
        """RFC 7946 GeoJSON axis order ``[lon, lat]``."""
        return [round(self.lon, 6), round(self.lat, 6)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def read_only(self) -> bool:
        """Always True: a projection may never feed a solver constraint."""
        return True

    def to_display_dict(self) -> Dict[str, Any]:
        """Minimal render payload handed to the frontend."""
        return {
            "corridor_id": self.corridor_id,
            "line_id": self.line_id,
            "km": self.km,
            "coordinates": self.coordinates,
            "section": self.section,
            "station_code": self.station_code,
            "nearest_waypoint_id": self.nearest_waypoint_id,
        }


# --------------------------------------------------------------------------- #
#  The unified contract
# --------------------------------------------------------------------------- #
class BlockRequisition(LinearSpan):
    """
    One maintenance block requisition, normalised from any CRIS silo.

    Inherits its primary spatial key from :class:`LinearSpan` -
    ``corridor_id``, ``line_id``, ``km_start``, ``km_end`` - so every
    requisition is natively a 1D linear-referencing span and two of them can be
    tested for physical collision without leaving the chainage domain.

    ``asset_id``, ``dept`` and ``aci`` complete the frozen five-field contract;
    the remaining fields carry the physical and statutory context the optimizer
    and the safety engine need.
    """

    model_config = ConfigDict(
        extra="allow",              # never lose a silo field; audit needs it
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )

    # ---- frozen contract -------------------------------------------------- #
    # ``corridor_id`` / ``line_id`` / ``km_start`` / ``km_end`` are inherited
    # from LinearSpan and are the primary spatial key; they are deliberately
    # declared in exactly one place.
    asset_id: str = Field(description="Unique asset/requisition id, e.g. TMS-ENG-1006")
    dept: Department = Field(
        validation_alias=AliasChoices("dept", "department", "directorate"),
        serialization_alias="dept",
        description="Owning directorate: CIVIL (TMS), SNT (SMMS), TRD (TDMS)",
    )
    aci: Optional[float] = Field(
        default=None, ge=0.0, le=100.0,
        description="Asset Criticality Index 0-100; None until the ACI engine scores it",
    )

    # ---- physical / operational ------------------------------------------- #
    duration_mins: int = Field(ge=MIN_DURATION_MINS, le=MAX_DURATION_MINS)
    urgency: Severity = Field(
        default=Severity.ROUTINE,
        validation_alias=AliasChoices("urgency", "severity", "priority"),
        serialization_alias="urgency",
    )

    # ---- provenance ------------------------------------------------------- #
    system: Optional[str] = Field(default=None, description="Source silo: TMS/SMMS/TDMS/COA")
    defect_type: Optional[str] = None
    description: Optional[str] = None
    defect_id: Optional[str] = Field(
        default=None,
        description="Kept for legacy consumers that key on defect_id instead of asset_id",
    )
    reported_date: Optional[str] = None
    status: str = "PENDING"

    # ---- statutory -------------------------------------------------------- #
    statutory_form: Optional[StatutoryForm] = None
    requires_traffic_block: bool = True
    requires_power_block: bool = False
    requires_disconnection: bool = Field(
        default=False,
        description="True when S&T gear must be proved disconnected (IRSEM Form T/351)",
    )
    requires_earthing_buffer: bool = Field(
        default=True,
        description="True when ACTM Para 204 15-minute discharge/earthing applies",
    )

    # ---- risk inputs consumed by the ACI engine --------------------------- #
    safety_weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    psr_speed_kmph: Optional[int] = Field(default=None, ge=0, le=160)
    days_overdue: int = Field(default=0, ge=0)
    target_completion_days: Optional[int] = Field(default=None, ge=1, le=90)
    gmt: Optional[float] = Field(default=None, ge=0.0)
    hotspot_temp_c: Optional[float] = None
    feeding_post: Optional[str] = None
    isolator: Optional[str] = None
    gear_id: Optional[str] = None

    # ---- derived at validation time --------------------------------------- #
    section: Optional[str] = None
    section_name: Optional[str] = None

    # ---- read-only display projection (never read by solver/invariants) ---- #
    display_start: Optional[DisplayProjection] = None
    display_end: Optional[DisplayProjection] = None

    # ------------------------------------------------------------------ #
    #  Field-level normalisation
    # ------------------------------------------------------------------ #
    @model_validator(mode="before")
    @classmethod
    def _absorb_contract_aliases(cls, data: Any) -> Any:
        """Map common silo spellings onto the canonical field names.

        Case-insensitive, additive and non-destructive: a raw CRIS dict may be
        handed straight to the contract, and any spelling the contract does not
        know is preserved as an extra field rather than dropped.
        """
        if not isinstance(data, Mapping):
            return data
        indexed = {str(key).strip().upper(): key for key in data}

        def locate(*names: str) -> Any:
            for name in names:
                original = indexed.get(name.strip().upper())
                if original is not None:
                    return original
            return None

        enriched: Dict[Any, Any] = dict(data)

        # 1. Re-case declared field names (KM_START, DURATION_MINS, ...).
        for field_name in cls.model_fields:
            original = locate(field_name)
            if original is not None and str(original) != field_name:
                enriched[field_name] = data[original]

        # 2. Resolve the non-canonical contract spellings (KM_FROM, TRACK, ...).
        for canonical, spellings in CONTRACT_ALIASES.items():
            if locate(canonical) is not None:
                continue
            source_key = locate(*spellings)
            if source_key is not None and data[source_key] is not None:
                enriched[canonical] = data[source_key]
        return enriched

    @field_validator("dept", mode="before")
    @classmethod
    def _normalise_dept(cls, value: Any) -> Any:
        return _lookup(DEPT_ALIASES, value, "department")

    @field_validator("urgency", mode="before")
    @classmethod
    def _normalise_urgency(cls, value: Any) -> Any:
        return _lookup(SEVERITY_ALIASES, value, "urgency")

    @field_validator("statutory_form", mode="before")
    @classmethod
    def _normalise_form(cls, value: Any) -> Any:
        if value is None:
            return None
        return _lookup(FORM_ALIASES, value, "statutory_form")

    @field_validator("duration_mins", mode="before")
    @classmethod
    def _normalise_duration(cls, value: Any) -> Any:
        return _to_minutes(value)

    @field_validator("asset_id", mode="before")
    @classmethod
    def _normalise_asset_id(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("asset_id is required")
        text = str(value).strip().upper().replace(" ", "")
        if not text:
            raise ValueError("asset_id is empty")
        if not _ASSET_ID_RE.match(text):
            raise ValueError(
                f"asset_id {value!r} is not a valid requisition key "
                "(expected e.g. TMS-ENG-1006)"
            )
        return text

    # NOTE: ``km_start`` / ``km_end`` coercion and the corridor bound are
    # inherited from LinearSpan (:func:`lrs.coerce_km`), so chainage is
    # quantised in exactly one place for every LRS-aware type in the system.

    @field_validator("hotspot_temp_c")
    @classmethod
    def _sane_hotspot(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if not -20.0 <= float(value) <= 400.0:
            raise ValueError(f"hotspot_temp_c {value} outside plausible range -20..400 C")
        return float(value)

    # ------------------------------------------------------------------ #
    #  Model-level invariants
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _enforce_possession_invariants(self) -> "BlockRequisition":
        # Chainage ordering and the corridor bound are enforced once, on the
        # inherited LinearSpan, so they cannot drift between the 1D contract and
        # any other LRS consumer. Only possession-specific rules live here.
        if self.km_end - self.km_start > MAX_POSSESSION_SPAN_KM:
            raise ValueError(
                f"span {round(self.km_end - self.km_start, 3)} km exceeds the "
                f"maximum single-possession length of {MAX_POSSESSION_SPAN_KM} km"
            )
        # NOTE: a power block requested without a traffic block is deliberately
        # NOT auto-corrected here. Silently repairing that contradiction would
        # hide a real data-entry error; instead the requisition is accepted and
        # flagged by safety.evaluate_safety_invariants (ACTM_203_POWER_ISOLATION)
        # so the block is held back before it reaches the simplex solver.

        # Derive the sectional bookmark from the start chainage. This is a
        # reporting / COA-matching convenience only: ``section`` plays no part
        # in deciding whether two possessions collide, which is a pure 1D
        # chainage question (see lrs.has_overlap) - two jobs in the same section
        # 8 km apart are not a conflict, and the old section-keyed check wrongly
        # said they were.
        if not self.section:
            section = waypoints.section_definition(waypoints.section_for_km(self.km_start))
            if section:
                self.section = str(section["code"])
                self.section_name = str(section["name"])
        return self

    # ------------------------------------------------------------------ #
    #  Derived properties
    # ------------------------------------------------------------------ #
    # ``span_km`` and ``lrs_key`` are inherited from LinearSpan.

    @property
    def line(self) -> Line:
        """
        The running line as a :class:`Line` member.

        Convenience only - ``line_id`` (a plain ``LineId`` string, inherited
        from LinearSpan) is the canonical field, exactly as it is stored by the
        CRIS silos.
        """
        return Line(self.line_id)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def priority_tier(self) -> str:
        """SLA tier, consistent with the ACI engine thresholds."""
        if self.urgency is Severity.CRITICAL:
            return "P1_CRITICAL"
        if self.urgency is Severity.URGENT:
            return "P2_URGENT"
        return "P3_ROUTINE"

    @property
    def power_isolation_required(self) -> bool:
        """
        True when the 25 kV OHE must be proved dead before work starts.

        Deliberately driven by the explicit flag only: the "every TDMS job needs
        a power block" default is applied at normalisation time, so an explicit
        ``POWER_BLOCK: N`` on an earth-continuity audit is honoured rather than
        overridden here.
        """
        return bool(self.requires_power_block)

    # ------------------------------------------------------------------ #
    #  Serialisation helpers
    # ------------------------------------------------------------------ #
    def project_display(self) -> "BlockRequisition":
        """
        Attach the read-only ``[lat, lon]`` display projection for both ends.

        Strictly an *enrichment* step: it reads ``km_start``/``km_end`` off the
        LRS span and writes two inert :class:`DisplayProjection` objects. The
        projection is one-way - nothing downstream may read a solver constraint
        or a safety invariant back off these coordinates.

        ``km_start``/``km_end`` are projected on the requisition's own running
        line, so a DN FAST job and a UP SLOW job at the same chainage render
        12 m apart on a quad corridor instead of on top of each other.
        """
        for label, km in (("display_start", self.km_start), ("display_end", self.km_end)):
            projected = waypoints.project_km(km, self.line_id)
            station = waypoints.station_record(str(projected["station_code"])) or {}
            setattr(
                self,
                label,
                DisplayProjection(
                    corridor_id=self.corridor_id,
                    line_id=self.line_id,
                    km=float(km),
                    lon=float(projected["lon"]),
                    lat=float(projected["lat"]),
                    lateral_offset_m=float(projected["lateral_offset_m"]),
                    section=str(projected["section"]),
                    station_code=str(projected["station_code"]),
                    station_name=str(station.get("name", projected["station_code"])),
                    station_km=float(projected["station_km"]),
                    nearest_waypoint_id=str(projected["nearest_waypoint_id"]),
                ),
            )
        return self

    #: Retained name for callers written against the pre-LRS ingestion layer.
    with_spatial_fix = project_display

    @property
    def geo_start(self) -> Optional[DisplayProjection]:
        """Deprecated read-only alias of :attr:`display_start`."""
        return self.display_start

    @property
    def geo_end(self) -> Optional[DisplayProjection]:
        """Deprecated read-only alias of :attr:`display_end`."""
        return self.display_end

    def to_display_payload(self) -> Dict[str, Any]:
        """
        The LRS identity plus its projected coordinates, for the frontend.

        This is the shape a map, a Cesium polygon or a sidebar consumes. It is
        explicitly marked ``projection_only`` so no caller mistakes it for a
        scheduling input.
        """
        return {
            **self.to_lrs_dict(),
            "asset_id": self.asset_id,
            "display_start": self.display_start.to_display_dict() if self.display_start else None,
            "display_end": self.display_end.to_display_dict() if self.display_end else None,
            "projection_only": True,
        }

    def to_contract_dict(self) -> Dict[str, Any]:
        """The frozen five fields, the LRS key, and provenance as a plain dict."""
        payload = self.model_dump(mode="json")
        return {key: payload.get(key) for key in CONTRACT_FIELDS} | {
            **self.to_lrs_dict(),
            "urgency": self.urgency.value,
            "duration_mins": self.duration_mins,
            "system": self.system,
            "statutory_form": self.statutory_form.value if self.statutory_form else None,
            "section": self.section,
        }

    def to_legacy_dict(self) -> Dict[str, Any]:
        """
        Contract -> the dict shape the existing ACI/optimizer pipeline consumes.

        Bridges the ingestion layer onto :meth:`IntegratedBlockOptimizer.
        optimize_blocks` without touching that engine: ``dept`` -> ``department``,
        ``line_id`` -> ``track_id``, ``asset_id`` -> ``defect_id``.

        The LRS key travels explicitly (``corridor_id``/``line_id``/``km_start``/
        ``km_end``) so the solver can group and test collisions purely in 1D;
        ``section`` and ``track_id`` survive only because the legacy optimizer
        and the COA disruption model still key their reporting on them.

        Three durations travel with every row so the corridor footprint can
        never be misread:

        * ``duration_mins`` - working duration (the existing optimizer's field).
        * ``possession_duration_mins`` - ``work + 2 x 15 min`` ACTM Para 204
          earthing buffer when live OHE work applies; this is the real time the
          section is blocked.
        * ``work_duration_mins`` / ``earthing_buffer_mins`` - the arithmetic
          behind it, so the safety guardrail can re-derive the envelope instead
          of trusting an opaque total.
        """
        buffer_mins = (
            ACTM_PARA_204_EARTHING_BUFFER_MINS
            if (self.requires_earthing_buffer and self.power_isolation_required)
            else 0
        )
        payload: Dict[str, Any] = {
            "defect_id": self.asset_id,
            "asset_id": self.asset_id,
            "department": self.dept.value,
            "dept": self.dept.value,
            "system": self.system,
            "corridor_id": self.corridor_id,
            "line_id": self.line_id,
            "lrs_key": list(self.lrs_key),
            "section": self.section,
            "section_name": self.section_name,
            "track_id": self.line_id,
            "line": self.line_id,
            "km_start": self.km_start,
            "km_end": self.km_end,
            "span_km": self.span_km,
            "defect_type": self.defect_type,
            "description": self.description,
            "severity": self.urgency.value,
            "urgency": self.urgency.value,
            "priority_tier": self.priority_tier,
            "duration_mins": self.duration_mins,
            "work_duration_mins": self.duration_mins,
            "earthing_buffer_mins": buffer_mins,
            "possession_duration_mins": self.duration_mins + 2 * buffer_mins,
            "safety_weight": self.safety_weight if self.safety_weight is not None else 0.5,
            "psr_speed_kmph": self.psr_speed_kmph,
            "days_overdue": self.days_overdue,
            "target_completion_days": self.target_completion_days or 7,
            "gmt": self.gmt if self.gmt is not None else (waypoints.section_definition(self.section or "") or {}).get("gmt", 65),
            "requires_traffic_block": self.requires_traffic_block,
            "requires_power_block": self.requires_power_block,
            "requires_disconnection": self.requires_disconnection,
            "statutory_form": self.statutory_form.value if self.statutory_form else None,
            "status": self.status,
            "reported_date": self.reported_date,
        }
        if self.aci is not None:
            payload["aci_score"] = self.aci
        if self.hotspot_temp_c is not None:
            payload["hotspot_temp_c"] = self.hotspot_temp_c
        if self.feeding_post:
            payload["feeding_post"] = self.feeding_post
        if self.gear_id:
            payload["gear_id"] = self.gear_id
        for label in ("display_start", "display_end"):
            projection = getattr(self, label, None)
            if projection is not None:
                payload[label] = projection.model_dump(mode="json")
        return payload


# --------------------------------------------------------------------------- #
#  Feed + ingestion report models
# --------------------------------------------------------------------------- #
class RawFeed(BaseModel):
    """A raw CRIS payload as received, before per-directorate normalisation."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    feed_id: Optional[str] = None
    source: Optional[str] = Field(default=None, description="TMS / SMMS / TDMS / COA")
    corridor: Optional[str] = "Churchgate - Virar"
    generated_at: Optional[str] = None
    requisitions: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("requisitions", mode="before")
    @classmethod
    def _wrap_single(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value


class RejectedRequisition(BaseModel):
    """A payload that failed the contract, with the reason kept for the jury log."""

    model_config = ConfigDict(extra="allow")

    index: int
    source: Optional[str] = None
    asset_id: Optional[str] = None
    dept: Optional[str] = None
    #: LRS identity as far as it could be recovered from the raw payload, so a
    #: quarantine report can be mapped and filtered the same way as an accepted
    #: one.
    corridor_id: str = CORRIDOR_ID
    line_id: Optional[str] = None
    km_start: Optional[Any] = None
    km_end: Optional[Any] = None
    errors: List[str] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)


class IngestReport(BaseModel):
    """Result of ingesting one feed: what passed, what was rejected, and why."""

    model_config = ConfigDict(extra="allow")

    feed_id: Optional[str] = None
    source: Optional[str] = None
    corridor_id: str = CORRIDOR_ID
    corridor: str = "Churchgate - Virar"
    received: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    acceptance_pct: float = 0.0
    accepted: List[BlockRequisition] = Field(default_factory=list)
    rejected: List[RejectedRequisition] = Field(default_factory=list)
    safety: List[Dict[str, Any]] = Field(default_factory=list)
    geojson: Optional[Dict[str, Any]] = None
    warnings: List[str] = Field(default_factory=list)

    def legacy_requisitions(self) -> List[Dict[str, Any]]:
        """Accepted requisitions shaped for the HiGHS optimizer / ACI engine."""
        return [req.to_legacy_dict() for req in self.accepted]

    def display_payloads(self) -> List[Dict[str, Any]]:
        """
        Read-only rendering payloads for the frontend.

        LRS identity plus projected ``[lat, lon]`` coordinates. Handing these to
        a map is the *only* sanctioned use of the waypoint projection table.
        """
        return [req.to_display_payload() for req in self.accepted]

    def linear_spans(self) -> List[Dict[str, Any]]:
        """The 1D spans the solver reasons about, one per accepted requisition."""
        return [req.to_lrs_dict() for req in self.accepted]

    def chainage_conflict_pairs(self) -> List[Tuple[int, int, float]]:
        """
        Candidate pairs whose 1D chainage intervals collide on the same line.

        Overlap at the *requisition* stage is expected and is exactly what the
        joint bundler collapses into a single possession - so this is exposed as
        an accessor rather than raised as a warning. It lets the bundling step
        and the UI read the 1D relation directly instead of inferring proximity
        from section codes, which is the coupling the LRS refactor removes.
        """
        return find_collisions(self.linear_spans())

    def summary(self) -> Dict[str, Any]:
        """Compact status block for API responses and the execution trace."""
        by_dept: Dict[str, int] = {}
        by_line: Dict[str, int] = {}
        for req in self.accepted:
            by_dept[req.dept.value] = by_dept.get(req.dept.value, 0) + 1
            by_line[req.line_id] = by_line.get(req.line_id, 0) + 1
        return {
            "feed_id": self.feed_id,
            "source": self.source,
            "corridor_id": self.corridor_id,
            "received": self.received,
            "accepted": self.accepted_count,
            "rejected": self.rejected_count,
            "acceptance_pct": self.acceptance_pct,
            "by_department": by_dept,
            "by_line": by_line,
            "safety_passed": sum(1 for verdict in self.safety if verdict.get("passed")),
            "safety_failed": sum(1 for verdict in self.safety if not verdict.get("passed")),
            "warnings": self.warnings,
        }


#: Backwards-compatible alias. The pre-LRS ingestion layer called the display
#: payload a "spatial fix"; it is now explicitly a projection, and nothing that
#: decides a schedule reads it.
SpatialFix = DisplayProjection


__all__ = [
    "CONTRACT_FIELDS",
    "LRS_FIELDS",
    "CORRIDOR_ID",
    "CORRIDOR_LENGTH_KM",
    "LINE_IDS",
    "MIN_DURATION_MINS",
    "MAX_DURATION_MINS",
    "MAX_POSSESSION_SPAN_KM",
    "Department",
    "Line",
    "LineId",
    "LinearSpan",
    "Severity",
    "StatutoryForm",
    "DisplayProjection",
    "SpatialFix",
    "BlockRequisition",
    "RawFeed",
    "RejectedRequisition",
    "IngestReport",
]
