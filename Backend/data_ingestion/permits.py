"""
permits.py - Downstream Statutory Permit Generation (Outputs, not Inputs)

BlockFlow plans **prospectively**. Field units submit engineering demands - a
USFD rail flaw, a point-machine obstruction test, a 25 kV thermovision hotspot -
and the optimizer decides *when* that work can occupy the track. Only then can
the statutory paperwork exist.

That ordering is the whole design of this module:

    engineering requisition            (what work, where, how long, how urgent)
                |
                v
        HiGHS block allocation          (which window on which day)
                |
                v
    DigitalGrantPermit + memos          <-- IRSEM Form T/351, T/352, ACTM PTW
                                            prefilled from the assigned window

Why memos cannot be inputs
--------------------------
IRSEM Form T/351 (Disconnection Notice), Form T/352 (Reconnection Notice) and
the ACTM Permit-to-Work are **legal instruments**, not data fields. In real
operation they are issued by a Sectional Controller, a Signal Maintainer, a
Station Master or a Traction Power Controller *after* a block has been scheduled
and authorised. A raw requisition arriving on Monday morning for work planned
next month cannot cite a T/351 that does not yet exist - so validating inputs
against a ``statutory_form`` field manufactures rejections for conforming data
and hides the real question, which is whether the *engineering* description is
complete enough to raise the paperwork later.

This module therefore:
1.  **Derives** which instruments a block will need, from engineering attributes
    (work type / department) plus the declared operating context.
2.  **Prefills** each instrument from the allocated window: the granted
    ``window_start``/``window_end``, the ACTM Para 204 earthing wrap, the
    affected signalling gears, and the authorities who must sign.
3.  **Never fabricates a signature.** Every authorization is a ``PENDING``
    placeholder with the correct authority and designation. The optimizer
    proposes; a human signs. :class:`AuthorizationSignature` refuses to be
    ``SIGNED`` without a name and a timestamp.

Statutory basis
---------------
*   **ACTM Vol II Para 203** - traction power isolation, taken from the TPC.
*   **ACTM Vol II Para 204** - mandatory ``15 min`` discharge/earthing buffer
    both **before and after** live OHE work, so a possession is
    ``work + 15 + 15`` minutes and is earthed throughout.
*   **IRSEM Para 22** - Form T/351 before gear disconnection, Form T/352 after
    the gear is proved back in.
*   **IRPWM Para 284 / 268(b)** - caution order for P-Way work under traffic,
    and a permanent speed restriction inside the permissible section speed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from .lrs import (
    CORRIDOR_ID,
    CORRIDOR_LENGTH_KM,
    coerce_km,
    coerce_line_id,
    corridor_id_of,
    km_end_of,
    km_start_of,
    line_id_of,
)

# --------------------------------------------------------------------------- #
#  Statutory constants (owned here: this is the module that enforces them)
# --------------------------------------------------------------------------- #
#: ACTM Vol II Para 204 - earthing / discharge buffer, minutes, each side.
ACTM_PARA_204_EARTHING_BUFFER_MINS = 15

#: ACTM Vol II Para 203 - traction power isolation reference.
ACTM_PARA_203_REFERENCE = "ACTM Vol II Para 203 (Traction Power Controller isolation)"

#: ACTM Vol II Para 204 - the earthing buffer reference.
ACTM_PARA_204_REFERENCE = "ACTM Vol II Para 204 (15-minute discharge/earthing buffer)"

#: IRSEM Para 22 - disconnection / reconnection paperwork.
IRSEM_PARA_22_REFERENCE = "IRSEM Para 22 (Form T/351 disconnection, T/352 reconnection)"

#: IRPWM Para 284 - P-Way work under traffic protection.
IRPWM_PARA_284_REFERENCE = "IRPWM Para 284 (P-Way work under traffic protection)"

#: IRPWM Para 268(b) - speed restriction must be sanctioned and inside line speed.
IRPWM_PARA_268B_REFERENCE = "IRPWM Para 268(b) (caution order and speed restriction)"

#: Permissible speed of the Mumbai suburban quad section (km/h).
TRACK_PERMISSIBLE_SPEED_KMPH = 110

#: Legacy code names for the two IRSEM forms, kept as wire values.
IRSEM_DISCONNECTION_FORM = "T_351"
IRSEM_RECONNECTION_FORM = "T_352"


# --------------------------------------------------------------------------- #
#  Enumerations
# --------------------------------------------------------------------------- #
class FormCode(str, Enum):
    """
    Every statutory reference BlockFlow can name.

    Split into *issuable documents* (a permit object exists for them, see
    :data:`ISSUABLE_FORMS`) and mere *references* an incoming silo row might
    cite. A cited reference is provenance; it is never a validation gate.
    """

    T_351 = "T_351"                        # IRSEM - disconnection notice (issuable)
    T_352 = "T_352"                        # IRSEM - reconnection notice (issuable)
    ACTM_PTW = "ACTM_PTW"                  # ACTM permit-to-work (issuable)
    IRPWM_284_CAUTION = "IRPWM_284_CAUTION"  # caution order (issuable)
    ACTM_203 = "ACTM_203"                  # TPC isolation reference
    ACTM_204 = "ACTM_204"                  # earthing buffer reference
    IRPWM_268B = "IRPWM_268B"              # speed restriction reference
    IRPWM_284 = "IRPWM_284"                # P-Way under traffic reference
    IRSEM_22 = "IRSEM_22"                  # IRSEM Para 22 reference
    STRATEGIC_NONE = "NONE"                # explicitly "no paperwork cited"


#: Form codes for which this module can generate a permit document.
ISSUABLE_FORMS: Tuple[FormCode, ...] = (
    FormCode.T_351,
    FormCode.T_352,
    FormCode.ACTM_PTW,
    FormCode.IRPWM_284_CAUTION,
)

#: Silo spellings -> canonical form code. Used only to *record provenance* on an
#: incoming requisition (``referenced_form``); it never gates ingestion.
FORM_CODE_ALIASES: Dict[str, FormCode] = {
    "T_351": FormCode.T_351, "T/351": FormCode.T_351,
    "T351": FormCode.T_351, "FORM T/351": FormCode.T_351,
    "FORM T-351": FormCode.T_351, "T 351": FormCode.T_351,
    "T_352": FormCode.T_352, "T/352": FormCode.T_352,
    "T352": FormCode.T_352, "FORM T/352": FormCode.T_352,
    "FORM T-352": FormCode.T_352, "T 352": FormCode.T_352,
    "ACTM_PTW": FormCode.ACTM_PTW, "ACTM PTW": FormCode.ACTM_PTW,
    "PTW": FormCode.ACTM_PTW, "PERMIT TO WORK": FormCode.ACTM_PTW,
    "ACTM_PARA_203": FormCode.ACTM_203, "ACTM_203": FormCode.ACTM_203,
    "ACTM 203": FormCode.ACTM_203, "ACTM203": FormCode.ACTM_203,
    "ACTM_PARA_204": FormCode.ACTM_204, "ACTM_204": FormCode.ACTM_204,
    "ACTM 204": FormCode.ACTM_204, "ACTM204": FormCode.ACTM_204,
    "IRPWM_268B": FormCode.IRPWM_268B, "IRPWM 268B": FormCode.IRPWM_268B,
    "IRPWM_284": FormCode.IRPWM_284, "IRPWM 284": FormCode.IRPWM_284,
    "IRPWM_284_CAUTION": FormCode.IRPWM_284_CAUTION,
    "IRSEM_22": FormCode.IRSEM_22, "IRSEM 22": FormCode.IRSEM_22,
    "T_351_REQUIRED": FormCode.T_351,
    "": FormCode.STRATEGIC_NONE, "NONE": FormCode.STRATEGIC_NONE,
    "NA": FormCode.STRATEGIC_NONE, "N/A": FormCode.STRATEGIC_NONE,
    "NOT REQUIRED": FormCode.STRATEGIC_NONE, "NIL": FormCode.STRATEGIC_NONE,
}


def coerce_form_code(value: Any) -> Optional[FormCode]:
    """Normalise a cited statutory reference; ``None`` when nothing was cited."""
    if value is None:
        return None
    if isinstance(value, FormCode):
        return value
    key = re.sub(r"\s+", " ", str(value).strip().upper())
    if not key:
        return None
    code = FORM_CODE_ALIASES.get(key)
    if code is None:
        return None
    return None if code is FormCode.STRATEGIC_NONE else code


class PermitStatus(str, Enum):
    """Lifecycle of a generated instrument."""

    PREFILLED = "PREFILLED"        # generated from a scheduled block, awaiting signature
    ISSUED = "ISSUED"              # signed and released by the cognisant authority
    ACKNOWLEDGED = "ACKNOWLEDGED"  # field unit has confirmed receipt
    CANCELLED = "CANCELLED"        # block withdrawn before the instrument took effect


class Authority(str, Enum):
    """Who is legally competent to sign a given instrument."""

    SECTION_CONTROLLER = "SECTION_CONTROLLER"                  # T/351, T/352, caution order
    TRACTION_POWER_CONTROLLER = "TRACTION_POWER_CONTROLLER"    # ACTM PTW
    STATION_MASTER = "STATION_MASTER"                          # caution order notice
    SECTION_ENGINEER = "SECTION_ENGINEER"                      # P-Way work supervision


class SignatureStatus(str, Enum):
    """Signature state - a machine may only ever set ``PENDING``."""

    PENDING = "PENDING"
    SIGNED = "SIGNED"
    DECLINED = "DECLINED"


class GearType(str, Enum):
    """Class of signalling / interlocking equipment affected by the work."""

    POINT_MACHINE = "POINT_MACHINE"
    AXLE_COUNTER = "AXLE_COUNTER"
    TRACK_CIRCUIT = "TRACK_CIRCUIT"
    LEVEL_CROSSING = "LEVEL_CROSSING"
    ELECTRONIC_INTERLOCKING = "ELECTRONIC_INTERLOCKING"
    SIGNAL = "SIGNAL"
    UNKNOWN = "UNKNOWN"


class GearAction(str, Enum):
    """What happens to that gear during the possession."""

    DISCONNECT = "DISCONNECT"
    RECONNECT = "RECONNECT"
    CLAMP = "CLAMP"
    PROVE = "PROVE"


#: Gear-id prefixes / tokens seen in SMMS exports -> equipment class.
_GEAR_TYPE_TOKENS: Tuple[Tuple[Tuple[str, ...], GearType], ...] = (
    (("PT", "POINT", "PM"), GearType.POINT_MACHINE),
    (("MSDAC", "AXLE", "ACS", "WHEEL"), GearType.AXLE_COUNTER),
    (("AFTC", "TC", "TRACK_CIRCUIT", "BOND"), GearType.TRACK_CIRCUIT),
    (("LC", "GATE", "LEVEL_CROSSING"), GearType.LEVEL_CROSSING),
    (("EI", "IL", "INTERLOCK", "RRI", "PANEL"), GearType.ELECTRONIC_INTERLOCKING),
    (("SIG", "SIGNAL", "LED"), GearType.SIGNAL),
)

#: Work-type token families -> equipment class, so a gear can be classified even
#: when the silo sends only a work type.
_WORK_TYPE_GEAR_TOKENS: Tuple[Tuple[Tuple[str, ...], GearType], ...] = (
    (("POINT_MACHINE",), GearType.POINT_MACHINE),
    (("AXLE_COUNTER", "MSDAC"), GearType.AXLE_COUNTER),
    (("TRACK_CIRCUIT", "AFTC"), GearType.TRACK_CIRCUIT),
    (("LC_GATE", "LEVEL_CROSSING"), GearType.LEVEL_CROSSING),
    (("INTERLOCKING",), GearType.ELECTRONIC_INTERLOCKING),
    (("SIGNAL_ASPECT", "SIGNAL"), GearType.SIGNAL),
)


def classify_gear(gear_id: Optional[str], work_type: Optional[str] = None) -> GearType:
    """Infer the equipment class of a gear id, falling back to its work type."""
    for source, table in ((gear_id, _GEAR_TYPE_TOKENS), (work_type, _WORK_TYPE_GEAR_TOKENS)):
        if not source:
            continue
        token = re.sub(r"[^A-Z0-9]+", "_", str(source).strip().upper())
        for needles, gear_type in table:
            if any(re.search(rf"(^|_){re.escape(n)}($|_)", token) for n in needles):
                return gear_type
    return GearType.UNKNOWN


# --------------------------------------------------------------------------- #
#  Work-type -> statutory profile
# --------------------------------------------------------------------------- #
class WorkProfile(BaseModel):
    """
    Which statutory instruments a class of work pulls in.

    Deliberately narrow. It answers only the two questions the silos do **not**
    reliably answer for us:

    * ``disconnection`` - does this work put signalling gear out of service
      (Form T/351 + T/352)?
    * ``caution_order`` - is this P-Way work under traffic (IRPWM Para 284)?

    Traction power isolation is *not* modelled here. It is already an explicit
    input flag with its own ACTM Para 203 invariant, and the TDMS silo defaults
    it on; second-guessing it from a work-type string would let a lookup table
    override a declared duty-sheet answer.
    """

    model_config = ConfigDict(extra="ignore")

    disconnection: bool = False
    caution_order: bool = False


_NO_PROFILE = WorkProfile()

#: Engineering work type -> statutory profile. Matched on the exact token first,
#: then on a family prefix, so an unseen variant of a known job still classifies.
#:
#: Scope note: only the instruments a work type **provably** attracts are listed.
#: Civil track work under traffic raises a caution order; S&T work that opens a
#: circuit raises a disconnection notice. Traction work raises nothing here - its
#: isolation is already a declared flag with its own ACTM Para 203 invariant.
#: Inventing a document "because it might be needed" is the same mistake as
#: demanding one on the way in.
WORK_PROFILES: Dict[str, WorkProfile] = {
    # ---- Civil / P-Way (TMS) - track work under traffic -> caution order --- #
    "USFD_IMR_WELD": WorkProfile(caution_order=True),
    "USFD_OBS_FLAW": WorkProfile(caution_order=True),
    "TRACK_TAMPING": WorkProfile(caution_order=True),
    "TURNOUT_RENEWAL": WorkProfile(caution_order=True),
    "RAIL_DESTRESSING": WorkProfile(caution_order=True),
    "DEEP_SCREENING_BCM": WorkProfile(caution_order=True),
    # ---- Signal & Telecom (SMMS) - field gear goes out of service ---------- #
    "POINT_MACHINE_TEST": WorkProfile(disconnection=True),
    "AXLE_COUNTER_CALIBRATION": WorkProfile(disconnection=True),
    "TRACK_CIRCUIT_BONDING": WorkProfile(disconnection=True),
    "LC_GATE_INTERLOCK_TEST": WorkProfile(disconnection=True),
    # An interlocking CPU diagnostic or an LED head swap touches no field gear, so
    # no Form T/351 is raised. Forcing one on every S&T row (which the pre-refactor
    # code did, off the department alone) manufactured paperwork for work that
    # never needed it and drove false escalations.
    "ELECTRONIC_INTERLOCKING_DIAG": WorkProfile(),
    "SIGNAL_ASPECT_REPLACEMENT": WorkProfile(),
    # ---- Traction Distribution (TDMS) ------------------------------------- #
    # Power isolation is a declared flag, not a work-type inference; these raise
    # no memo of their own.
    "EARTHING_BOND_AUDIT": WorkProfile(),
    "OHE_CANTILEVER_OVERHAUL": WorkProfile(),
    "CONTACT_WIRE_HOTSPOT": WorkProfile(),
    "NEUTRAL_SECTION_INSPECTION": WorkProfile(),
    "OHE_HEIGHT_STAGGER_REC": WorkProfile(),
    "ISOLATOR_INTERRUPTER_TEST": WorkProfile(),
}

#: Family prefixes, checked when the exact work type is unknown.
WORK_PROFILE_FAMILIES: Tuple[Tuple[str, WorkProfile], ...] = (
    ("POINT_MACHINE", WorkProfile(disconnection=True)),
    ("AXLE_COUNTER", WorkProfile(disconnection=True)),
    ("MSDAC", WorkProfile(disconnection=True)),
    ("TRACK_CIRCUIT", WorkProfile(disconnection=True)),
    ("AFTC", WorkProfile(disconnection=True)),
    ("LC_GATE", WorkProfile(disconnection=True)),
    ("USFD", WorkProfile(caution_order=True)),
    ("TAMPING", WorkProfile(caution_order=True)),
    ("TURNOUT", WorkProfile(caution_order=True)),
    ("BALLAST", WorkProfile(caution_order=True)),
    ("RAIL_", WorkProfile(caution_order=True)),
)

#: Department-level fallback when nothing else identifies the work.
DEPARTMENT_PROFILES: Dict[str, WorkProfile] = {
    "CIVIL": WorkProfile(caution_order=True),
    "SNT": WorkProfile(disconnection=True),
    "TRD": WorkProfile(),
}


def work_profile(work_type: Optional[str], department: Optional[str] = None) -> WorkProfile:
    """Resolve the statutory profile for a work type, then a department."""
    if work_type:
        token = re.sub(r"[^A-Z0-9]+", "_", str(work_type).strip().upper()).strip("_")
        if token in WORK_PROFILES:
            return WORK_PROFILES[token]
        for prefix, profile in WORK_PROFILE_FAMILIES:
            if token.startswith(prefix) or prefix in token:
                return profile
    if department:
        key = re.sub(r"[^A-Z0-9]+", "_", str(department).strip().upper()).strip("_")
        if key in DEPARTMENT_PROFILES:
            return DEPARTMENT_PROFILES[key]
        if key in ("SNT", "S_T", "SIGNAL", "SIGNALLING"):
            return DEPARTMENT_PROFILES["SNT"]
    return _NO_PROFILE


# --------------------------------------------------------------------------- #
#  Time helpers (the single implementation; safety.parse_time delegates here)
# --------------------------------------------------------------------------- #
_TIME_FORMATS: Tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M", "%H:%M:%S", "%H:%M",
)


def parse_timestamp(value: Any, default_date: Optional[datetime] = None) -> datetime:
    """
    Parse the time encodings CRIS / COA feeds use into a ``datetime``.

    A time-only value (``"01:30"``) is pinned to ``default_date`` (today if not
    given), which is how a block window on a planned date is reconstructed.
    """
    if isinstance(value, datetime):
        return value
    base = default_date or datetime.now()
    if value is None or str(value).strip() == "":
        return base
    text = str(value).strip()
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=base.year, month=base.month, day=base.day)
        return parsed
    raise ValueError(f"unparseable time {value!r}")


# --------------------------------------------------------------------------- #
#  Earthing window (ACTM Vol II Para 204)
# --------------------------------------------------------------------------- #
class EarthingBuffer(BaseModel):
    """
    The mandatory ACTM Para 204 discharge / earthing wrap around live OHE work.

    ``[earth 15 min] [work] [earth 15 min]`` - the section is earthed for the
    whole possession, so the real track occupancy is ``work + 30`` minutes. The
    model refuses to exist in a state where that arithmetic does not hold.
    """

    model_config = ConfigDict(extra="ignore")

    required: bool
    buffer_mins: int = Field(ge=0)
    work_duration_mins: int = Field(ge=0)
    total_possession_mins: int = Field(ge=0)
    power_off_at: datetime
    work_start: datetime
    work_end: datetime
    power_restored_at: datetime
    statutory_reference: str

    @model_validator(mode="after")
    def _enforce_para_204(self) -> "EarthingBuffer":
        if self.required:
            if self.buffer_mins != ACTM_PARA_204_EARTHING_BUFFER_MINS:
                raise ValueError(
                    f"ACTM Para 204 requires a {ACTM_PARA_204_EARTHING_BUFFER_MINS}-minute "
                    f"earthing buffer, got {self.buffer_mins}"
                )
            expected = self.work_duration_mins + 2 * self.buffer_mins
            if self.total_possession_mins != expected:
                raise ValueError(
                    f"possession {self.total_possession_mins} min != work "
                    f"{self.work_duration_mins} + 2 x {self.buffer_mins} min = {expected}"
                )
        elif self.buffer_mins != 0:
            raise ValueError("an earthing buffer was applied where Para 204 does not apply")
        if not (self.power_off_at <= self.work_start <= self.work_end <= self.power_restored_at):
            raise ValueError(
                "earthing window is not ordered: power_off_at <= work_start <= "
                "work_end <= power_restored_at"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def arithmetic(self) -> str:
        """Human-readable proof of the buffer arithmetic, for the memo."""
        if not self.required:
            return (
                f"{self.work_duration_mins} min (no live OHE exposure, "
                "ACTM Para 204 not applicable)"
            )
        return (
            f"{self.work_duration_mins} + {self.buffer_mins} + {self.buffer_mins} = "
            f"{self.total_possession_mins} min (ACTM Para 204 earthing both sides)"
        )

    def to_legacy_window(self) -> Dict[str, Any]:
        """
        The pre-existing ``compute_earthing_window`` dict shape.

        Retained so every current consumer (safety verdicts, the GeoJSON export,
        the guardrail) keeps reading the same keys while the arithmetic lives in
        exactly one place - here.
        """
        return {
            "statutory_reference": (
                f"ACTM Vol II Para 204 ({self.buffer_mins}-minute discharge/earthing buffer)"
                if self.required
                else "ACTM Para 204 not applicable (no live OHE exposure)"
            ),
            "power_isolation_required": bool(self.required),
            "earthing_buffer_mins": self.buffer_mins,
            "earthing_applied": self.buffer_mins > 0,
            "power_off_at": self.power_off_at.isoformat(),
            "earthing_before": {
                "from": self.power_off_at.isoformat(),
                "to": self.work_start.isoformat(),
                "minutes": self.buffer_mins,
            },
            "work_start": self.work_start.isoformat(),
            "work_end": self.work_end.isoformat(),
            "earthing_after": {
                "from": self.work_end.isoformat(),
                "to": self.power_restored_at.isoformat(),
                "minutes": self.buffer_mins,
            },
            "power_restored_at": self.power_restored_at.isoformat(),
            "work_duration_mins": self.work_duration_mins,
            "total_possession_mins": self.total_possession_mins,
            "duration_with_buffer_expr": self.arithmetic,
        }


def build_earthing_buffer(
    work_start: Any,
    duration_mins: int,
    power_isolation_required: bool = True,
    buffer_mins: int = ACTM_PARA_204_EARTHING_BUFFER_MINS,
    default_date: Optional[datetime] = None,
) -> EarthingBuffer:
    """
    Build the ACTM Para 204 wrap around a working window.

    ``work_start`` may be a ``datetime`` or any CRIS time string. When no live
    25 kV exposure applies the buffer is zero and the possession is exactly the
    working duration.
    """
    duration_mins = int(duration_mins)
    applied = int(buffer_mins) if power_isolation_required else 0
    start = parse_timestamp(work_start, default_date=default_date)
    work_end = start + timedelta(minutes=duration_mins)
    return EarthingBuffer(
        required=bool(power_isolation_required),
        buffer_mins=applied,
        work_duration_mins=duration_mins,
        total_possession_mins=duration_mins + 2 * applied,
        power_off_at=start - timedelta(minutes=applied),
        work_start=start,
        work_end=work_end,
        power_restored_at=work_end + timedelta(minutes=applied),
        statutory_reference=(
            ACTM_PARA_204_REFERENCE if applied
            else "ACTM Para 204 not applicable (no live OHE exposure)"
        ),
    )


# --------------------------------------------------------------------------- #
#  Signatures + affected gear
# --------------------------------------------------------------------------- #
class AuthorizationSignature(BaseModel):
    """
    One required signature on a statutory instrument.

    A generator in this module can only ever create a ``PENDING`` placeholder:
    :attr:`signed_by` and :attr:`signed_at` must be absent until a human signs,
    and the model rejects a ``SIGNED`` state without them. That is the line
    between *prefilling* a legal document and *forging* one.
    """

    model_config = ConfigDict(extra="ignore")

    authority: Authority
    designation: str = Field(description="e.g. 'Sectional Controller, Mumbai Division'")
    signed_by: Optional[str] = None
    employee_id: Optional[str] = None
    signed_at: Optional[datetime] = None
    signature_ref: Optional[str] = None
    status: SignatureStatus = SignatureStatus.PENDING

    @model_validator(mode="after")
    def _no_fabricated_signature(self) -> "AuthorizationSignature":
        if self.status is SignatureStatus.SIGNED and (not self.signed_by or not self.signed_at):
            raise ValueError(
                "a signature cannot be SIGNED without both signed_by and signed_at; "
                "the optimizer may only prefill PENDING authorizations"
            )
        return self

    def sign(self, signed_by: str, employee_id: Optional[str] = None,
             signed_at: Optional[datetime] = None, signature_ref: Optional[str] = None
             ) -> "AuthorizationSignature":
        """Record a human signature (the only sanctioned way to reach SIGNED)."""
        return self.model_copy(
            update={
                "signed_by": signed_by,
                "employee_id": employee_id,
                "signed_at": signed_at or datetime.now(),
                "signature_ref": signature_ref,
                "status": SignatureStatus.SIGNED,
            }
        )


def pending_signature(authority: Authority, designation: str) -> AuthorizationSignature:
    """A blank, unsigned placeholder for the authority that must sign."""
    return AuthorizationSignature(authority=authority, designation=designation)


class AffectedSignallingGear(BaseModel):
    """A signalling asset the possession puts out of service or returns."""

    model_config = ConfigDict(extra="ignore")

    gear_id: str
    gear_type: GearType = GearType.UNKNOWN
    action: GearAction = GearAction.DISCONNECT
    corridor_id: str = CORRIDOR_ID
    line_id: str = "UP_FAST"
    km: Optional[float] = None
    station_code: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def description(self) -> str:
        """Memo-ready one-liner naming the asset and its location."""
        where = f"km {self.km}" if self.km is not None else "chainage unresolved"
        if self.station_code:
            where = f"{self.station_code} ({where})"
        return f"{self.gear_id} [{self.gear_type.value}] {self.action.value} at {where}"


# --------------------------------------------------------------------------- #
#  Statutory instruments (the outputs)
# --------------------------------------------------------------------------- #
class StatutoryMemo(BaseModel):
    """
    Base class for a generated statutory instrument.

    Every field the optimizer *can* know is filled in; every field that requires
    a human judgement (a signature, a register entry, a test result) is left
    blank with an explicit placeholder list. ``window_start``/``window_end`` are
    the granted block window; ``form_no``/``valid_from``/``valid_to`` are legacy
    property aliases retained for existing consumers.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    form_code: FormCode
    form_title: str
    statutory_reference: str
    memo_no: str
    block_id: Optional[str] = None
    asset_ids: List[str] = Field(default_factory=list)
    corridor_id: str = CORRIDOR_ID
    line_id: str = "UP_FAST"
    km_start: float
    km_end: float
    section: Optional[str] = None
    window_start: datetime
    window_end: Optional[datetime] = None
    issued_to: Authority
    issued_for: str = Field(description="Primary asset/requisition this instrument covers")
    requirements: List[str] = Field(default_factory=list)
    status: PermitStatus = PermitStatus.PREFILLED
    generated_at: datetime = Field(default_factory=datetime.now)

    @field_validator("line_id", mode="before")
    @classmethod
    def _canonical_line(cls, value: Any) -> Any:
        return coerce_line_id(value)

    @field_validator("corridor_id", mode="before")
    @classmethod
    def _canonical_corridor(cls, value: Any) -> Any:
        return corridor_id_of({"corridor_id": value})

    @model_validator(mode="after")
    def _enforce_span(self) -> "StatutoryMemo":
        if self.km_start > self.km_end:
            raise ValueError(f"inverted chainage on {self.form_code.value}: {self.km_start} > {self.km_end}")
        for label, km in (("km_start", self.km_start), ("km_end", self.km_end)):
            if not (0.0 <= km <= CORRIDOR_LENGTH_KM):
                raise ValueError(f"{label}={km} outside the {self.corridor_id} corridor")
        if self.window_end is not None and self.window_end < self.window_start:
            raise ValueError("window_end precedes window_start")
        return self

    # ------------------------------------------------------------------ #
    #  Derived + legacy accessors
    # ------------------------------------------------------------------ #
    @computed_field  # type: ignore[prop-decorator]
    @property
    def km_range(self) -> str:
        """Chainage range exactly as a printed memo renders it."""
        return f"{self.km_start} - {self.km_end}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lrs_key(self) -> List[str]:
        """The ``(corridor_id, line_id)`` scope this instrument is valid in."""
        return [self.corridor_id, self.line_id]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def form_no(self) -> str:
        """Legacy name for :attr:`form_code`."""
        return self.form_code.value

    @property
    def valid_from(self) -> str:
        """Legacy ISO timestamp of :attr:`window_start`."""
        return self.window_start.isoformat()

    @property
    def valid_to(self) -> Optional[str]:
        """Legacy ISO timestamp of :attr:`window_end`."""
        return self.window_end.isoformat() if self.window_end else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unsigned(self) -> bool:
        """True while the instrument is still awaiting a human signature."""
        return self.status is PermitStatus.PREFILLED

    def to_legacy_memo_dict(self) -> Dict[str, Any]:
        """
        The dict shape the pre-refactor ``generate_statutory_memos`` returned.

        Keeps ``form_no``/``valid_from``/``valid_to`` working for the safety
        verdict payload and the API, while the model above stays the source of
        truth.
        """
        payload = self.model_dump(mode="json")
        payload.update(
            {
                "form_no": self.form_code.value,
                "title": self.form_title,
                "issued_for": self.issued_for,
                "line": self.line_id,
                "km_range": self.km_range,
                "valid_from": self.valid_from,
                "valid_to": self.valid_to,
                "issued_to": self.issued_to.value,
                "status": self.status.value,
            }
        )
        return payload


