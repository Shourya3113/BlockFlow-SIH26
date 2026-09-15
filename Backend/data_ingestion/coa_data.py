"""
coa_data.py - Control Office Application (COA) & Corridor Traffic Model
Models Train Time Tables, Freight paths, and Corridor Block Availability Windows
for Indian Railways Control Office.
"""

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

if __name__ == "__main__":
    print(f"Loaded {len(CORRIDOR_SLOTS)} standard COA corridor slots.")
    print("Slot 1:", CORRIDOR_SLOTS[0]["name"])
