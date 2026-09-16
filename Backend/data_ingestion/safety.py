"""
safety.py - Statutory Safety Invariants & Electrical Earthing Enforcement

Hard-coded Indian Railways rules that the optimizer is **not allowed to relax**,
applied to every requisition before it can become a scheduled block.

Rules enforced here
-------------------
*   **ACTM Vol II Para 203** - a traction power block (isolation) must be taken
    from the Traction Power Controller before any work inside the 25 kV OHE
    danger zone, and traffic must be stopped for that period.
*   **ACTM Vol II Para 204** - the mandatory **15-minute physical discharge /
    earthing buffer both before and after** live OHE work, while the section is
    earthed. A possession is therefore ``duration + 15 + 15`` minutes long, and
    the traction power restoration time is derived, never guessed.
*   **IRSEM Para 22** - S&T disconnection requires a **Form T/351** notice
    before disconnection and a **Form T/352** reconnection notice after the work
    is proved complete.
*   **IRPWM Para 268(b)** - a permanent speed restriction (PSR) must be inside
    the permissible speed of the section, and the work must be done under the
    caution order.
*   **Corridor boundary** - chainage must lie inside the surveyed 59.98 km
    Churchgate-Virar corridor.
*   **LRS identity** - the running line must resolve to one of the four quad
    lines, and the span must be a valid kilometre interval.

Every spatial judgement here is 1D. A possession is a half-open chainage
interval on a named running line, and two possessions conflict exactly when
those intervals collide on the same line - see
:func:`Backend.data_ingestion.lrs.has_overlap`. There is no polygon, no buffer
radius and no geometric intersection anywhere in this module, so there is no
float tolerance for a schedule to hide behind.

Everything in this module is deterministic and dependency-free: the same
requisition always yields the same verdict, which is what makes the
"1-click digital grant permit" auditable.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Union

from . import lrs, permits, waypoints

# The statutory constants and the earthing arithmetic live in ``permits`` - the
# module that actually raises the paperwork. They are re-exported here because
# the invariants, the API and the guardrail have always imported them from
# safety, and duplicating the number 15 in two modules is exactly how a buffer
# drifts.
ACTM_PARA_204_EARTHING_BUFFER_MINS = permits.ACTM_PARA_204_EARTHING_BUFFER_MINS
ACTM_PARA_203_REFERENCE = permits.ACTM_PARA_203_REFERENCE
ACTM_PARA_204_REFERENCE = permits.ACTM_PARA_204_REFERENCE
IRSEM_PARA_22_REFERENCE = permits.IRSEM_PARA_22_REFERENCE
IRSEM_DISCONNECTION_FORM = permits.IRSEM_DISCONNECTION_FORM
IRSEM_RECONNECTION_FORM = permits.IRSEM_RECONNECTION_FORM
IRPWM_PARA_268B_REFERENCE = permits.IRPWM_PARA_268B_REFERENCE
IRPWM_PARA_284_REFERENCE = permits.IRPWM_PARA_284_REFERENCE
TRACK_PERMISSIBLE_SPEED_KMPH = permits.TRACK_PERMISSIBLE_SPEED_KMPH

#: Absolute block / automatic block safe headway margin between trains (seconds).
MIN_HEADWAY_SECONDS = 90

#: Thermovision hotspot escalation threshold on 25 kV OHE clamps (deg C).
OHE_HOTSPOT_ALERT_C = 80.0

#: A 25 kV possession longer than this needs multi-post isolation (advisory).
MULTI_POST_ISOLATION_SPAN_KM = 10.0

CORRIDOR_LENGTH_KM = waypoints.CORRIDOR_LENGTH_KM

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_ADVISORY = "ADVISORY"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

DEPARTMENT_SCALE_CRITICAL = "CRITICAL"
DEPARTMENT_SCALE_WARNING = "WARNING"


class SafetyInvariantError(ValueError):
    """Raised when a requisition would violate a hard statutory rule."""


# --------------------------------------------------------------------------- #
#  Time helpers
# --------------------------------------------------------------------------- #
def parse_time(value: Union[str, datetime, None], default_date: Optional[datetime] = None) -> datetime:
    """
    Parse the time encodings used by CRIS / COA feeds into a ``datetime``.

    Thin re-export of :func:`Backend.data_ingestion.permits.parse_timestamp` so
    the block window is parsed by exactly one function - the one the permit
    generator also uses. A time-only value (``"01:30"``) is pinned to
    ``default_date``.
    """
    return permits.parse_timestamp(value, default_date=default_date)


# --------------------------------------------------------------------------- #
#  ACTM Para 204 earthing window
# --------------------------------------------------------------------------- #
def compute_earthing_window(
    work_start: Union[str, datetime, None],
    duration_mins: int,
    power_isolation_required: bool = True,
    earthing_buffer_mins: int = ACTM_PARA_204_EARTHING_BUFFER_MINS,
) -> Dict[str, Any]:
    """
    Build the full possession window including the ACTM Para 204 buffers.

    ``[earth] 15 min [work duration] 15 min [deconsecrate]``

    Returns ISO-8601 timestamps plus the total possession minutes so the
    optimizer sees the *real* footprint on the corridor, not just the working
    duration. The arithmetic itself lives on
    :meth:`Backend.data_ingestion.permits.EarthingBuffer`, which *proves* it
    holds (``work + 2 x 15`` and an ordered window) instead of merely computing
    it - so a memo and a verdict can never disagree about the same possession.
    """
    return permits.build_earthing_buffer(
        work_start,
        int(duration_mins),
        power_isolation_required,
        buffer_mins=int(earthing_buffer_mins),
    ).to_legacy_window()


# --------------------------------------------------------------------------- #
#  Linear lookup helpers
# --------------------------------------------------------------------------- #
def _section_for_km_safe(km: Any) -> Optional[str]:
    """
    Sectional bookmark for a chainage, or ``None`` when it cannot be resolved.

    A pure 1D lookup on the kilometre post - section codes are a reporting
    convenience and play no part in deciding a collision.
    """
    try:
        return waypoints.section_for_km(km)
    except (TypeError, ValueError):
        return None


def _read(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """
    First present value among ``names``.

    Lets every invariant accept a :class:`BlockRequisition` dump (which carries
    the canonical engineering-attribute names) *and* a legacy optimizer row
    (which carries the pre-refactor spellings) without a field-name table per
    check.
    """
    for name in names:
        value = row.get(name)
        if value is not None:
            return value
    return default


# --------------------------------------------------------------------------- #
#  Statutory paperwork
# --------------------------------------------------------------------------- #
def generate_statutory_memos(
    requisition: Dict[str, Any],
    window: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    The statutory instruments a requisition will attract, as plain dicts.

    Delegates to :func:`Backend.data_ingestion.permits.build_permits_for_requisition`,
    which owns the prefill logic, so there is exactly one implementation of what
    a Form T/351 or a Permit-to-Work contains.

    The returned windows are **provisional**: no block has been allocated yet,
    so the memo numbers and gear lists are indicative and every instrument comes
    back ``PREFILLED`` and unsigned. For the real instruments, generate from the
    scheduled block with ``permits.build_permits_for_block``.

    Which instruments appear is *derived* - from the work type, the department
    and the declared operating context - never read off a ``statutory_form``
    field on the incoming row. A requisition cannot cite a memo that the
    schedule has not yet created.
    """
    builder = permits.build_permits_for_requisition(dict(requisition))
    memos = [memo.to_legacy_memo_dict() for memo in builder.memos]
    if window is not None:
        # A caller that already computed the ACTM Para 204 window keeps it, so
        # the verdict's earthing window and its memos stay on one timeline.
        for memo in memos:
            if memo["form_no"] == permits.FormCode.T_352.value:
                memo["valid_from"] = window["power_restored_at"]
            elif memo["form_no"] == permits.FormCode.ACTM_PTW.value:
                memo["valid_from"] = window["power_off_at"]
                memo["valid_to"] = window["power_restored_at"]
            else:
                memo["valid_from"] = window["work_start"]
                memo["valid_to"] = window["work_end"]
    return memos


