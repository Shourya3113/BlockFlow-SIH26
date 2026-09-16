"""
schema.py - BlockFlow Unified Ingestion Contract (Pydantic v2)

The **day-1 frozen contract** that lets three siloed CRIS directorates speak one
language. Every requisition that reaches the HiGHS optimizer - whether it came
from TMS (Civil/P-Way), SMMS (Signal & Telecom) or TDMS (Traction Distribution)
- must first become a :class:`BlockRequisition`.

Core contract (frozen)
----------------------
``[asset_id, dept, km_start, km_end, aci]``

Everything else is either provenance (which silo sent it), physical attributes
(duration, whether a 25 kV power block is needed) or enrichment (the snapped
WGS84 fix, the derived section). Extra keys from a legacy silo are *kept*, never
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
"""

from __future__ import annotations

import re
import math
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
from .safety import ACTM_PARA_204_EARTHING_BUFFER_MINS

#: The frozen five-field contract other modules may rely on.
CONTRACT_FIELDS: List[str] = ["asset_id", "dept", "km_start", "km_end", "aci"]

#: Minimum silo spellings the contract itself absorbs (matched case-insensitively)
#: so the schema stays usable standalone - by the FastAPI gateway, the optimizer
#: or a notebook - without importing the full pipeline alias table.
#: The long tail of CRIS spellings lives in ``pipeline.FIELD_ALIASES``.
CONTRACT_ALIASES: Dict[str, Tuple[str, ...]] = {
    "asset_id": ("requisition_no", "req_no", "assetid"),
    "dept": ("department", "directorate", "wing"),
    "km_start": ("km_from", "from_km"),
    "km_end": ("km_to", "to_km"),
    "line": ("track", "track_id", "running_line"),
    "duration_mins": ("duration", "minutes"),
    "urgency": ("severity", "priority"),
    "statutory_form": ("form",),
    "aci": ("aci_score",),
}

#: Statutory corridor bounds (km), sourced from the 439-point waypoint database.
CORRIDOR_LENGTH_KM = waypoints.CORRIDOR_LENGTH_KM

#: Minimum permitted duration for any on-track possession (minutes).
MIN_DURATION_MINS = 15
#: Practical maximum single possession (minutes) - a single block window.
MAX_DURATION_MINS = 480

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
    """One of the four running lines of the Mumbai suburban quad corridor."""

    UP_FAST = "UP_FAST"
    UP_SLOW = "UP_SLOW"
    DN_SLOW = "DN_SLOW"
    DN_FAST = "DN_FAST"


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

LINE_ALIASES: Dict[str, Line] = {
    "UP_FAST": Line.UP_FAST, "UP FAST": Line.UP_FAST, "UPFAST": Line.UP_FAST,
    "UP-FAST": Line.UP_FAST, "FAST UP": Line.UP_FAST, "UF": Line.UP_FAST,
    "UP_SLOW": Line.UP_SLOW, "UP SLOW": Line.UP_SLOW, "UPSLOW": Line.UP_SLOW,
    "UP-SLOW": Line.UP_SLOW, "SLOW UP": Line.UP_SLOW, "US": Line.UP_SLOW,
    "UP": Line.UP_FAST, "UP LINE": Line.UP_FAST,
    "DN_SLOW": Line.DN_SLOW, "DN SLOW": Line.DN_SLOW, "DNSLOW": Line.DN_SLOW,
    "DN-SLOW": Line.DN_SLOW, "SLOW DN": Line.DN_SLOW, "DS": Line.DN_SLOW,
    "DN_FAST": Line.DN_FAST, "DN FAST": Line.DN_FAST, "DNFAST": Line.DN_FAST,
    "DN-FAST": Line.DN_FAST, "FAST DN": Line.DN_FAST, "DF": Line.DN_FAST,
    "DN": Line.DN_FAST, "DN LINE": Line.DN_FAST,
}

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
#  Spatial fix
# --------------------------------------------------------------------------- #
class SpatialFix(BaseModel):
    """A chainage snapped onto the 439-point WGS84 waypoint database."""

    model_config = ConfigDict(extra="ignore")

    km: float = Field(description="Statutory chainage in km from Churchgate")
    lon: float = Field(ge=-180.0, le=180.0)
    lat: float = Field(ge=-90.0, le=90.0)
    section: str
    station_code: str
    station_name: str
    station_km: float
    nearest_waypoint_id: str
    lateral_offset_m: float = 0.0
    line: Optional[Line] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def coordinates(self) -> List[float]:
        """GeoJSON axis order ``[lon, lat]``."""
        return [round(self.lon, 6), round(self.lat, 6)]


