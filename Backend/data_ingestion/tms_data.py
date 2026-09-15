"""
tms_data.py - Track Management System (TMS) Data Ingestion & Generator
Models Engineering (Civil/Track) defects, USFD rail flaws, and machine maintenance demands
for Indian Railways corridors.
"""

import random
from datetime import datetime, timedelta
from typing import List, Dict, Any

SECTIONS = [
    {"code": "CCG-DDR", "name": "Churchgate - Dadar", "length_km": 9.0, "gmt": 65},
    {"code": "DDR-BA",  "name": "Dadar - Bandra",     "length_km": 4.5, "gmt": 72},
    {"code": "BA-AND",  "name": "Bandra - Andheri",   "length_km": 7.5, "gmt": 68},
    {"code": "AND-BVI", "name": "Andheri - Borivali", "length_km": 12.0, "gmt": 75},
    {"code": "BVI-VR",  "name": "Borivali - Virar",   "length_km": 26.0, "gmt": 58},
]

TRACKS = ["UP_FAST", "DN_FAST", "UP_SLOW", "DN_SLOW"]

ENGINEERING_TASKS = [
    {
        "type": "USFD_IMR_WELD",
        "description": "USFD IMR (Immediate Removal) Rail Flaw detected in AT weld",
        "severity": "CRITICAL",
        "safety_weight": 1.0,
        "psr_speed_kmph": 30,
        "base_duration_mins": 120,
        "max_days_allowed": 2
    },
    {
        "type": "USFD_OBS_FLAW",
        "description": "USFD OBS (Observed) defect requiring joggled fishplating/renewal",
        "severity": "URGENT",
        "safety_weight": 0.75,
        "psr_speed_kmph": 50,
        "base_duration_mins": 90,
        "max_days_allowed": 5
    },
    {
        "type": "TRACK_TAMPING",
        "description": "CSM Track Tamping for track geometry correction (SD index > 3.2)",
        "severity": "URGENT",
        "safety_weight": 0.65,
        "psr_speed_kmph": 45,
        "base_duration_mins": 180,
        "max_days_allowed": 7
    },
    {
        "type": "TURNOUT_RENEWAL",
        "description": "1 in 12 Curved Switch & CMS Crossing Overhaul at crossover",
        "severity": "ROUTINE",
        "safety_weight": 0.50,
        "psr_speed_kmph": None,
        "base_duration_mins": 210,
        "max_days_allowed": 14
    },
    {
        "type": "RAIL_DESTRESSING",
        "description": "LWR/CWR Destressing and SEJ (Switch Expansion Joint) adjustment",
        "severity": "ROUTINE",
        "safety_weight": 0.40,
        "psr_speed_kmph": 60,
        "base_duration_mins": 150,
        "max_days_allowed": 21
    },
    {
        "type": "DEEP_SCREENING_BCM",
        "description": "Ballast Cleaning Machine (BCM) cushion restoration",
        "severity": "ROUTINE",
        "safety_weight": 0.45,
        "psr_speed_kmph": 40,
        "base_duration_mins": 240,
        "max_days_allowed": 28
    }
]

def generate_tms_defects(count: int = 25, seed: int = 42) -> List[Dict[str, Any]]:
    """Generates a realistic snapshot of TMS Engineering maintenance demands."""
    random.seed(seed)
    defects = []
    base_time = datetime.now()

    for i in range(1, count + 1):
        sec = random.choice(SECTIONS)
        trk = random.choice(TRACKS)
        task_tmpl = random.choice(ENGINEERING_TASKS)
        
        km_start = round(random.uniform(1.0, sec["length_km"] - 1.0), 2)
        km_end = round(km_start + random.choice([0.05, 0.2, 0.8, 1.5]), 2)
        days_overdue = random.choice([0, 0, 1, 2, 4, 7]) if task_tmpl["severity"] != "CRITICAL" else random.choice([0, 1])

        item = {
            "defect_id": f"TMS-ENG-{1000 + i}",
            "department": "ENGINEERING",
            "system": "TMS",
            "section": sec["code"],
            "section_name": sec["name"],
            "track_id": trk,
            "km_start": km_start,
            "km_end": km_end,
            "defect_type": task_tmpl["type"],
            "description": task_tmpl["description"],
            "severity": task_tmpl["severity"],
            "safety_weight": task_tmpl["safety_weight"],
            "psr_speed_kmph": task_tmpl["psr_speed_kmph"],
            "duration_mins": task_tmpl["base_duration_mins"],
            "days_overdue": days_overdue,
            "target_completion_days": max(1, task_tmpl["max_days_allowed"] - days_overdue),
            "gmt": sec["gmt"],
            "requires_traffic_block": True,
            "requires_power_block": False,
            "status": "PENDING",
            "reported_date": (base_time - timedelta(days=days_overdue + 1)).strftime("%Y-%m-%d")
        }
        defects.append(item)

    return defects

if __name__ == "__main__":
    data = generate_tms_defects(10)
    print(f"Generated {len(data)} sample TMS defects.")
    print("Sample record:", data[0])
