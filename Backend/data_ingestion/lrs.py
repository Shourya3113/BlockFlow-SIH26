"""
lrs.py - 1D Linear Referencing System (LRS) for Indian Railways Block Planning

This module is the **primary spatial reference** of BlockFlow. A track, a defect
and a possession are all identified the way Indian Railways actually identifies
them:

    (corridor_id, line_id, km_start, km_end)

Nothing here knows about latitude, longitude, Shapely, polygons, buffering or
geometric intersection - and that is the point. Chainage is a **1D** coordinate:
two possessions on the same running line conflict if and only if their kilometre
intervals overlap, which is a closed-form comparison of four floats:

    has_overlap(A, B)  <=>  (A.corridor_id == B.corridor_id)
                        and (A.line_id    == B.line_id)
                        and (max(A.km_start, B.km_start) < min(A.km_end, B.km_end))

Why 1D and not WGS84 polygons
-----------------------------
*   CRIS silos (TMS / SMMS / TDMS / COA) *store* ``corridor_id``, ``track`` and
    ``KM_FROM``/``KM_TO``. They never store latitude/longitude. Projecting to
    WGS84 and back only to test overlap invents two sources of error (projection
    and float tolerance) where one exact comparison already exists.
*   A kilometre is a statutory unit. "km 21.35 to km 21.65" is unambiguous;
    "does this polygon intersect that polygon" depends on the buffer radius, the
    vertex sampling pitch and the CRS.
*   The HiGHS MILP needs a discrete conflict relation it can encode as a linear
    constraint. An interval overlap is exactly that; a polygon intersection is
    neither linear nor stable under re-sampling.

The 1D contract is also *auditable*: a jury can re-derive any conflict verdict
with a pencil.

Geographic coordinates are still produced - but strictly downstream, as a
read-only display projection via :mod:`Backend.data_ingestion.waypoints`. They
never participate in a solver constraint or a validation invariant.

Design rules
------------
1.  **Chainage is truth.** ``km_start``/``km_end`` are the authority; any
    latitude/longitude is a derived, disposable rendering artefact.
2.  **Integer-exact comparisons.** Intervals are compared with strict ``<``/``>``
    on floats that were quantised once, at ingress (:func:`coerce_km`).
3.  **Failed line resolution is conservative.** When a payload cannot be
    resolved to one of the four running lines it is bucketed as ``UNKNOWN``, so
    two unresolvable records still collide instead of silently passing a
    guardrail.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Literal, Mapping, Sequence, Tuple

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
#  The LRS identity of the corridor
# --------------------------------------------------------------------------- #
#: Statutory corridor key. Every chainage in this system is measured from km
#: 0.000 at Churchgate (CCG) on Western Railway, Mumbai Division.
CORRIDOR_ID = "WR-MUMBAI-CCG-VR"

#: Statutory corridor length (km). Chainage is valid on ``[0.0, 59.980]``.
CORRIDOR_LENGTH_KM = 59.980

#: The four running lines of the Churchgate-Virar quad corridor.
LineId = Literal["UP_FAST", "UP_SLOW", "DN_SLOW", "DN_FAST"]

#: Runtime tuple of the same four ids (iteration / membership / validation).
LINE_IDS: Tuple[LineId, ...] = ("UP_FAST", "UP_SLOW", "DN_SLOW", "DN_FAST")

#: Bucket for a payload whose running line cannot be resolved. Kept distinct
#: from every real line so two unresolved records still collide (conservative).
UNKNOWN_LINE_ID = "UNKNOWN"

#: Spellings seen in CRIS exports, folded onto the four canonical line ids.
#: This is the single source of truth for that mapping: the Pydantic contract,
#: the safety invariants and the MILP all resolve a running line through
#: :func:`coerce_line_id`, so they can never disagree about which line a job is on.
LINE_ID_ALIASES: Dict[str, str] = {
    "UP_FAST": "UP_FAST", "UP FAST": "UP_FAST", "UPFAST": "UP_FAST",
    "UP-FAST": "UP_FAST", "FAST UP": "UP_FAST", "UF": "UP_FAST", "UP": "UP_FAST",
    "UP LINE": "UP_FAST", "UP_MAIN": "UP_FAST",
    "UP_SLOW": "UP_SLOW", "UP SLOW": "UP_SLOW", "UPSLOW": "UP_SLOW",
    "UP-SLOW": "UP_SLOW", "SLOW UP": "UP_SLOW", "US": "UP_SLOW",
    "DN_SLOW": "DN_SLOW", "DN SLOW": "DN_SLOW", "DNSLOW": "DN_SLOW",
    "DN-SLOW": "DN_SLOW", "SLOW DN": "DN_SLOW", "DS": "DN_SLOW",
    "DN_FAST": "DN_FAST", "DN FAST": "DN_FAST", "DNFAST": "DN_FAST",
    "DN-FAST": "DN_FAST", "FAST DN": "DN_FAST", "DF": "DN_FAST", "DN": "DN_FAST",
    "DN LINE": "DN_FAST", "DN_MAIN": "DN_FAST",
}

#: Corridor spellings folded onto :data:`CORRIDOR_ID`.
CORRIDOR_ID_ALIASES: Dict[str, str] = {
    "WR-MUMBAI-CCG-VR": CORRIDOR_ID,
    "WR_MUMBAI_CCG_VR": CORRIDOR_ID,
    "CCG-VR": CORRIDOR_ID,
    "CCG - VR": CORRIDOR_ID,
    "CCG_VR": CORRIDOR_ID,
    "CHURCHGATE-VIRAR": CORRIDOR_ID,
    "CHURCHGATE - VIRAR": CORRIDOR_ID,
    "CHURCHGATE VIRAR": CORRIDOR_ID,
    "MUMBAI SUBURBAN": CORRIDOR_ID,
    "WR": CORRIDOR_ID,
}

_KM_UNIT_RE = re.compile(r"^(?P<num>[-+]?\d+(?:\.\d+)?)\s*(?P<unit>km|kms|m|mtr|mtrs|metre|metres|meter|meters)?$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
#  Coercion
# --------------------------------------------------------------------------- #
def coerce_km(value: Any) -> float:
    """
    Quantise a CRIS chainage encoding to kilometres (6 dp), exactly once.

    Handles the encodings the silos actually emit - ``19.40``, ``"19.40 KM"``,
    ``"20400 M"`` (a bare metre figure, a very common TMS export quirk) and
    ``"1,234.5"`` - and rejects non-finite values instead of clamping them. A
    silently clamped kilometre is how a tamper lands on the wrong track.
    """
    if value is None:
        raise ValueError("chainage is required")
    if isinstance(value, bool):
        raise ValueError("chainage must be numeric, not a boolean")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        match = _KM_UNIT_RE.match(text)
        if match is None:
            raise ValueError(f"chainage {value!r} is not a number")
        number = float(match.group("num"))
        if (match.group("unit") or "").lower().startswith("m") and not (
            match.group("unit") or ""
        ).lower().startswith("km"):
            number /= 1000.0  # bare metre figure
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"chainage {value!r} is not finite")
    return round(number, 6)


def coerce_line_id(value: Any) -> str:
    """
    Fold a silo running-line spelling onto one of the four canonical line ids.

    Unresolvable values become :data:`UNKNOWN_LINE_ID` rather than raising: an
    unknown line is a *conservative* bucket (it collides with other unknowns),
    and the contract layer above is responsible for rejecting it outright.
    """
    if value is None:
        return UNKNOWN_LINE_ID
    if hasattr(value, "value") and not isinstance(value, str):
        value = getattr(value, "value")
    key = str(value).strip().upper().replace("  ", " ")
    return LINE_ID_ALIASES.get(key, LINE_ID_ALIASES.get(key.replace("_", " "), UNKNOWN_LINE_ID))


def coerce_corridor_id(value: Any) -> str:
    """Fold a silo corridor spelling onto :data:`CORRIDOR_ID`."""
    if value is None or str(value).strip() == "":
        return CORRIDOR_ID
    key = re.sub(r"\s+", " ", str(value).strip().upper())
    return CORRIDOR_ID_ALIASES.get(key, CORRIDOR_ID_ALIASES.get(key.replace("_", "-"), key))


# --------------------------------------------------------------------------- #
#  The 1D span
# --------------------------------------------------------------------------- #
class LinearSpan(BaseModel):
    """
    A half-open kilometre interval on one running line of one corridor.

    This is the **primary spatial key** of BlockFlow. ``[km_start, km_end)`` is
    half-open so that two possessions meeting end-to-end (``... 21.65`` and
    ``21.65 ...``) do not register as a collision.

    The model is intentionally free of any geographic concept - no lat/lon, no
    polygon, no buffer. Coordinates are attached separately, as a read-only
    display projection (:meth:`BlockRequisition.project_display`).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    corridor_id: str = Field(
        default=CORRIDOR_ID,
        validation_alias=AliasChoices("corridor_id", "corridor", "corridor_code"),
        description="LRS corridor key, e.g. WR-MUMBAI-CCG-VR",
    )
    line_id: LineId = Field(
        validation_alias=AliasChoices(
            "line_id", "line", "track_id", "track", "running_line", "road",
        ),
        serialization_alias="line_id",
        description="Running line: one of the four quad lines",
    )
    km_start: float = Field(
        validation_alias=AliasChoices(
            "km_start", "km_from", "from_km", "start_km", "km_start_chainage",
            "chainage_from", "km_from_chainage",
        ),
        serialization_alias="km_start",
        description="Start kilometre post, km from Churchgate (0.000)",
    )
    km_end: float = Field(
        validation_alias=AliasChoices(
            "km_end", "km_to", "to_km", "end_km", "km_end_chainage",
            "chainage_to", "km_to_chainage",
        ),
        serialization_alias="km_end",
        description="End kilometre post, km from Churchgate (59.980 max)",
    )

    # ------------------------------------------------------------------ #
    #  Validation
    # ------------------------------------------------------------------ #
    @field_validator("km_start", "km_end", mode="before")
    @classmethod
    def _quantise_chainage(cls, value: Any) -> Any:
        return coerce_km(value)

    @field_validator("line_id", mode="before")
    @classmethod
    def _canonical_line(cls, value: Any) -> Any:
        line_id = coerce_line_id(value)
        if line_id not in LINE_IDS:
            raise ValueError(
                f"unrecognised line_id {value!r}; expected one of {list(LINE_IDS)}"
            )
        return line_id

    @field_validator("corridor_id", mode="before")
    @classmethod
    def _canonical_corridor(cls, value: Any) -> Any:
        corridor_id = coerce_corridor_id(value)
        if not corridor_id:
            raise ValueError("corridor_id is required")
        return corridor_id

    @model_validator(mode="after")
    def _enforce_lrs_invariants(self) -> "LinearSpan":
        if self.km_start > self.km_end:
            raise ValueError(
                f"km_start ({self.km_start}) is greater than km_end ({self.km_end}); "
                "chainage must increase away from Churchgate"
            )
        for label, km in (("km_start", self.km_start), ("km_end", self.km_end)):
            if not (0.0 <= km <= CORRIDOR_LENGTH_KM):
                raise ValueError(
                    f"{label}={km} km is outside the {CORRIDOR_ID} corridor "
                    f"(0.000 - {CORRIDOR_LENGTH_KM} km)"
                )
        return self

    # ------------------------------------------------------------------ #
    #  Derived
    # ------------------------------------------------------------------ #
    @computed_field  # type: ignore[prop-decorator]
    @property
    def span_km(self) -> float:
        """Length of the interval in kilometres."""
        return round(self.km_end - self.km_start, 3)

    @property
    def lrs_key(self) -> Tuple[str, str]:
        """The ``(corridor_id, line_id)`` pair a 1D comparison is scoped to."""
        return (self.corridor_id, self.line_id)

    @property
    def interval(self) -> Tuple[float, float]:
        """The half-open kilometre interval ``[km_start, km_end)``."""
        return (self.km_start, self.km_end)

    def to_lrs_dict(self) -> Dict[str, Any]:
        """The LRS identity as a plain dict - the solver-facing shape."""
        return {
            "corridor_id": self.corridor_id,
            "line_id": self.line_id,
            "km_start": self.km_start,
            "km_end": self.km_end,
            "span_km": self.span_km,
        }


