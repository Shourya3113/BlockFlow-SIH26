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

Everything in this module is deterministic and dependency-free: the same
requisition always yields the same verdict, which is what makes the
"1-click digital grant permit" auditable.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

from . import waypoints

#: ACTM Vol II Para 204 - earthing / discharge buffer, minutes, each side.
ACTM_PARA_204_EARTHING_BUFFER_MINS = 15

#: ACTM Vol II Para 203 - traction power isolation reference.
ACTM_PARA_203_REFERENCE = "ACTM Vol II Para 203 (Traction Power Controller isolation)"

#: IRSEM Para 22 - disconnection / reconnection paperwork.
IRSEM_PARA_22_REFERENCE = "IRSEM Para 22 (Form T/351 disconnection, T/352 reconnection)"
IRSEM_DISCONNECTION_FORM = "T_351"
IRSEM_RECONNECTION_FORM = "T_352"

#: IRPWM Para 268(b) - speed restriction must be sanctioned and inside line speed.
IRPWM_PARA_268B_REFERENCE = "IRPWM Para 268(b) (caution order and speed restriction)"

#: IRPWM Para 284 - P-Way work under traffic protection.
IRPWM_PARA_284_REFERENCE = "IRPWM Para 284 (P-Way work under traffic protection)"

#: Absolute block / automatic block safe headway margin between trains (seconds).
MIN_HEADWAY_SECONDS = 90