class FormT351Notice(StatutoryMemo):
    """
    IRSEM Para 22 **Form T/351** - S&T Disconnection Notice.

    Issued by the Sectional Controller (on the Signal Maintainer's request)
    before signalling gear is disconnected. Prefilled with the granted window
    and the affected gear; the lever-lock / disconnection-register entry is left
    blank because a human makes it at the site.
    """

    form_code: Literal[FormCode.T_351] = FormCode.T_351
    form_title: str = "Disconnection Notice (S&T gear)"
    statutory_reference: str = IRSEM_PARA_22_REFERENCE
    affected_gears: List[AffectedSignallingGear] = Field(default_factory=list)
    disconnection_register_entry: Optional[str] = Field(
        default=None, description="Filled in at the site when the disconnection collar is applied"
    )
    reconnection_form_no: Optional[str] = Field(
        default=None, description="Memo number of the paired Form T/352"
    )


class FormT352Notice(StatutoryMemo):
    """
    IRSEM Para 22 **Form T/352** - Reconnection Notice.

    The counterpart to Form T/351. Prefilled at scheduling time so it is ready
    in the register, but it only takes effect once the gear has been proved back
    in; ``window_start`` is the moment power is restored, and there is no end (a
    reconnection does not expire).
    """

    form_code: Literal[FormCode.T_352] = FormCode.T_352
    form_title: str = "Reconnection Notice (S&T gear proved in)"
    statutory_reference: str = IRSEM_PARA_22_REFERENCE
    affected_gears: List[AffectedSignallingGear] = Field(default_factory=list)
    proofs: List[str] = Field(default_factory=list)
    disconnection_form_no: Optional[str] = Field(
        default=None, description="Memo number of the paired Form T/351"
    )