# --------------------------------------------------------------------------- #
#  Field accessors (model | dict | any object)
# --------------------------------------------------------------------------- #
_MISSING = object()


def _field(source: Any, names: Sequence[str], default: Any = _MISSING) -> Any:
    """First present value among ``names`` on a mapping or an arbitrary object."""
    if source is None:
        if default is _MISSING:
            raise TypeError("cannot read LRS fields from None")
        return default
    for name in names:
        if isinstance(source, Mapping):
            if name in source and source[name] is not None:
                return source[name]
        else:
            value = getattr(source, name, None)
            if value is not None:
                return value
    if default is _MISSING:
        raise KeyError(f"none of {list(names)} present on {type(source).__name__}")
    return default


def line_id_of(source: Any) -> str:
    """
    Canonical ``line_id`` of a span, dict or legacy optimizer row.

    Accepts the LRS spelling (``line_id``) plus the legacy silo spellings the
    HiGHS hand-off still carries (``track_id``, ``line``, ``track``).
    """
    raw = _field(source, ("line_id", "line", "track_id", "track", "running_line"), None)
    return coerce_line_id(raw)


def corridor_id_of(source: Any) -> str:
    """Canonical ``corridor_id``; a single-corridor deployment defaults in."""
    raw = _field(source, ("corridor_id", "corridor", "corridor_code"), None)
    return coerce_corridor_id(raw)