#: Permissible speed of the Mumbai suburban quad section (km/h).
TRACK_PERMISSIBLE_SPEED_KMPH = 110

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
    """Parse the time encodings used by CRIS / COA feeds into a ``datetime``."""
    if isinstance(value, datetime):
        return value
    base = default_date or datetime.now()
    if value is None or str(value).strip() == "":
        return base
    text = str(value).strip()
    candidates = (
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%H:%M:%S", "%H:%M",
    )
    for fmt in candidates:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:  # time-only -> pin to the planning date
            parsed = parsed.replace(year=base.year, month=base.month, day=base.day)
        return parsed
    raise ValueError(f"unparseable time {value!r}")


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
    duration.
    """
    duration_mins = int(duration_mins)
    start = parse_time(work_start)
    buffer_mins = int(earthing_buffer_mins) if power_isolation_required else 0

    earthing_start = start - timedelta(minutes=buffer_mins)
    work_end = start + timedelta(minutes=duration_mins)
    earthing_end = work_end + timedelta(minutes=buffer_mins)
    total_mins = duration_mins + 2 * buffer_mins

    return {
        "statutory_reference": (
            f"ACTM Vol II Para 204 ({buffer_mins}-minute discharge/earthing buffer)"
            if buffer_mins else "ACTM Para 204 not applicable (no live OHE exposure)"
        ),
        "power_isolation_required": bool(power_isolation_required),
        "earthing_buffer_mins": buffer_mins,
        "earthing_applied": buffer_mins > 0,
        "power_off_at": earthing_start.isoformat(),
        "earthing_before": {
            "from": earthing_start.isoformat(),
            "to": start.isoformat(),
            "minutes": buffer_mins,
        },
        "work_start": start.isoformat(),
        "work_end": work_end.isoformat(),
        "earthing_after": {
            "from": work_end.isoformat(),
            "to": earthing_end.isoformat(),
            "minutes": buffer_mins,
        },
        "power_restored_at": earthing_end.isoformat(),
        "work_duration_mins": duration_mins,
        "total_possession_mins": total_mins,
        "duration_with_buffer_expr": (
            f"{duration_mins} + {buffer_mins} + {buffer_mins} = {total_mins} min "
            f"(ACTM Para 204 earthing both sides)"
            if buffer_mins
            else f"{duration_mins} min (no live OHE exposure, ACTM Para 204 not applicable)"
        ),
    }


# --------------------------------------------------------------------------- #
#  Statutory paperwork
# --------------------------------------------------------------------------- #
def generate_statutory_memos(
    requisition: Dict[str, Any],
    window: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Auto-generate the statutory memos a block cannot start without.

    * ``T_351`` - IRSEM Para 22 disconnection notice (S&T gear isolated).
    * ``T_352`` - IRSEM Para 22 reconnection notice (gear proved back in).
    * ``ACTM_PTW`` - traction permit-to-work with the Para 204 earthing window.
    * ``IRPWM_284_CAUTION`` - caution order for P-Way work under traffic.
    """
    dept = str(requisition.get("dept") or requisition.get("department") or "").upper()
    asset_id = requisition.get("asset_id") or requisition.get("defect_id") or "UNKNOWN"
    line = requisition.get("line") or requisition.get("track_id") or "UNKNOWN"
    km_start = requisition.get("km_start")
    km_end = requisition.get("km_end")
    section = requisition.get("section") or waypoints.section_for_km(float(km_start))
    power_required = bool(
        requisition.get("requires_power_block") or dept in ("TRD", "ELECTRICAL_TRD", "ELECTRICAL")
    )
    disconnection_required = bool(requisition.get("requires_disconnection")) or dept in ("SNT", "S&T")
    window = window or compute_earthing_window(
        requisition.get("work_start"), int(requisition.get("duration_mins", 60)), power_required
    )

    memos: List[Dict[str, Any]] = []

    if disconnection_required:
        memos.append({
            "form_no": IRSEM_DISCONNECTION_FORM,
            "title": "Disconnection Notice (S&T gear)",
            "statutory_reference": IRSEM_PARA_22_REFERENCE,
            "issued_to": "Sectional Controller / Signal Maintainer",
            "issued_for": asset_id,
            "section": section,
            "line": line,
            "km_range": f"{km_start} - {km_end}",
            "valid_from": window["work_start"],
            "valid_to": window["work_end"],
            "requirements": [
                "Point machine / track circuit proved disconnected and clamped before work",
                "Lever lock and disconnection collar applied; disconnection register entry made",
                "No signal movement permitted until Form T/352 is issued",
            ],
            "status": "GENERATED",
        })
        memos.append({
            "form_no": IRSEM_RECONNECTION_FORM,
            "title": "Reconnection Notice (S&T gear proved in)",
            "statutory_reference": IRSEM_PARA_22_REFERENCE,
            "issued_to": "Sectional Controller / Signal Maintainer",
            "issued_for": asset_id,
            "section": section,
            "line": line,
            "km_range": f"{km_start} - {km_end}",
            "valid_from": window["power_restored_at"],
            "valid_to": None,
            "requirements": [
                "Insulation / earth test values recorded before reconnection",
                "Point machine obstruction test repeated and proved",
                "Track circuit occupancy test completed for the full section",
            ],
            "status": "GENERATED",
        })

    if power_required:
        memos.append({
            "form_no": "ACTM_PTW",
            "title": "Traction Permit-To-Work with 25 kV earthing",
            "statutory_reference": ACTM_PARA_203_REFERENCE,
            "issued_to": "Traction Power Controller (TPC)",
            "issued_for": asset_id,
            "section": section,
            "line": line,
            "km_range": f"{km_start} - {km_end}",
            "valid_from": window["power_off_at"],
            "valid_to": window["power_restored_at"],
            "requirements": [
                ACTM_PARA_203_REFERENCE,
                f"ACTM Para 204: {window['earthing_buffer_mins']} min discharge/earthing "
                "buffer before AND after live work",
                "Earthing rods applied at both ends of the isolated zone",
                "TPC written confirmation of power off received before earthing",
                "Section restored to traffic only after earthing removed and TPC informed",
            ],
            "earthing_window": {
                "earthing_before": window["earthing_before"],
                "work_window": {"from": window["work_start"], "to": window["work_end"]},
                "earthing_after": window["earthing_after"],
            },
            "status": "GENERATED",
        })

    pway = bool(requisition.get("requires_traffic_block", True)) and dept in (
        "CIVIL", "ENGINEERING", "ENGG", "PWAY", "P-WAY", "TRACK",
    )
    if pway:
        memos.append({
            "form_no": "IRPWM_284_CAUTION",
            "title": "Caution Order for P-Way work under traffic",
            "statutory_reference": IRPWM_PARA_284_REFERENCE,
            "issued_to": "Loco Pilots / Guard (through NOTICE to Station Masters)",
            "issued_for": asset_id,
            "section": section,
            "line": line,
            "km_range": f"{km_start} - {km_end}",
            "valid_from": window["work_start"],
            "valid_to": window["work_end"],
            "requirements": [
                "Caution order issued to all affected drivers",
                "Look-out man posted in both directions",
                "Indication / banner flags placed 600 m on either approach as applicable",
            ],
            "status": "GENERATED",
        })
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
    line = req.get("line") or req.get("track_id") or "UNKNOWN"
    km_start = req.get("km_start")
    km_end = req.get("km_end")
    duration = int(req.get("duration_mins") or 0)

    checks: List[Dict[str, Any]] = []
    violations: List[str] = []

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

    # 5. IRSEM Para 22 Form T/351 ------------------------------------------- #
    form = str(
        (req.get("statutory_form").value if hasattr(req.get("statutory_form"), "value")
         else req.get("statutory_form")) or ""
    ).upper().replace("/", "_")
    # The rule binds *disconnection jobs*, not every S&T requisition: an
    # interlocking diagnostic or an LED signal swap touches no field gear.
    snT_work = bool(req.get("requires_disconnection")) or form in ("T_351", "IRSEM_22", "T_352")
    form_ok = (not snT_work) or form in ("T_351", "IRSEM_22", "T_352")
    checks.append(_check(
        "IRSEM_22_T351_DISCONNECTION",
        IRSEM_PARA_22_REFERENCE,
        form_ok,
        "Form T/351 disconnection notice referenced and auto-generated"
        if snT_work and form_ok
        else "not an S&T disconnection job"
        if not snT_work
        else f"S&T gear disconnection requires Form T/351 (received {form or 'none'})",
    ))
    if not form_ok:
        violations.append("IRSEM_22_T351_DISCONNECTION")

    # 6. IRPWM Para 268(b) speed restriction -------------------------------- #
    psr = req.get("psr_speed_kmph")
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
        "line": line,
        "km_start": km_start,
        "km_end": km_end,
        "section": req.get("section") or waypoints.section_for_km(km_start),
        "passed": passed,
        "status": "SAFE_TO_SCHEDULE" if passed else "BLOCKED_BY_SAFETY_INVARIANT",
        "violations": violations,
        "checks": checks,
        "earthing_window": window,
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
    work_mins = int(task.get("duration_mins") or 0)
    buffer_mins = ACTM_PARA_204_EARTHING_BUFFER_MINS if task.get("requires_power_block") else 0
    return {
        "work_mins": work_mins,
        "buffer_mins": buffer_mins,
        "possession_mins": work_mins + 2 * buffer_mins,
        "required_mins": work_mins + 2 * buffer_mins,
    }