class ACTMPermitToWork(StatutoryMemo):
    """
    **ACTM Permit-to-Work** for work inside the 25 kV OHE danger zone.

    Carries the Para 203 isolation reference, the Para 204 earthing wrap, the
    feeding post / isolator that must be opened, and the Traction Power
    Controller's authorization. The TPC countersigns; the optimizer only
    proposes.
    """

    form_code: Literal[FormCode.ACTM_PTW] = FormCode.ACTM_PTW
    form_title: str = "Traction Permit-To-Work with 25 kV earthing"
    statutory_reference: str = ACTM_PARA_203_REFERENCE
    earthing: EarthingBuffer
    feeding_post: Optional[str] = None
    isolator: Optional[str] = None
    earthing_rod_locations: List[str] = Field(default_factory=list)
    tpc_authorization: AuthorizationSignature


class IRPWM284CautionOrder(StatutoryMemo):
    """
    **IRPWM Para 284** caution order for P-Way work under traffic.

    Notified through the Station Masters to every Loco Pilot and Guard working
    over the section, with the permanent speed restriction where one applies.
    """

    form_code: Literal[FormCode.IRPWM_284_CAUTION] = FormCode.IRPWM_284_CAUTION
    form_title: str = "Caution Order for P-Way work under traffic"
    statutory_reference: str = IRPWM_PARA_284_REFERENCE
    speed_restriction_psr: Optional[int] = Field(
        default=None, description="IRPWM Para 268(b) permanent speed restriction, km/h"
    )


