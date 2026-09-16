"""
coa_data.py - Control Office Application (COA) & Corridor Traffic Model
Models Train Time Tables, Freight paths, and Corridor Block Availability Windows
for Indian Railways Control Office.
"""

from datetime import timedelta
from typing import List, Dict, Any
from .tms_data import SECTIONS, TRACKS

# Typical corridor windows available for maintenance blocks on busy routes
CORRIDOR_SLOTS = [
    {
        "slot_id": "SLOT-NIGHT-01",
        "name": "Primary Night Mega Block",
        "start_time": "01:15",
        "end_time": "04:30",
        "duration_mins": 195,
        "traffic_impact_factor": 0.15, # Low passenger disruption (only late freight/empty rakes)
        "allowed_days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        "slot_type": "NIGHT_MAJOR"
    },
    {
        "slot_id": "SLOT-MIDDAY-01",
        "name": "Off-Peak Midday Shadow Window",
        "start_time": "11:30",
        "end_time": "13:30",
        "duration_mins": 120,
        "traffic_impact_factor": 0.55, # Moderate suburban diversion / single line working
        "allowed_days": ["TUE", "WED", "THU"],
        "slot_type": "MIDDAY_SHADOW"
    },
    {
        "slot_id": "SLOT-SUNDAY-MEGA",
        "name": "Sunday Maintenance Mega Block",
        "start_time": "10:00",
        "end_time": "15:00",
        "duration_mins": 300,
        "traffic_impact_factor": 0.40, # Sunday suburban timetable has planned diversions
        "allowed_days": ["SUN"],
        "slot_type": "SUNDAY_MEGA"
    },
    {
        "slot_id": "SLOT-EARLY-AFT-01",
        "name": "Post-Noon Slack Window",
        "start_time": "13:45",
        "end_time": "15:15",
        "duration_mins": 90,
        "traffic_impact_factor": 0.50,
        "allowed_days": ["MON", "FRI"],
        "slot_type": "DAY_SHORT"
    }
]

# Sample key trains running across Churchgate - Virar corridor for Time-Distance charting
SAMPLE_TRAIN_PATHS = [
    {"train_no": "20901", "name": "Vande Bharat Exp", "type": "SUPERFAST", "dir": "DN", "dept_time": "06:10", "arr_time": "07:05", "track": "DN_FAST"},
    {"train_no": "12951", "name": "Mumbai Rajdhani", "type": "RAJDHANI",  "dir": "UP", "dept_time": "07:35", "arr_time": "08:32", "track": "UP_FAST"},
    {"train_no": "12953", "name": "August Kranti",   "type": "RAJDHANI",  "dir": "UP", "dept_time": "09:40", "arr_time": "10:35", "track": "UP_FAST"},
    {"train_no": "SUB-901", "name": "Churchgate Fast", "type": "SUBURBAN", "dir": "UP", "dept_time": "10:15", "arr_time": "11:10", "track": "UP_FAST"},
    {"train_no": "SUB-902", "name": "Borivali Fast",   "type": "SUBURBAN", "dir": "DN", "dept_time": "11:45", "arr_time": "12:40", "track": "DN_FAST"},
    {"train_no": "FRT-JNPT1", "name": "JNPT Container Rake", "type": "FREIGHT", "dir": "DN", "dept_time": "02:00", "arr_time": "03:30", "track": "DN_FAST"},
    {"train_no": "FRT-POL2", "name": "BTPN Petroleum Rake", "type": "FREIGHT", "dir": "UP", "dept_time": "02:45", "arr_time": "04:15", "track": "UP_FAST"},
    {"train_no": "SUB-903", "name": "Virar Slow Local", "type": "SUBURBAN", "dir": "DN", "dept_time": "14:00", "arr_time": "15:20", "track": "DN_SLOW"},
    {"train_no": "12925", "name": "Paschim Express", "type": "SUPERFAST", "dir": "DN", "dept_time": "11:25", "arr_time": "12:20", "track": "DN_FAST"},
]

def get_corridor_slots() -> List[Dict[str, Any]]:
    """Returns standard Control Office corridor slots."""
    return CORRIDOR_SLOTS

def get_train_paths() -> List[Dict[str, Any]]:
    """Returns sample train paths for time-distance visualization."""
    return SAMPLE_TRAIN_PATHS