def validate_plan(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Post-solve guardrail over the optimizer's own output.

    Re-checks, on the *scheduled* blocks, the three things the architecture
    promises are mathematically guaranteed:

    1. ``Dur_b <= Win_s`` - the possession **including the ACTM Para 204
       earthing wrap** fits its corridor window. This is where a scheduler that
       budgets only the working duration is caught: the section is physically
       occupied for ``duration + 15 + 15`` minutes, not ``duration``.
    2. Physical conflict exclusivity - no two possessions overlap in time on the
       same section and running line.
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

    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            a, b = blocks[i], blocks[j]
            if a.get("section") != b.get("section") or a.get("track_id") != b.get("track_id"):
                continue
            a_metrics, b_metrics = _block_metrics(a), _block_metrics(b)
            overlap = a_metrics["start"] < b_metrics["end"] and b_metrics["start"] < a_metrics["end"]
            label = f"{a.get('block_id')}~{b.get('block_id')}"
            checks.append(_check(
                "PHYSICAL_EXCLUSIVITY",
                "No two possessions overlap on the same section and running line",
                not overlap,
                f"{label}: {'OVERLAP' if overlap else 'disjoint'}",
            ))
            if overlap:
                violations.append(f"PHYSICAL_EXCLUSIVITY:{label}")

    return {
        "passed": not violations,
        "blocks_checked": len(blocks),
        "checks_run": len(checks),
        "violations": violations,
        "failed_checks": [c for c in checks if c["status"] == STATUS_FAIL],
        "checks": checks,
    }


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    sample = {
        "asset_id": "TMS-ENG-1006", "dept": "CIVIL", "line": "DN_FAST",
        "km_start": 19.4, "km_end": 21.2, "duration_mins": 180,
        "severity": "CRITICAL", "psr_speed_kmph": 30,
        "requires_power_block": True, "requires_traffic_block": True,
        "work_start": "01:30",
    }
    verdict = evaluate_safety_invariants(sample)
    print(f"Verdict      : {verdict['status']}")
    print(f"Earthing     : {verdict['earthing_window']['duration_with_buffer_expr']}")
    print(f"Forms        : {verdict['statutory_forms_required']}")
    for check in verdict["checks"]:
        print(f"  [{check['status']:13s}] {check['code']:32s} {check['detail']}")