# --------------------------------------------------------------------------- #
#  Derived permit requirements (engineering attributes -> instruments)
# --------------------------------------------------------------------------- #
class PermitRequirements(BaseModel):
    """
    Which instruments a piece of work needs, derived before anything is signed.

    This is the replacement for the old ``statutory_form`` input gate. The
    question it answers is *"what paperwork will this work pull in?"*, not
    *"did the requisition already cite it?"* - a requisition cannot cite a memo
    that the schedule has not yet created.
    """

    model_config = ConfigDict(extra="ignore")

    corridor_id: str = CORRIDOR_ID
    line_id: str = "UP_FAST"
    section: Optional[str] = None
    work_type: Optional[str] = None
    fault_code: Optional[str] = None

    requires_disconnection: bool = False
    requires_caution_order: bool = False
    requires_power_block: bool = False
    requires_traffic_block: bool = False
    requires_earthing_buffer: bool = False

    #: Statutory references cited on the incoming row. Provenance only.
    cited_forms: List[FormCode] = Field(default_factory=list)
    #: Why each requirement was raised, for the audit trail.
    reasons: List[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def forms(self) -> List[str]:
        """The instruments that will be generated, in issue order."""
        forms: List[str] = []
        if self.requires_disconnection:
            forms.extend([FormCode.T_351.value, FormCode.T_352.value])
        if self.requires_power_block:
            forms.append(FormCode.ACTM_PTW.value)
        if self.requires_caution_order:
            forms.append(FormCode.IRPWM_284_CAUTION.value)
        return forms


def _as_bool(value: Any) -> bool:
    """Truthiness for a declared flag arriving as bool / int / CRIS token."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().upper() in {"1", "Y", "YES", "T", "TRUE", "REQUIRED", "ON"}


def _department_of(source: Mapping[str, Any]) -> Optional[str]:
    """Canonical directorate token: CIVIL (TMS) / SNT (SMMS) / TRD (TDMS)."""
    raw = source.get("dept") or source.get("department") or source.get("directorate")
    if raw is None:
        return None
    token = getattr(raw, "value", raw)
    key = re.sub(r"[^A-Z0-9]+", "_", str(token).strip().upper()).strip("_")
    return {
        "CIVIL": "CIVIL", "ENGINEERING": "CIVIL", "ENGG": "CIVIL", "PWAY": "CIVIL",
        "P_WAY": "CIVIL", "TRACK": "CIVIL", "TMS": "CIVIL",
        "SNT": "SNT", "S_T": "SNT", "SIGNAL": "SNT", "SIGNALLING": "SNT",
        "TELECOM": "SNT", "SMMS": "SNT",
        "TRD": "TRD", "ELECTRICAL": "TRD", "ELECTRICAL_TRD": "TRD", "OHE": "TRD",
        "TDMS": "TRD",
    }.get(key)


def derive_permit_requirements(requisition: Mapping[str, Any]) -> PermitRequirements:
    """
    Derive the statutory instruments a requisition will require.

    Inputs are **engineering attributes** - work type / fault code, department,
    chainage - plus the declared operating context. A statutory form cited on
    the incoming row is recorded as provenance and nothing more.

    Conservatism rule: a requirement is raised by *either* the declared flag
    *or* the work-type profile, so a silo that simply omits
    ``requires_disconnection`` (as the SMMS generator does) still gets its
    Form T/351. Requirements can only be added, never talked away.
    """
    source: Dict[str, Any] = dict(requisition)
    work_type = source.get("work_type") or source.get("defect_type")
    fault_code = source.get("fault_code")
    department = _department_of(source)
    profile = work_profile(work_type or fault_code, department)

    declared_disconnection = _as_bool(source.get("requires_disconnection"))
    declared_power = _as_bool(source.get("requires_power_block"))
    declared_traffic = source.get("requires_traffic_block")
    traffic = True if declared_traffic is None else _as_bool(declared_traffic)
    declared_earthing = source.get("requires_earthing_buffer")
    earthing = True if declared_earthing is None else _as_bool(declared_earthing)

    reasons: List[str] = []
    disconnection = declared_disconnection or profile.disconnection
    if declared_disconnection:
        reasons.append("declared as gear disconnection work on the requisition")
    elif profile.disconnection:
        reasons.append(
            f"work type {work_type or fault_code!r} touches field signalling gear "
            f"(IRSEM Para 22)"
        )

    caution = profile.caution_order and traffic
    if caution:
        reasons.append(
            f"work type {work_type or fault_code or department!r} is on-track P-Way work "
            f"under traffic, requiring a caution order (IRPWM Para 284)"
        )
    if declared_power:
        reasons.append("25 kV OHE exposure declared; TPC isolation under ACTM Para 203")

    cited: List[FormCode] = []
    for key in ("referenced_form", "statutory_form", "form", "form_no"):
        code = coerce_form_code(source.get(key))
        if code is not None and code not in cited:
            cited.append(code)

    return PermitRequirements(
        corridor_id=corridor_id_of(source),
        line_id=line_id_of(source),
        section=source.get("section"),
        work_type=work_type,
        fault_code=fault_code,
        requires_disconnection=disconnection,
        requires_caution_order=caution,
        requires_power_block=declared_power,
        requires_traffic_block=traffic,
        requires_earthing_buffer=bool(earthing and declared_power),
        cited_forms=cited,
        reasons=reasons,
    )


def requires_gear_identification(requirements: PermitRequirements,
                                 gears: Sequence[AffectedSignallingGear]) -> bool:
    """
    True when the row demands a disconnection but names no gear to disconnect.

    Form T/351 has a field for *which* gear is being proved out. A requisition
    that cannot fill it cannot produce a valid notice, so this is a real
    engineering-data gap - unlike a missing memo reference, which simply has not
    been issued yet.
    """
    return requirements.requires_disconnection and not gears


# --------------------------------------------------------------------------- #
#  Gear extraction + prefill
# --------------------------------------------------------------------------- #
_GEAR_KEYS: Tuple[str, ...] = ("gear_id", "point_no", "point_machine", "equipment_id", "asset_gear_id")


def _gear_of(task: Mapping[str, Any]) -> Optional[str]:
    """First gear identifier present on a task, in either silo spelling."""
    indexed = {re.sub(r"[^A-Z0-9]+", "_", str(k).strip().upper()): v for k, v in task.items()}
    for key in _GEAR_KEYS:
        value = indexed.get(key.upper())
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def collect_affected_gears(
    tasks: Sequence[Mapping[str, Any]],
    line_id: str,
    corridor_id: str = CORRIDOR_ID,
    km_hint: Optional[float] = None,
    station_code: Optional[str] = None,
) -> List[AffectedSignallingGear]:
    """Every gear referenced by a block's bundled tasks, classified and located."""
    gears: List[AffectedSignallingGear] = []
    seen: set = set()
    for task in tasks:
        gear_id = _gear_of(task)
        if not gear_id or gear_id in seen:
            continue
        seen.add(gear_id)
        work_type = task.get("work_type") or task.get("defect_type")
        km = task.get("km_start")
        gears.append(
            AffectedSignallingGear(
                gear_id=gear_id,
                gear_type=classify_gear(gear_id, work_type),
                action=GearAction.DISCONNECT,
                corridor_id=corridor_id,
                line_id=line_id,
                km=coerce_km(km) if km is not None else km_hint,
                station_code=station_code,
            )
        )
    return gears


def _span_of(source: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]) -> Tuple[str, str, float, float]:
    """Chainage envelope of a block, falling back to its bundled tasks."""
    if source.get("km_start") is not None and source.get("km_end") is not None:
        return (
            corridor_id_of(source),
            line_id_of(source),
            km_start_of(source),
            km_end_of(source),
        )
    spans = [t for t in tasks if t.get("km_start") is not None and t.get("km_end") is not None]
    if not spans:
        return CORRIDOR_ID, coerce_line_id(source.get("line_id") or source.get("track_id")), 0.0, 0.0
    return (
        corridor_id_of(source),
        line_id_of(source),
        min(km_start_of(t) for t in spans),
        max(km_end_of(t) for t in spans),
    )