# --------------------------------------------------------------------------- #
#  The unified contract
# --------------------------------------------------------------------------- #
class BlockRequisition(BaseModel):
    """
    One maintenance block requisition, normalised from any CRIS silo.

    Only ``asset_id``, ``dept``, ``km_start``, ``km_end`` and ``aci`` are part
    of the frozen contract; the remaining fields carry the physical and
    statutory context the optimizer and safety engine need.
    """

    model_config = ConfigDict(
        extra="allow",              # never lose a silo field; audit needs it
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )

    # ---- frozen contract -------------------------------------------------- #
    asset_id: str = Field(description="Unique asset/requisition id, e.g. TMS-ENG-1006")
    dept: Department = Field(
        validation_alias=AliasChoices("dept", "department", "directorate"),
        serialization_alias="dept",
        description="Owning directorate: CIVIL (TMS), SNT (SMMS), TRD (TDMS)",
    )
    km_start: float = Field(description="Start chainage, km from Churchgate (0.00)")
    km_end: float = Field(description="End chainage, km from Churchgate (59.98 max)")
    aci: Optional[float] = Field(
        default=None, ge=0.0, le=100.0,
        description="Asset Criticality Index 0-100; None until the ACI engine scores it",
    )

    # ---- physical / operational ------------------------------------------- #
    line: Line = Field(
        validation_alias=AliasChoices("line", "track_id", "track", "running_line"),
        serialization_alias="line",
    )
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
    geo_start: Optional[SpatialFix] = None
    geo_end: Optional[SpatialFix] = None

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

    @field_validator("line", mode="before")
    @classmethod
    def _normalise_line(cls, value: Any) -> Any:
        return _lookup(LINE_ALIASES, value, "line")

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

    @field_validator("km_start", "km_end", mode="before")
    @classmethod
    def _normalise_chainage(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("chainage is required")
        if isinstance(value, bool):
            raise ValueError("chainage must be numeric, not a boolean")
        text = str(value).strip().replace("KM", "").replace("km", "").replace(",", "").strip()
        if "M" in text.upper() and "KM" not in text.upper():
            # bare metre figures (e.g. "20400 M") are a common CRIS export quirk
            try:
                return round(float(re.sub(r"[^0-9.\-]", "", text)) / 1000.0, 6)
            except ValueError:
                pass
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"chainage {value!r} is not a number") from exc
        if math.isnan(number) or math.isinf(number):
            raise ValueError(f"chainage {value!r} is not finite")
        return round(number, 6)

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
    def _enforce_corridor_invariants(self) -> "BlockRequisition":
        if self.km_start > self.km_end:
            raise ValueError(
                f"km_start ({self.km_start}) is greater than km_end ({self.km_end}); "
                "chainage must increase away from Churchgate"
            )
        for label, km in (("km_start", self.km_start), ("km_end", self.km_end)):
            if not waypoints.validate_chainage(km):
                raise ValueError(
                    f"{label}={km} km is outside the Churchgate-Virar corridor "
                    f"(0.000 - {CORRIDOR_LENGTH_KM} km)"
                )
        if self.km_end - self.km_start > 19.98:
            raise ValueError(
                f"span {round(self.km_end - self.km_start, 3)} km exceeds the "
                "maximum single-possession length of 19.98 km"
            )
        # NOTE: a power block requested without a traffic block is deliberately
        # NOT auto-corrected here. Silently repairing that contradiction would
        # hide a real data-entry error; instead the requisition is accepted and
        # flagged by safety.evaluate_safety_invariants (ACTM_203_POWER_ISOLATION)
        # so the block is held back before it reaches the simplex solver.

        # Derive the sectional boundary from the snapped start chainage.
        if not self.section:
            section = waypoints.section_definition(waypoints.section_for_km(self.km_start))
            if section:
                self.section = str(section["code"])
                self.section_name = str(section["name"])
        return self

    # ------------------------------------------------------------------ #
    #  Derived properties
    # ------------------------------------------------------------------ #
    @computed_field  # type: ignore[prop-decorator]
    @property
    def span_km(self) -> float:
        """Length of the possession in kilometers."""
        return round(self.km_end - self.km_start, 3)

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
    def with_spatial_fix(self) -> "BlockRequisition":
        """
        Attach snapped WGS84 fixes for both ends of the possession.

        ``km_start``/``km_end`` are snapped on the defect's own running line, so
        a DN FAST defect and a UP SLOW defect at the same chainage do not
        overlap when the 3D twin extrudes the block volume.
        """
        for label, km in (("geo_start", self.km_start), ("geo_end", self.km_end)):
            fix = waypoints.waypoint_at_km(km)
            track = waypoints.track_geometry(km, self.line.value)
            station = waypoints.station_record(str(fix["station_code"])) or {}
            setattr(
                self,
                label,
                SpatialFix(
                    km=float(km),
                    lon=track["lon"],
                    lat=track["lat"],
                    lateral_offset_m=track["lateral_offset_m"],
                    section=str(fix["section"]),
                    station_code=str(fix["station_code"]),
                    station_name=str(station.get("name", fix["station_code"])),
                    station_km=float(fix["station_km"]),
                    nearest_waypoint_id=str(fix["nearest_waypoint_id"]),
                    line=self.line,
                ),
            )
        return self

    def to_contract_dict(self) -> Dict[str, Any]:
        """The frozen five fields (plus provenance) as a plain dict."""
        payload = self.model_dump(mode="json")
        return {key: payload.get(key) for key in CONTRACT_FIELDS} | {
            "line": self.line.value,
            "urgency": self.urgency.value,
            "duration_mins": self.duration_mins,
            "system": self.system,
            "statutory_form": self.statutory_form.value if self.statutory_form else None,
            "section": self.section,
            "span_km": self.span_km,
        }

    def to_legacy_dict(self) -> Dict[str, Any]:
        """
        Contract -> the dict shape the existing ACI/optimizer pipeline consumes.

        Bridges the new ingestion layer onto :meth:`IntegratedBlockOptimizer.
        optimize_blocks` without touching that engine: ``dept`` -> ``department``,
        ``line`` -> ``track_id``, ``asset_id`` -> ``defect_id``.

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
            "section": self.section,
            "section_name": self.section_name,
            "track_id": self.line.value,
            "line": self.line.value,
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
        for label in ("geo_start", "geo_end"):
            fix = getattr(self, label, None)
            if fix is not None:
                payload[label] = fix.model_dump(mode="json")
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
    km_start: Optional[Any] = None
    km_end: Optional[Any] = None
    errors: List[str] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)


class IngestReport(BaseModel):
    """Result of ingesting one feed: what passed, what was rejected, and why."""

    model_config = ConfigDict(extra="allow")

    feed_id: Optional[str] = None
    source: Optional[str] = None
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

    def summary(self) -> Dict[str, Any]:
        """Compact status block for API responses and the execution trace."""
        by_dept: Dict[str, int] = {}
        for req in self.accepted:
            by_dept[req.dept.value] = by_dept.get(req.dept.value, 0) + 1
        return {
            "feed_id": self.feed_id,
            "source": self.source,
            "received": self.received,
            "accepted": self.accepted_count,
            "rejected": self.rejected_count,
            "acceptance_pct": self.acceptance_pct,
            "by_department": by_dept,
            "safety_passed": sum(1 for verdict in self.safety if verdict.get("passed")),
            "safety_failed": sum(1 for verdict in self.safety if not verdict.get("passed")),
            "warnings": self.warnings,
        }


__all__ = [
    "CONTRACT_FIELDS",
    "CORRIDOR_LENGTH_KM",
    "MIN_DURATION_MINS",
    "MAX_DURATION_MINS",
    "Department",
    "Line",
    "Severity",
    "StatutoryForm",
    "SpatialFix",
    "BlockRequisition",
    "RawFeed",
    "RejectedRequisition",
    "IngestReport",
]