def km_start_of(source: Any) -> float:
    """Start chainage of a span, dict or row."""
    km = _field(source, ("km_start", "km_from", "from_km"), None)
    if km is None:
        raise ValueError(f"no km_start on {type(source).__name__}")
    return coerce_km(km)


def km_end_of(source: Any) -> float:
    """End chainage of a span, dict or row."""
    km = _field(source, ("km_end", "km_to", "to_km"), None)
    if km is None:
        raise ValueError(f"no km_end on {type(source).__name__}")
    return coerce_km(km)


# --------------------------------------------------------------------------- #
#  1D interval algebra - the whole of the "spatial" solver constraint
# --------------------------------------------------------------------------- #
def same_line(a: Any, b: Any) -> bool:
    """True when two spans sit on the same corridor and the same running line."""
    return corridor_id_of(a) == corridor_id_of(b) and line_id_of(a) == line_id_of(b)


def has_overlap(a: Any, b: Any) -> bool:
    """
    True when two 1D chainage intervals physically collide.

    ``has_overlap(A, B)`` iff the spans share a corridor *and* a running line
    *and* ``max(km_start) < min(km_end)``.

    Accepts :class:`LinearSpan` / ``BlockRequisition`` instances, plain dicts
    (including legacy optimizer rows carrying ``track_id``) or anything exposing
    the same attributes. Half-open semantics: two possessions meeting exactly
    end-to-end do **not** overlap - use :func:`gap_km` when adjacency matters.
    """
    if corridor_id_of(a) != corridor_id_of(b):
        return False
    if line_id_of(a) != line_id_of(b):
        return False
    return max(km_start_of(a), km_start_of(b)) < min(km_end_of(a), km_end_of(b))