def build_consent_placeholders(
    requirements: PermitRequirements,
) -> List[AuthorizationSignature]:
    """
    The authorities that must sign, in escalation order.

    Always includes the Sectional Controller for anything that occupies the
    track; the TPC is added when traction power is isolated, and the Station
    Master is notified for P-Way work under traffic.
    """
    signatures = [
        pending_signature(Authority.SECTION_CONTROLLER, "Sectional Controller, Mumbai Division")
    ]
    if requirements.requires_power_block:
        signatures.append(
            pending_signature(
                Authority.TRACTION_POWER_CONTROLLER,
                "Traction Power Controller, Mumbai Suburban",
            )
        )
    if requirements.requires_caution_order:
        signatures.append(
            pending_signature(Authority.STATION_MASTER, "Station Master (through NOTICE)")
        )
        signatures.append(
            pending_signature(Authority.SECTION_ENGINEER, "Section Engineer (P-Way)")
        )
    return signatures


class DigitalGrantPermit(BaseModel):
    """
    The umbrella instrument: one authorised possession, with its memos attached.

    Produced for every block the optimizer schedules. It carries the granted
    window, the ACTM Para 204 earthing wrap, every affected signalling gear, the
    statutory instruments that must exist before work starts, and the *unsigned*
    authorization slots for the officers who must release them.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    permit_id: str
    block_id: Optional[str] = None
    asset_ids: List[str] = Field(default_factory=list)

    # ---- 1D LRS identity of the possession ------------------------------- #
    corridor_id: str = CORRIDOR_ID
    line_id: str = "UP_FAST"
    km_start: float
    km_end: float
    section: Optional[str] = None

    # ---- granted window + the ACTM Para 204 wrap ------------------------- #
    window_start: datetime
    window_end: datetime
    work_start: datetime
    work_end: datetime
    earthing: EarthingBuffer

    # ---- work context --------------------------------------------------- #
    requested_duration_mins: int
    allocated_duration_mins: int
    possession_mins: int
    requires_power_block: bool = False
    requires_traffic_block: bool = False
    requires_disconnection: bool = False
    speed_restriction_psr: Optional[int] = None

    requirements: PermitRequirements
    affected_signalling_gears: List[AffectedSignallingGear] = Field(default_factory=list)

    # ---- the statutory instruments -------------------------------------- #
    form_t351: Optional[FormT351Notice] = None
    form_t352: Optional[FormT352Notice] = None
    actm_permit_to_work: Optional[ACTMPermitToWork] = None
    caution_order: Optional[IRPWM284CautionOrder] = None

    authorizations: List[AuthorizationSignature] = Field(default_factory=list)
    status: PermitStatus = PermitStatus.PREFILLED
    generated_at: datetime = Field(default_factory=datetime.now)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lrs_key(self) -> List[str]:
        """``(corridor_id, line_id)`` - the 1D scope of this possession."""
        return [self.corridor_id, self.line_id]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def span_km(self) -> float:
        """Degrees of freedom the permit locks out, in kilometres."""
        return round(self.km_end - self.km_start, 3)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def memos(self) -> List[StatutoryMemo]:
        """Every generated instrument, in the order they must be issued."""
        ordered: List[StatutoryMemo] = []
        for memo in (self.form_t351, self.form_t352, self.actm_permit_to_work, self.caution_order):
            if memo is not None:
                ordered.append(memo)
        return ordered

    @computed_field  # type: ignore[prop-decorator]
    @property
    def memo_index(self) -> Dict[str, str]:
        """``form code -> memo number``, for cross-referencing T/351 with T/352."""
        return {memo.form_code.value: memo.memo_no for memo in self.memos}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def statutory_references(self) -> List[str]:
        """Distinct statutory references the permit invokes, deduplicated."""
        references: List[str] = []
        for memo in self.memos:
            if memo.statutory_reference not in references:
                references.append(memo.statutory_reference)
        return references

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unsigned_authorities(self) -> List[str]:
        """Authorities still to sign - the open actions on the control desk."""
        return [
            signature.authority.value
            for signature in self.authorizations
            if signature.status is SignatureStatus.PENDING
        ]

    def issue(self, authority: Authority, signed_by: str, **kwargs: Any) -> "DigitalGrantPermit":
        """
        Record one authority's signature and promote the permit to ``ISSUED``
        once every required signature is in. The only path to a signed permit.
        """
        updated = [
            signature.sign(signed_by, **kwargs) if signature.authority is authority else signature
            for signature in self.authorizations
        ]
        fully_signed = all(s.status is SignatureStatus.SIGNED for s in updated)
        return self.model_copy(
            update={
                "authorizations": updated,
                "status": PermitStatus.ISSUED if fully_signed else PermitStatus.PREFILLED,
            }
        )


# --------------------------------------------------------------------------- #
#  Generators
# --------------------------------------------------------------------------- #
def build_permits_for_block(
    block: Mapping[str, Any],
    generated_at: Optional[datetime] = None,
) -> DigitalGrantPermit:
    """
    Prefill the full statutory bundle for one *scheduled* block.

    This is the downstream half of the refactor: the memo is created because a
    window now exists, not because a requisition declared one. Everything the
    optimizer knows is filled in; everything that needs a human is left as an
    explicit placeholder.

    ``block`` is a scheduled block record (as produced by
    :class:`~Backend.ai_engine.block_optimizer.IntegratedBlockOptimizer`).
    """
    stamp = generated_at or datetime.now()
    tasks: List[Mapping[str, Any]] = [t for t in (block.get("tasks_bundled") or []) if isinstance(t, Mapping)]
    if not tasks:
        tasks = [dict(block)]

    corridor_id, line_id, km_start, km_end = _span_of(block, tasks)
    block_id = block.get("block_id")

    # ---- the granted window --------------------------------------------- #
    date = str(block.get("date") or "").strip()
    start_time = block.get("start_time") or "00:00"
    anchor = stamp
    if date:
        try:
            anchor = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            anchor = stamp
    work_start = parse_timestamp(
        f"{date} {start_time}".strip() if date else start_time, default_date=anchor
    )

    allocated = int(block.get("allocated_duration_mins") or 0)
    requested = max(
        [int(t.get("requested_duration_mins") or t.get("duration_mins") or 0) for t in tasks]
        + [allocated]
    )
    power = any(_as_bool(t.get("requires_power_block")) for t in tasks) or _as_bool(
        block.get("requires_power_block")
    )
    # The possession must cover the longest bundle member, so the ACTM wrap is
    # applied to the allocated duration rather than the raw request.
    earthing = build_earthing_buffer(
        work_start, allocated or requested, power, default_date=anchor
    )

    # ---- requirements: the UNION over every bundled job ------------------ #
    # A joint block is legal only once every instrument any of its members needs
    # exists, so requirements are unioned across the bundle rather than read off
    # one "primary" task. The per-task derivation is kept (not a lookup on a
    # merged row) because each member knows its own department and work type.
    per_task = [derive_permit_requirements(dict(task)) for task in tasks] or [
        derive_permit_requirements(dict(block))
    ]
    work_types = sorted(
        {
            str(task.get("work_type") or task.get("defect_type"))
            for task in tasks
            if task.get("work_type") or task.get("defect_type")
        }
    )
    cited: List[FormCode] = []
    reasons: List[str] = []
    for derived in per_task:
        for form in derived.cited_forms:
            if form not in cited:
                cited.append(form)
        for reason in derived.reasons:
            if reason not in reasons:
                reasons.append(reason)

    requirements = PermitRequirements(
        corridor_id=corridor_id,
        line_id=line_id,
        section=block.get("section"),
        work_type="+".join(work_types) or None,
        fault_code=next(
            (task.get("fault_code") for task in tasks if task.get("fault_code")), None
        ),
        requires_disconnection=any(r.requires_disconnection for r in per_task),
        requires_caution_order=any(r.requires_caution_order for r in per_task),
        requires_power_block=power,
        requires_traffic_block=any(r.requires_traffic_block for r in per_task),
        requires_earthing_buffer=bool(earthing.required),
        cited_forms=cited,
        reasons=reasons,
    )

    asset_ids = [
        str(t.get("asset_id") or t.get("defect_id"))
        for t in tasks
        if (t.get("asset_id") or t.get("defect_id"))
    ]
    issued_for = asset_ids[0] if asset_ids else str(block_id or "UNASSIGNED")
    gears = collect_affected_gears(tasks, line_id, corridor_id, km_hint=km_start)

    common: Dict[str, Any] = {
        "block_id": str(block_id) if block_id else None,
        "asset_ids": asset_ids,
        "corridor_id": corridor_id,
        "line_id": line_id,
        "km_start": km_start,
        "km_end": km_end,
        "section": block.get("section"),
        "issued_for": issued_for,
        "generated_at": stamp,
    }

    permit_id = f"IR-DGP-{block_id or issued_for}"
    memo_prefix = permit_id

    # ---- IRSEM Para 22: Form T/351 + Form T/352 -------------------------- #
    t351: Optional[FormT351Notice] = None
    t352: Optional[FormT352Notice] = None
    if requirements.requires_disconnection:
        t351 = FormT351Notice(
            **common,
            memo_no=f"{memo_prefix}/T351",
            window_start=earthing.work_start,
            window_end=earthing.power_restored_at,
            issued_to=Authority.SECTION_CONTROLLER,
            affected_gears=gears,
            reconnection_form_no=f"{memo_prefix}/T352",
            requirements=[
                "Point machine / track circuit proved disconnected and clamped before work",
                "Lever lock and disconnection collar applied; disconnection register entry made",
                "No signal movement permitted until Form T/352 is issued",
            ],
        )
        t352 = FormT352Notice(
            **common,
            memo_no=f"{memo_prefix}/T352",
            window_start=earthing.power_restored_at,
            window_end=None,  # a reconnection does not expire
            issued_to=Authority.SECTION_CONTROLLER,
            affected_gears=[gear.model_copy(update={"action": GearAction.RECONNECT}) for gear in gears],
            disconnection_form_no=t351.memo_no,
            proofs=[
                "Insulation / earth test values recorded before reconnection",
                "Point machine obstruction test repeated and proved",
                "Track circuit occupancy test completed for the full section",
            ],
            requirements=[
                "Form T/352 issued to the Sectional Controller only after every proof above",
                "Gear restored to normal working and the register entry closed",
            ],
        )

    # ---- ACTM Para 203 / 204: Permit-to-Work with earthing -------------- #
    ptw: Optional[ACTMPermitToWork] = None
    if requirements.requires_power_block:
        rod_locations = [
            f"{gear.description} ({gear.gear_type.value} isolation boundary)" for gear in gears
        ] or [f"km {km_start} (start of isolated zone)", f"km {km_end} (end of isolated zone)"]
        ptw = ACTMPermitToWork(
            **common,
            memo_no=f"{memo_prefix}/PTW",
            window_start=earthing.power_off_at,
            window_end=earthing.power_restored_at,
            issued_to=Authority.TRACTION_POWER_CONTROLLER,
            earthing=earthing,
            feeding_post=block.get("feeding_post") or None,
            isolator=block.get("isolator") or None,
            earthing_rod_locations=rod_locations,
            tpc_authorization=pending_signature(
                Authority.TRACTION_POWER_CONTROLLER,
                "Traction Power Controller, Mumbai Suburban",
            ),
            requirements=[
                ACTM_PARA_203_REFERENCE,
                f"ACTM Para 204: {earthing.buffer_mins} min discharge/earthing buffer "
                "before AND after live work",
                "Earthing rods applied at both ends of the isolated zone",
                "TPC written confirmation of power off received before earthing",
                "Section restored to traffic only after earthing removed and TPC informed",
            ],
        )

    # ---- IRPWM Para 284: caution order ---------------------------------- #
    caution: Optional[IRPWM284CautionOrder] = None
    if requirements.requires_caution_order:
        caution = IRPWM284CautionOrder(
            **common,
            memo_no=f"{memo_prefix}/CAUTION",
            window_start=earthing.work_start,
            window_end=earthing.work_end,
            issued_to=Authority.SECTION_CONTROLLER,
            speed_restriction_psr=_first_psr(tasks, block),
            requirements=[
                "Caution order issued to all affected drivers through the Station Masters",
                "Look-out man posted in both directions",
                "Indication / banner flags placed 600 m on either approach as applicable",
            ],
        )

    system = next((t.get("system") for t in tasks if t.get("system")), None)
    if system:
        common["system"] = system

    return DigitalGrantPermit(
        **common,
        permit_id=permit_id,
        window_start=earthing.power_off_at,
        window_end=earthing.power_restored_at,
        work_start=earthing.work_start,
        work_end=earthing.work_end,
        earthing=earthing,
        requested_duration_mins=requested,
        allocated_duration_mins=allocated or requested,
        possession_mins=earthing.total_possession_mins,
        requires_power_block=requirements.requires_power_block,
        requires_traffic_block=requirements.requires_traffic_block,
        requires_disconnection=requirements.requires_disconnection,
        speed_restriction_psr=_first_psr(tasks, block),
        requirements=requirements,
        affected_signalling_gears=gears,
        form_t351=t351,
        form_t352=t352,
        actm_permit_to_work=ptw,
        caution_order=caution,
        authorizations=build_consent_placeholders(requirements),
    )


def _first_psr(tasks: Sequence[Mapping[str, Any]], block: Mapping[str, Any]) -> Optional[int]:
    """First speed restriction declared anywhere on the block, km/h."""
    for source in list(tasks) + [block]:
        for key in ("speed_restriction_psr", "psr_speed_kmph", "psr_speed", "psr"):
            value = source.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def build_permits_for_requisition(
    requisition: Mapping[str, Any],
    generated_at: Optional[datetime] = None,
) -> DigitalGrantPermit:
    """
    *Provisional* permit bundle for a requisition that is not yet scheduled.

    Used to preview the paperwork a demand will attract, and to confirm the
    engineering data is sufficient to raise it. Windows are placeholders derived
    from the declared ``work_start`` (today if absent), so the memo numbers and
    gear lists are indicative until :func:`build_permits_for_block` runs for
    real. The returned permit is always ``PREFILLED`` and never signed.
    """
    stamp = generated_at or datetime.now()
    duration = int(
        requisition.get("requested_duration_mins") or requisition.get("duration_mins") or 0
    )
    power = _as_bool(requisition.get("requires_power_block"))
    earthing = build_earthing_buffer(
        requisition.get("work_start"), duration, power, default_date=stamp
    )
    requirements = derive_permit_requirements(requisition)
    requirements = requirements.model_copy(
        update={"requires_earthing_buffer": bool(earthing.required)}
    )
    line_id = line_id_of(requisition)
    corridor_id = corridor_id_of(requisition)
    km_start, km_end = km_start_of(requisition), km_end_of(requisition)
    asset_id = str(
        requisition.get("asset_id") or requisition.get("defect_id") or "UNASSIGNED"
    )
    gears = collect_affected_gears([requisition], line_id, corridor_id, km_hint=km_start)

    # A block-shaped view, so both entry points share one generation path.
    return build_permits_for_block(
        {
            "block_id": None,
            "date": None,
            "start_time": earthing.work_start.strftime("%H:%M"),
            "corridor_id": corridor_id,
            "line_id": line_id,
            "km_start": km_start,
            "km_end": km_end,
            "section": requisition.get("section"),
            "allocated_duration_mins": duration,
            "requires_power_block": power,
            "requires_traffic_block": requirements.requires_traffic_block,
            "work_type": requirements.work_type,
            "fault_code": requirements.fault_code,
            "requires_disconnection": requirements.requires_disconnection,
            "speed_restriction_psr": _first_psr([requisition], {}),
            "tasks_bundled": [
                dict(requisition, asset_id=asset_id, km_start=km_start, km_end=km_end)
            ],
        },
        generated_at=stamp,
    )


__all__ = [
    # constants + references
    "ACTM_PARA_204_EARTHING_BUFFER_MINS",
    "ACTM_PARA_203_REFERENCE",
    "ACTM_PARA_204_REFERENCE",
    "IRSEM_PARA_22_REFERENCE",
    "IRSEM_DISCONNECTION_FORM",
    "IRSEM_RECONNECTION_FORM",
    "IRPWM_PARA_284_REFERENCE",
    "IRPWM_PARA_268B_REFERENCE",
    "TRACK_PERMISSIBLE_SPEED_KMPH",
    # enums + helpers
    "FormCode",
    "ISSUABLE_FORMS",
    "FORM_CODE_ALIASES",
    "coerce_form_code",
    "PermitStatus",
    "Authority",
    "SignatureStatus",
    "GearType",
    "GearAction",
    "classify_gear",
    "WorkProfile",
    "WORK_PROFILES",
    "work_profile",
    "parse_timestamp",
    # models
    "EarthingBuffer",
    "build_earthing_buffer",
    "AuthorizationSignature",
    "pending_signature",
    "AffectedSignallingGear",
    "StatutoryMemo",
    "FormT351Notice",
    "FormT352Notice",
    "ACTMPermitToWork",
    "IRPWM284CautionOrder",
    "PermitRequirements",
    "DigitalGrantPermit",
    # generators
    "derive_permit_requirements",
    "requires_gear_identification",
    "collect_affected_gears",
    "build_consent_placeholders",
    "build_permits_for_block",
    "build_permits_for_requisition",
]