def evaluate_slot_disruption(slot_id: str, section_code: str, track_id: str) -> Dict[str, Any]:
    """Calculates estimated train impact (trains regulated or diverted) for a given slot."""
    slot = next((s for s in CORRIDOR_SLOTS if s["slot_id"] == slot_id), None)
    if not slot:
        return {"affected_trains": 0, "penalty_minutes": 0}
    
    if slot["slot_type"] == "NIGHT_MAJOR":
        # Mainly freight / empty rakes
        return {"affected_passenger_trains": 0, "affected_freight_trains": 2, "estimated_delay_mins": 15}
    elif slot["slot_type"] == "SUNDAY_MEGA":
        return {"affected_passenger_trains": 4, "affected_freight_trains": 1, "estimated_delay_mins": 35}
    else: # Midday / Day slots
        return {"affected_passenger_trains": 6, "affected_freight_trains": 0, "estimated_delay_mins": 60}

# --------------------------------------------------------------------------- #
#  CRIS COA ingestion (timetable / block-availability windows)
# --------------------------------------------------------------------------- #
DAY_ALIASES = {
    "MON": "MON", "MONDAY": "MON", "M": "MON",
    "TUE": "TUE", "TUES": "TUE", "TUESDAY": "TUE", "T": "TUE",
    "WED": "WED", "WEDS": "WED", "WEDNESDAY": "WED", "W": "WED",
    "THU": "THU", "THUR": "THU", "THURS": "THU", "THURSDAY": "THU", "TH": "THU",
    "FRI": "FRI", "FRIDAY": "FRI", "F": "FRI",
    "SAT": "SAT", "SATURDAY": "SAT", "SA": "SAT",
    "SUN": "SUN", "SUNDAY": "SUN", "SU": "SUN",
}

#: Slot-type spellings are matched *after* ``-``/``_`` are folded to spaces.
SLOT_TYPE_ALIASES = {
    "NIGHT MAJOR": "NIGHT_MAJOR", "NIGHT MEGA": "NIGHT_MAJOR", "NIGHT": "NIGHT_MAJOR",
    "MIDDAY SHADOW": "MIDDAY_SHADOW", "MIDDAY": "MIDDAY_SHADOW", "SHADOW": "MIDDAY_SHADOW",
    "SUNDAY MEGA": "SUNDAY_MEGA", "SUNDAY": "SUNDAY_MEGA",
    "DAY SHORT": "DAY_SHORT", "DAY": "DAY_SHORT", "SHORT": "DAY_SHORT",
}

_SLOT_ALIASES = {
    "slot_id": ("slot_id", "slot_no", "id", "block_slot_id", "window_id"),
    "name": ("name", "slot_name", "description", "title"),
    "start_time": ("start_time", "from_time", "block_from", "start", "window_start"),
    "end_time": ("end_time", "to_time", "block_to", "end", "window_end"),
    "duration_mins": ("duration_mins", "duration", "window_mins", "minutes"),
    "allowed_days": ("allowed_days", "days", "day", "weekdays"),
    "traffic_impact_factor": (
        "traffic_impact_factor", "impact_factor", "disruption_factor", "impact",
    ),
    "slot_type": ("slot_type", "type", "class", "category"),
}

_TRAIN_ALIASES = {
    "train_no": ("train_no", "train_number", "number", "train_id"),
    "name": ("name", "train_name", "description"),
    "type": ("type", "train_type", "class"),
    "dir": ("dir", "direction", "up_dn"),
    "dept_time": ("dept_time", "departure_time", "dep_time", "from_time"),
    "arr_time": ("arr_time", "arrival_time", "to_time"),
    "track": ("track", "line", "track_id"),
}