# --------------------------------------------------------------------------- #
#  Invariant checks
# --------------------------------------------------------------------------- #
def _check(code: str, rule: str, passed: bool, detail: str,
           severity: str = DEPARTMENT_SCALE_CRITICAL,
           advisory: bool = False) -> Dict[str, Any]:
    if advisory:
        status = STATUS_ADVISORY
    else:
        status = STATUS_PASS if passed else STATUS_FAIL
    return {
        "code": code,
        "rule": rule,
        "status": status,
        "severity": severity,
        "detail": detail,
    }


def evaluate_safety_invariants(requisition: Dict[str, Any]) -> Dict[str, Any]:
    """
    ACI-independent safety verdict for one requisition.

    Accepts either a :class:`BlockRequisition` dump (``dept``) or a legacy
    optimizer dict (``department``), and returns a JSON-serialisable verdict
    containing the ACTM Para 204 window, the generated memoranda and a per-rule
    PASS/FAIL ledger.
    """
    req = dict(requisition)
    dept_raw = req.get("dept") or req.get("department") or ""
    dept = str(dept_raw.value if hasattr(dept_raw, "value") else dept_raw).upper()
    asset_id = req.get("asset_id") or req.get("defect_id") or "UNKNOWN"
    # The running line comes from the LRS field first, then the legacy optimiser
    # spellings, and is bucketed as UNKNOWN (which collides conservatively)
    # rather than being silently dropped.
    line_id = lrs.line_id_of(req)
    corridor_id = lrs.corridor_id_of(req)
    line = line_id
    km_start = req.get("km_start")
    km_end = req.get("km_end")
    # Both the new engineering-attribute name and the legacy optimizer spelling
    # are accepted, so this engine can evaluate a contract dump or a legacy row.
    duration = int(_read(req, "requested_duration_mins", "duration_mins", default=0) or 0)

    checks: List[Dict[str, Any]] = []
    violations: List[str] = []

    # 0. LRS identity ------------------------------------------------------- #
    line_ok = line_id in lrs.LINE_IDS
    checks.append(_check(
        "LRS_LINE_ID",
        "Running line is one of the four quad lines on the LRS corridor",
        line_ok,
        f"line_id={line_id} on corridor_id={corridor_id}" if line_ok
        else f"unresolvable running line {req.get('line_id') or req.get('line') or req.get('track_id')!r}; "
        f"expected one of {list(lrs.LINE_IDS)}",
    ))
    if not line_ok:
        violations.append("LRS_LINE_ID")

    # 1. Corridor boundary -------------------------------------------------- #
    inside = waypoints.validate_chainage(km_start) and waypoints.validate_chainage(km_end)
    checks.append(_check(
        "CORRIDOR_BOUNDARY",
        "Churchgate-Virar surveyed chainage 0.000 - 59.980 km",
        inside,
        f"km_start={km_start}, km_end={km_end} both inside corridor"
        if inside else f"chainage outside 0.000-{CORRIDOR_LENGTH_KM} km corridor",
    ))
    if not inside:
        violations.append("CORRIDOR_BOUNDARY")

    # 2. Chainage order ----------------------------------------------------- #
    try:
        ordered = float(km_start) <= float(km_end)
    except (TypeError, ValueError):
        ordered = False
    checks.append(_check(
        "CHAINAGE_ORDER",
        "km_start <= km_end (chainage increases away from Churchgate)",
        ordered,
        f"{km_start} <= {km_end}" if ordered else f"inverted chainage {km_start} > {km_end}",
    ))
    if not ordered:
        violations.append("CHAINAGE_ORDER")

    # 3. Duration ----------------------------------------------------------- #
    duration_ok = 15 <= duration <= 480
    checks.append(_check(
        "POSSESSION_DURATION",
        "15 min <= duration <= 480 min single possession",
        duration_ok,
        f"duration {duration} min",
    ))
    if not duration_ok:
        violations.append("POSSESSION_DURATION")

    # 4. ACTM Para 203 isolation / Para 204 earthing ------------------------ #
    # The TDMS default (every OHE job needs a power block) is applied during
    # normalisation, so by this point the flag is explicit and must be trusted:
    # an earth-continuity audit outside the danger zone legitimately carries no
    # isolation, and forcing one on would drive a false P1 escalation.
    power_required = bool(req.get("requires_power_block"))
    traffic_blocked = bool(req.get("requires_traffic_block", True))
    window = compute_earthing_window(req.get("work_start"), duration, power_required)

    checks.append(_check(
        "ACTM_203_POWER_ISOLATION",
        ACTM_PARA_203_REFERENCE,
        (power_required and traffic_blocked) or not power_required,
        "TPC isolation taken; section blocked to traffic"
        if power_required and traffic_blocked
        else "no 25 kV exposure on this requisition"
        if not power_required
        else "power isolation demanded but traffic block not taken - LOTO cannot be assured",
    ))
    if power_required and not traffic_blocked:
        violations.append("ACTM_203_POWER_ISOLATION")

    buffer_ok = (not power_required) or (
        window["earthing_buffer_mins"] == ACTM_PARA_204_EARTHING_BUFFER_MINS
        and window["total_possession_mins"] == duration + 2 * ACTM_PARA_204_EARTHING_BUFFER_MINS
    )
    checks.append(_check(
        "ACTM_204_EARTHING_BUFFER",
        "ACTM Vol II Para 204 - 15-min discharge/earthing before AND after live work",
        buffer_ok,
        f"possession = {window['duration_with_buffer_expr']} (work + 15 before + 15 after)"
        if power_required
        else "not applicable - no live OHE exposure",
    ))
    if not buffer_ok:
        violations.append("ACTM_204_EARTHING_BUFFER")

    # 5. Permit requirements: DERIVED, never demanded (IRSEM Para 22) -------- #
    # There is deliberately no rule here of the form "a disconnection job must
    # cite Form T/351". A T/351 is a legal instrument a Sectional Controller
    # issues *after* the block is authorised; a requisition arriving weeks ahead
    # cannot hold one, so gating on it rejected conforming data and hid the real
    # question. What the input owes us is enough engineering detail to raise the
    # paperwork later - and for Form T/351 that means naming the gear.
    permit_requirements = permits.derive_permit_requirements(req)
    identified_gears = permits.collect_affected_gears(
        [req], line_id, corridor_id, km_hint=km_start
    )
    gear_ok = not permits.requires_gear_identification(permit_requirements, identified_gears)
    checks.append(_check(
        "DISCONNECTION_GEAR_IDENTIFIED",
        "Work that puts signalling gear out of service must name the gear "
        "(Form T/351 needs it; the notice itself is generated after scheduling)",
        gear_ok,
        f"gear identified for Form T/351: {', '.join(g.gear_id for g in identified_gears)}"
        if permit_requirements.requires_disconnection and gear_ok
        else "not a gear-disconnection job; no Form T/351 will be raised"
        if not permit_requirements.requires_disconnection
        else "gear disconnection required but no gear_id/point_no supplied: "
        "Form T/351 cannot be prefilled with the affected gear",
    ))
    if not gear_ok:
        violations.append("DISCONNECTION_GEAR_IDENTIFIED")

    checks.append(_check(
        "PERMIT_REQUIREMENTS_DERIVED",
        "Statutory instruments are derived from the engineering attributes and "
        "generated downstream; none is required as an input",
        True,
        "will raise " + (
            ", ".join(permit_requirements.forms) if permit_requirements.forms
            else "no statutory instrument (no disconnection, isolation or caution order applies)"
        ) + (
            f"; silo cited {', '.join(f.value for f in permit_requirements.cited_forms)} (provenance only)"
            if permit_requirements.cited_forms else ""
        ),
        severity=DEPARTMENT_SCALE_WARNING,
        advisory=True,
    ))

    # 6. IRPWM Para 268(b) speed restriction -------------------------------- #
    psr = _read(req, "speed_restriction_psr", "psr_speed_kmph")
    psr_ok = True
    if psr is not None:
        try:
            psr_val = int(psr)
            psr_ok = 0 < psr_val <= TRACK_PERMISSIBLE_SPEED_KMPH
        except (TypeError, ValueError):
            psr_ok = False
    checks.append(_check(
        "IRPWM_268B_SPEED_RESTRICTION",
        f"{IRPWM_PARA_268B_REFERENCE} - PSR <= {TRACK_PERMISSIBLE_SPEED_KMPH} km/h",
        psr_ok,
        f"PSR {psr} km/h inside line speed" if psr is not None and psr_ok
        else "no speed restriction imposed" if psr is None
        else f"PSR {psr} km/h is outside the permissible section speed",
    ))
    if not psr_ok:
        violations.append("IRPWM_268B_SPEED_RESTRICTION")

    # 7. 25 kV OHE hotspot escalation --------------------------------------- #
    hotspot = req.get("hotspot_temp_c")
    hotspot_ok = True
    if hotspot is not None:
        try:
            hotspot_ok = float(hotspot) >= OHE_HOTSPOT_ALERT_C
        except (TypeError, ValueError):
            hotspot_ok = False
    checks.append(_check(
        "OHE_HOTSPOT_ESCALATION",
        f"Thermovision hotspot > {OHE_HOTSPOT_ALERT_C:.0f} C escalated to P1",
        hotspot_ok,
        f"hotspot {hotspot} C requires immediate P1 treatment" if hotspot is not None and hotspot_ok
        else "no thermovision finding attached" if hotspot is None
        else f"hotspot {hotspot} C is below the escalation threshold",
        advisory=hotspot is None,
    ))
    if not hotspot_ok:
        violations.append("OHE_HOTSPOT_ESCALATION")

    # 8. Feeding post containment (advisory) -------------------------------- #
    advisory = False
    detail = "not a TDMS isolation"
    if power_required:
        try:
            start_post = waypoints.feeding_post_for_km(float(km_start))["feeding_post"]
            end_post = waypoints.feeding_post_for_km(float(km_end))["feeding_post"]
            span = float(km_end) - float(km_start)
            if start_post == end_post:
                detail = f"possession contained in feeding post {start_post}"
            elif span > MULTI_POST_ISOLATION_SPAN_KM:
                detail = (
                    f"span {span:.2f} km crosses feeding posts {start_post}->{end_post}; "
                    "multi-post isolation and a sectional TPC authorisation are required"
                )
                advisory = True
            else:
                detail = f"crosses feeding posts {start_post}->{end_post} (within one boundary window)"
        except (TypeError, ValueError):
            detail = "chainage could not be resolved against feeding post boundaries"
            advisory = True
    checks.append(_check(
        "TRD_FEEDING_POST_CONTAINMENT",
        "25 kV possession contained within traction feeding-post / isolator boundaries",
        not advisory,
        detail,
        severity=DEPARTMENT_SCALE_WARNING,
        advisory=advisory,
    ))

    # 9. Headway margin (informational - enforced by the route conflict matrix)
    checks.append(_check(
        "ABS_HEADWAY_MARGIN",
        f"Automatic block headway >= {MIN_HEADWAY_SECONDS} s around the possession",
        True,
        f"{MIN_HEADWAY_SECONDS} s headway margin to be verified by the route conflict matrix",
        severity=DEPARTMENT_SCALE_WARNING,
        advisory=True,
    ))

    passed = not violations
    verdict: Dict[str, Any] = {
        "asset_id": asset_id,
        "dept": dept,
        # ---- LRS identity of the evaluated span ---------------------------- #
        "corridor_id": corridor_id,
        "line_id": line_id,
        "line": line,
        "km_start": km_start,
        "km_end": km_end,
        "section": req.get("section") or _section_for_km_safe(km_start),
        "passed": passed,
        "status": "SAFE_TO_SCHEDULE" if passed else "BLOCKED_BY_SAFETY_INVARIANT",
        "violations": violations,
        "checks": checks,
        "earthing_window": window,
        # ---- derived statutory requirements (the paper trail to come) ------ #
        "permit_requirements": permit_requirements.model_dump(mode="json"),
        "required_forms": permit_requirements.forms,
        "referenced_form": (
            permit_requirements.cited_forms[0].value if permit_requirements.cited_forms else None
        ),
        # Provisional instruments, so a reviewer can see exactly what will be
        # raised once the block is scheduled. Always PREFILLED and unsigned.
        "statutory_forms_required": [m["form_no"] for m in generate_statutory_memos(req, window)],
        "memos": generate_statutory_memos(req, window),
    }
    return verdict