def overlap_length_km(a: Any, b: Any) -> float:
    """Length of the shared kilometre interval, ``0.0`` when disjoint."""
    if not has_overlap(a, b):
        return 0.0
    shared = min(km_end_of(a), km_end_of(b)) - max(km_start_of(a), km_start_of(b))
    return round(max(0.0, shared), 6)


def gap_km(a: Any, b: Any) -> float:
    """
    Clear kilometre separation between two spans on the same line.

    ``0.0`` when they overlap or touch end-to-end; ``math.inf`` when they are on
    different corridors or running lines (no finite 1D distance exists).
    """
    if not same_line(a, b):
        return math.inf
    return round(max(0.0, max(km_start_of(a), km_start_of(b)) - min(km_end_of(a), km_end_of(b))), 6)


def merge_spans(spans: Iterable[Any]) -> List[Tuple[str, str, float, float]]:
    """
    Coalesce a set of spans into the minimal set of disjoint linear extents.

    Adjacent and overlapping intervals on the same ``(corridor_id, line_id)``
    collapse into one entry - this is what the bundler uses to decide that two
    requisitions 200 m apart are one physical possession site.
    """
    buckets: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}
    for span in spans:
        buckets.setdefault((corridor_id_of(span), line_id_of(span)), []).append(
            (km_start_of(span), km_end_of(span))
        )

    merged: List[Tuple[str, str, float, float]] = []
    for (corridor_id, line_id), intervals in sorted(buckets.items()):
        intervals.sort()
        current_start, current_end = intervals[0]
        for start, end in intervals[1:]:
            if start <= current_end:  # overlapping or touching -> extend
                current_end = max(current_end, end)
            else:
                merged.append((corridor_id, line_id, current_start, current_end))
                current_start, current_end = start, end
        merged.append((corridor_id, line_id, current_start, current_end))
    return merged


def find_collisions(spans: Sequence[Any]) -> List[Tuple[int, int, float]]:
    """
    Every colliding pair in a sequence, as ``(i, j, overlap_km)``.

    Order-independent, category-free and ``O(n^2)`` - the demand set for a
    weekly plan is in the tens, and an intransitive "collides with" relation
    (A-B and B-C overlap while A-C does not) is exactly what a hash-bucketed
    approximation gets wrong.
    """
    collisions: List[Tuple[int, int, float]] = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            if has_overlap(spans[i], spans[j]):
                collisions.append((i, j, overlap_length_km(spans[i], spans[j])))
    return collisions


def conflicting_indices(spans: Sequence[Any]) -> List[Tuple[int, int]]:
    """Colliding index pairs, without the overlap lengths."""
    return [(i, j) for i, j, _ in find_collisions(spans)]


__all__ = [
    "CORRIDOR_ID",
    "CORRIDOR_LENGTH_KM",
    "CORRIDOR_ID_ALIASES",
    "LINE_IDS",
    "LINE_ID_ALIASES",
    "UNKNOWN_LINE_ID",
    "LineId",
    "LinearSpan",
    "coerce_km",
    "coerce_line_id",
    "coerce_corridor_id",
    "line_id_of",
    "corridor_id_of",
    "km_start_of",
    "km_end_of",
    "same_line",
    "has_overlap",
    "overlap_length_km",
    "gap_km",
    "merge_spans",
    "find_collisions",
    "conflicting_indices",
]