def _normalise_days(value) -> List[str]:
    """Normalise day encodings (``'MON,TUE'``, ``['Monday']``, ``'Mon'``)."""
    if value is None:
        return ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    if isinstance(value, str):
        raw_days = [part for part in value.replace(";", ",").replace("/", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_days = list(value)
    else:
        raw_days = [value]
    days: List[str] = []
    for day in raw_days:
        token = str(day).strip().upper()
        if token in DAY_ALIASES:
            code = DAY_ALIASES[token]
            if code not in days:
                days.append(code)
    return days


def normalize_corridor_slot(raw, index: int = 0) -> Dict[str, Any]:
    """
    Normalise one COA block-availability window into the corridor slot schema.

    Durations are *derived* from the window times when the feed omits them, so a
    slot can never advertise 240 minutes of availability inside a 120-minute
    window - the single most common cause of an infeasible block plan.
    """
    from .pipeline import coerce_bool, pick_any
    from .safety import parse_time

    if not isinstance(raw, dict):
        raise ValueError(f"slot #{index} is {type(raw).__name__}, expected an object")

    slot_id = pick_any(raw, _SLOT_ALIASES["slot_id"])
    if slot_id is None or str(slot_id).strip() == "":
        slot_id = f"SLOT-IMPORTED-{index + 1:02d}"
    start_raw = pick_any(raw, _SLOT_ALIASES["start_time"])
    end_raw = pick_any(raw, _SLOT_ALIASES["end_time"])
    if start_raw is None or end_raw is None:
        raise ValueError(f"slot {slot_id} is missing its start/end window times")

    start = parse_time(start_raw)
    end = parse_time(end_raw)
    if end <= start:  # a midnight-crossing window is normal for night blocks
        end = end.replace(day=start.day) + timedelta(days=1)
    derived_mins = int((end - start).total_seconds() // 60)

    declared = pick_any(raw, _SLOT_ALIASES["duration_mins"])
    if declared is None:
        duration_mins = derived_mins
    else:
        try:
            duration_mins = int(round(float(str(declared).strip())))
        except (TypeError, ValueError):
            raise ValueError(f"slot {slot_id} has an unparseable duration {declared!r}")
        if duration_mins > derived_mins:
            duration_mins = derived_mins  # never claim more than the window holds

    if not 15 <= duration_mins <= 480:
        raise ValueError(
            f"slot {slot_id} duration {duration_mins} min is outside 15-480 min"
        )

    factor_raw = pick_any(raw, _SLOT_ALIASES["traffic_impact_factor"])
    try:
        factor = 0.5 if factor_raw is None else float(factor_raw)
    except (TypeError, ValueError):
        raise ValueError(f"slot {slot_id} has an unparseable traffic impact factor")
    factor = max(0.0, min(1.0, factor))

    slot_type_raw = str(pick_any(raw, _SLOT_ALIASES["slot_type"]) or "DAY_SHORT").strip().upper()
    slot_type = SLOT_TYPE_ALIASES.get(slot_type_raw.replace("-", " ").replace("_", " "), "DAY_SHORT")

    days = _normalise_days(pick_any(raw, _SLOT_ALIASES["allowed_days"]))
    corridor_wide = coerce_bool(pick_any(raw, ("corridor_wide", "full_corridor", "all_sections")), default=False)

    return {
        "slot_id": str(slot_id).strip().upper(),
        "name": str(pick_any(raw, _SLOT_ALIASES["name"]) or slot_id).strip(),
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "duration_mins": duration_mins,
        "window_mins_derived": derived_mins,
        "traffic_impact_factor": round(factor, 3),
        "allowed_days": days or ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        "slot_type": slot_type,
        "corridor_wide": corridor_wide,
        "normalised": True,
    }


def normalize_train_path(raw, index: int = 0) -> Dict[str, Any]:
    """Normalise one COA time-distance path entry for the string chart."""
    from .pipeline import pick_any
    from .safety import parse_time

    if not isinstance(raw, dict):
        raise ValueError(f"train path #{index} is {type(raw).__name__}, expected an object")
    train_no = pick_any(raw, _TRAIN_ALIASES["train_no"])
    if train_no is None or str(train_no).strip() == "":
        raise ValueError(f"train path #{index} has no train number")
    dept_raw = pick_any(raw, _TRAIN_ALIASES["dept_time"])
    arr_raw = pick_any(raw, _TRAIN_ALIASES["arr_time"])
    if dept_raw is None or arr_raw is None:
        raise ValueError(f"train {train_no} is missing its departure/arrival times")

    dept = parse_time(dept_raw)
    arr = parse_time(arr_raw)
    if arr <= dept:
        arr = arr + timedelta(days=1)

    direction = str(pick_any(raw, _TRAIN_ALIASES["dir"]) or "DN").strip().upper()
    if direction in ("UP", "U"):
        direction = "UP"
    elif direction in ("DN", "DOWN", "D"):
        direction = "DN"
    else:
        raise ValueError(f"train {train_no} has an invalid direction {direction!r}")

    track = str(pick_any(raw, _TRAIN_ALIASES["track"]) or "DN_FAST").strip().upper().replace(" ", "_")
    if track not in TRACKS:
        raise ValueError(f"train {train_no} runs on unknown line {track!r}")

    return {
        "train_no": str(train_no).strip().upper(),
        "name": str(pick_any(raw, _TRAIN_ALIASES["name"]) or train_no).strip(),
        "type": str(pick_any(raw, _TRAIN_ALIASES["type"]) or "SUBURBAN").strip().upper(),
        "dir": direction,
        "dept_time": dept.strftime("%H:%M"),
        "arr_time": arr.strftime("%H:%M"),
        "track": track,
        "run_mins": int((arr - dept).total_seconds() // 60),
        "normalised": True,
    }


def parse_coa_timetable(payload) -> Dict[str, Any]:
    """
    Ingest a CRIS **COA** feed (block-availability windows + train paths).

    COA is not a defect silo: it supplies the *time* dimension the optimizer
    allocates against, so it returns accepted/rejected window records rather
    than :class:`BlockRequisition` objects.
    """
    slots_raw: List[Any] = []
    paths_raw: List[Any] = []
    feed_id = None

    if isinstance(payload, dict):
        feed_id = payload.get("feed_id")
        candidate = payload.get("slots", payload.get("windows", payload.get("blocks")))
        if isinstance(candidate, dict):
            slots_raw = [candidate]
        elif isinstance(candidate, (list, tuple)):
            slots_raw = list(candidate)
        elif candidate is None and payload.get("start_time"):
            slots_raw = [payload]
        paths = payload.get("train_paths", payload.get("trains"))
        if isinstance(paths, (list, tuple)):
            paths_raw = list(paths)
    elif isinstance(payload, (list, tuple)):
        slots_raw = list(payload)
    elif payload is not None:
        slots_raw = [payload]

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for index, raw in enumerate(slots_raw):
        try:
            accepted.append(normalize_corridor_slot(raw, index))
        except (ValueError, TypeError) as exc:
            rejected.append({"index": index, "errors": [str(exc)], "raw": raw if isinstance(raw, dict) else {"value": repr(raw)}})

    train_paths: List[Dict[str, Any]] = []
    for index, raw in enumerate(paths_raw):
        try:
            train_paths.append(normalize_train_path(raw, index))
        except (ValueError, TypeError) as exc:
            rejected.append({"index": index, "errors": [str(exc)], "raw": raw if isinstance(raw, dict) else {"value": repr(raw)}})

    total = len(slots_raw) + len(paths_raw)
    return {
        "feed_id": feed_id,
        "source": "COA",
        "received": total,
        "accepted_count": len(accepted) + len(train_paths),
        "rejected_count": len(rejected),
        "acceptance_pct": round((len(accepted) + len(train_paths)) / total * 100.0, 1) if total else 100.0,
        "slots": accepted,
        "train_paths": train_paths,
        "rejected": rejected,
        "total_window_mins": sum(slot["duration_mins"] for slot in accepted),
    }


if __name__ == "__main__":
    print(f"Loaded {len(CORRIDOR_SLOTS)} standard COA corridor slots.")
    print("Slot 1:", CORRIDOR_SLOTS[0]["name"])

    report = parse_coa_timetable({"feed_id": "COA-SAMPLE", "slots": CORRIDOR_SLOTS, "train_paths": SAMPLE_TRAIN_PATHS})
    print(
        f"COA ingestion   : {report['accepted_count']}/{report['received']} records normalised "
        f"({report['rejected_count']} rejected)"
    )
    print(f"Window capacity : {report['total_window_mins']} min across {len(report['slots'])} slots")
    print(f"Sample slot     : {report['slots'][0]['slot_id']} {report['slots'][0]['start_time']}-"
          f"{report['slots'][0]['end_time']} ({report['slots'][0]['duration_mins']} min, {report['slots'][0]['slot_type']})")