def assert_safe(requisition: Dict[str, Any]) -> Dict[str, Any]:
    """Raise :class:`SafetyInvariantError` unless the requisition passes."""
    verdict = evaluate_safety_invariants(requisition)
    if not verdict["passed"]:
        failed = [c for c in verdict["checks"] if c["status"] == STATUS_FAIL]
        detail = "; ".join(f"{c['code']}: {c['detail']}" for c in failed)
        raise SafetyInvariantError(
            f"{verdict['asset_id']} violates statutory invariants -> {detail}"
        )
    return verdict


def safety_summary(verdicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate a list of verdicts for the KPIs endpoint / execution trace."""
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v["passed"])
    failed_codes: Dict[str, int] = {}
    for verdict in verdicts:
        for check in verdict["checks"]:
            if check["status"] == STATUS_FAIL:
                failed_codes[check["code"]] = failed_codes.get(check["code"], 0) + 1
    return {
        "requisitions_checked": total,
        "passed": passed,
        "failed": total - passed,
        "compliance_pct": round(passed / total * 100.0, 1) if total else 100.0,
        "earthing_buffer_mins": ACTM_PARA_204_EARTHING_BUFFER_MINS,
        "failed_checks": failed_codes,
        "solver_ready": total - passed == 0,
    }


# --------------------------------------------------------------------------- #
#  Plan-level guardrail
# --------------------------------------------------------------------------- #
def possession_of(task: Dict[str, Any]) -> Dict[str, int]:
    """
    Corridor occupancy of one task/bundle member, with its own arithmetic exposed.

    Two input conventions are understood, and both are *re-derived* rather than
    trusted:

    * Marker fields present (``possession_duration_mins`` from the ingestion
      hand-off) - the value is used, but must equal ``work + 2 x buffer``.
    * Marker fields absent (a raw optimizer bundle) - the ACTM Para 204 wrap is
      added from the task's own ``requires_power_block`` flag.
    """
    if task.get("possession_duration_mins") is not None:
        buffer_mins = int(task.get("earthing_buffer_mins") or 0)
        possession = int(task["possession_duration_mins"])
        work_mins = int(task.get("work_duration_mins") or max(0, possession - 2 * buffer_mins))
        return {
            "work_mins": work_mins,
            "buffer_mins": buffer_mins,
            "possession_mins": possession,
            "required_mins": work_mins + 2 * buffer_mins,
        }
    work_mins = int(_read(task, "requested_duration_mins", "duration_mins", default=0) or 0)
    buffer_mins = ACTM_PARA_204_EARTHING_BUFFER_MINS if task.get("requires_power_block") else 0
    return {
        "work_mins": work_mins,
        "buffer_mins": buffer_mins,
        "possession_mins": work_mins + 2 * buffer_mins,
        "required_mins": work_mins + 2 * buffer_mins,
    }


def _lrs_span(block: Dict[str, Any], tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    1D chainage extent of a scheduled block.

    Prefers the block's own ``corridor_id``/``line_id``/``km_start``/``km_end``
    (written by the LRS-aware optimizer) and otherwise takes the union of its
    bundled tasks - so a block dict that only ever carried ``section`` and
    ``track_id`` can still be assessed in the chainage domain. Returns an empty
    dict when no chainage is present at all, which the caller reports as an
    unresolved-extent violation rather than silently waving the pair through.
    """
    line_raw = block.get("line_id") or block.get("track_id") or block.get("line")
    corridor_raw = block.get("corridor_id") or block.get("corridor")
    starts = [] if block.get("km_start") is None else [block["km_start"]]
    ends = [] if block.get("km_end") is None else [block["km_end"]]

    for task in tasks:
        if task.get("km_start") is not None:
            starts.append(task["km_start"])
        if task.get("km_end") is not None:
            ends.append(task["km_end"])
        if line_raw is None:
            line_raw = task.get("line_id") or task.get("track_id") or task.get("line")
        if corridor_raw is None:
            corridor_raw = task.get("corridor_id")

    if not starts or not ends:
        return {}
    try:
        return {
            "corridor_id": lrs.coerce_corridor_id(corridor_raw),
            "line_id": lrs.coerce_line_id(line_raw),
            "km_start": min(lrs.coerce_km(value) for value in starts),
            "km_end": max(lrs.coerce_km(value) for value in ends),
        }
    except (ValueError, TypeError):
        return {}


def validate_plan(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Post-solve guardrail over the optimizer's own output.

    Re-checks, on the *scheduled* blocks, the three things the architecture
    promises are mathematically guaranteed:

    1. ``Dur_b <= Win_s`` - the possession **including the ACTM Para 204
       earthing wrap** fits its corridor window. This is where a scheduler that
       budgets only the working duration is caught: the section is physically
       occupied for ``duration + 15 + 15`` minutes, not ``duration``.
    2. **1D chainage exclusivity** - no two possessions whose kilometre
       intervals collide on the same running line overlap in time. The test is
       :func:`lrs.has_overlap` on ``(corridor_id, line_id, km_start, km_end)``.
       Note what it deliberately does *not* say: two jobs in the same
       ``section`` but different chainage are **not** a conflict, which is why
       this guardrail is keyed on kilometre posts rather than on section codes.
    3. ACTM Para 204 arithmetic - the scheduled possession is at least
       ``max(work + 2 x buffer)`` over the bundled tasks, i.e. the earthing
       buffer of every job it wraps is actually paid for.
    """
    checks: List[Dict[str, Any]] = []
    violations: List[str] = []

    def _block_metrics(block: Dict[str, Any]) -> Dict[str, Any]:
        date = str(block.get("date", ""))
        raw_start = f"{date} {block.get('start_time', '00:00')}" if date else block.get("start_time")
        start = parse_time(raw_start)
        tasks = list(block.get("tasks_bundled") or [])
        if not tasks:
            tasks = [{
                "duration_mins": block.get("allocated_duration_mins") or block.get("duration_mins") or 0,
                "requires_power_block": bool(block.get("requires_power_block")),
                "possession_duration_mins": block.get("possession_duration_mins"),
                "earthing_buffer_mins": block.get("earthing_buffer_mins"),
                "work_duration_mins": block.get("work_duration_mins"),
            }]
        per_task = [possession_of(task) for task in tasks]
        allocated = int(block.get("allocated_duration_mins") or 0)
        possession = max([m["possession_mins"] for m in per_task] + [allocated])
        return {
            "start": start,
            "end": start + timedelta(minutes=possession),
            "possession_mins": possession,
            "allocated_mins": allocated,
            "required_mins": max(m["required_mins"] for m in per_task),
            "buffer_mins": max(m["buffer_mins"] for m in per_task),
            # The 1D extent the exclusivity invariant is evaluated on.
            "span": _lrs_span(block, tasks),
        }

    for index, block in enumerate(blocks):
        label = block.get("block_id") or f"block[{index}]"
        metrics = _block_metrics(block)
        window_mins = int(block.get("slot_window_mins") or metrics["possession_mins"])
        fits = metrics["possession_mins"] <= window_mins
        checks.append(_check(
            "DURATION_CAPACITY",
            "Dur_b (work + ACTM 204 earthing buffers) <= available window Win_s",
            fits,
            f"{label}: {metrics['possession_mins']} min possession (allocated "
            f"{metrics['allocated_mins']} + {metrics['buffer_mins']} min buffer) vs "
            f"{window_mins} min window",
        ))
        if not fits:
            violations.append(f"DURATION_CAPACITY:{label}")

        envelope_ok = metrics["possession_mins"] + 1e-9 >= metrics["required_mins"]
        checks.append(_check(
            "POWER_ISOLATION_ENVELOPE",
            "PowerIsolation(TRD) >= Duration(P-Way) + 30 min (ACTM Para 204)",
            envelope_ok,
            f"{label}: scheduled possession {metrics['possession_mins']} min vs required "
            f"{metrics['required_mins']} min",
        ))
        if not envelope_ok:
            violations.append(f"POWER_ISOLATION_ENVELOPE:{label}")

    metrics_by_index = [_block_metrics(block) for block in blocks]

    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            a, b = blocks[i], blocks[j]
            a_metrics, b_metrics = metrics_by_index[i], metrics_by_index[j]
            label = f"{a.get('block_id')}~{b.get('block_id')}"

            # Different corridor or different running line -> no 1D interaction
            # is possible, so there is nothing for this pair to answer for.
            if not a_metrics["span"] or not b_metrics["span"]:
                checks.append(_check(
                    "LRS_EXTENT_RESOLVED",
                    "Every scheduled block resolves to a 1D chainage interval",
                    False,
                    f"{label}: block carries no corridor_id/line_id/km_start/km_end "
                    "and none of its bundled tasks do either",
                    severity=DEPARTMENT_SCALE_WARNING,
                ))
                violations.append(f"LRS_EXTENT_RESOLVED:{label}")
                continue
            if not lrs.same_line(a_metrics["span"], b_metrics["span"]):
                continue

            # 1D chainage collision: the whole of the spatial test.
            shared_km = lrs.overlap_length_km(a_metrics["span"], b_metrics["span"])
            time_overlap = (
                a_metrics["start"] < b_metrics["end"]
                and b_metrics["start"] < a_metrics["end"]
            )
            conflict = shared_km > 0.0 and time_overlap

            a_span, b_span = a_metrics["span"], b_metrics["span"]
            detail = (
                f"{label}: {a_span['line_id']} km {a_span['km_start']}-{a_span['km_end']} vs "
                f"km {b_span['km_start']}-{b_span['km_end']} -> "
            )
            if shared_km > 0.0 and time_overlap:
                detail += f"CONFLICT over {shared_km:.3f} km in an overlapping time window"
            elif shared_km > 0.0:
                detail += f"{shared_km:.3f} km of shared chainage, disjoint in time"
            else:
                detail += f"disjoint chainage ({lrs.gap_km(a_span, b_span):.3f} km apart)"

            checks.append(_check(
                "CHAINAGE_INTERVAL_EXCLUSIVITY",
                "No two possessions whose 1D chainage intervals collide on the same "
                "running line occupy overlapping time windows",
                not conflict,
                detail,
            ))
            if conflict:
                violations.append(f"CHAINAGE_INTERVAL_EXCLUSIVITY:{label}")

    return {
        "passed": not violations,
        "blocks_checked": len(blocks),
        "checks_run": len(checks),
        "violations": violations,
        "failed_checks": [c for c in checks if c["status"] == STATUS_FAIL],
        "checks": checks,
    }


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    # An engineering requisition as a silo would submit it: what work, where,
    # how long, how urgent. No form number - a memo cannot exist yet.
    sample = {
        "asset_id": "TMS-ENG-1006", "dept": "CIVIL", "line_id": "DN_FAST",
        "km_start": 19.4, "km_end": 21.2, "requested_duration_mins": 180,
        "work_type": "TRACK_TAMPING", "fault_code": "SD_INDEX_HIGH",
        "urgency": "CRITICAL", "speed_restriction_psr": 30,
        "requires_power_block": True, "requires_traffic_block": True,
        "work_start": "01:30",
    }
    verdict = evaluate_safety_invariants(sample)
    print(f"Verdict      : {verdict['status']}")
    print(f"Earthing     : {verdict['earthing_window']['duration_with_buffer_expr']}")
    print(f"Will raise   : {verdict['required_forms']}")
    print(f"Cited on row : {verdict['referenced_form']} (provenance only)")
    for check in verdict["checks"]:
        print(f"  [{check['status']:13s}] {check['code']:32s} {check['detail']}")

    from .permits import build_permits_for_requisition

    permit = build_permits_for_requisition(sample)
    print(f"\nPermit       : {permit.permit_id} [{permit.status.value}]")
    print(f"Window       : {permit.window_start:%Y-%m-%d %H:%M} -> {permit.window_end:%Y-%m-%d %H:%M}")
    print(f"Earthing     : {permit.earthing.arithmetic}")
    print(f"Memos        : {permit.memo_index}")
    print(f"To be signed : {permit.unsigned_authorities}")
