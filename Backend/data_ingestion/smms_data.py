"""
smms_data.py - Signalling Maintenance & Management System (SMMS) Data Ingestion
Models Signal & Telecommunication (S&T) defects, gear overhauls, and disconnection demands.
"""

import random
from datetime import datetime, timedelta
from typing import List, Dict, Any
from .tms_data import SECTIONS, TRACKS

SNT_TASKS = [
    {
        "type": "POINT_MACHINE_TEST",
        "description": "Electric Point Machine (143mm stroke) obstruction test & motor overhaul",
        "severity": "CRITICAL",
        "safety_weight": 0.90,
        "base_duration_mins": 60,
        "max_days_allowed": 3,
        "requires_disconnection": True
    },
    {
        "type": "AXLE_COUNTER_CALIBRATION",
        "description": "Multi-Section Digital Axle Counter (MSDAC) wheel sensor tuning & reset test",
        "severity": "URGENT",
        "safety_weight": 0.80,
        "base_duration_mins": 45,
        "max_days_allowed": 5,
        "requires_disconnection": True
    },
    {
        "type": "TRACK_CIRCUIT_BONDING",
        "description": "Audio Frequency Track Circuit (AFTC) bond replacement and tuning unit check",
        "severity": "URGENT",
        "safety_weight": 0.70,
        "base_duration_mins": 60,
        "max_days_allowed": 7,
        "requires_disconnection": True
    },
    {
        "type": "ELECTRONIC_INTERLOCKING_DIAG",
        "description": "Electronic Interlocking (EI) CPU redundancy switchover & diagnostic audit",
        "severity": "ROUTINE",
        "safety_weight": 0.50,
        "base_duration_mins": 90,
        "max_days_allowed": 14,
        "requires_disconnection": False
    },
    {
        "type": "SIGNAL_ASPECT_REPLACEMENT",
        "description": "LED Signal unit replacement, current regulator test, and focal alignment",
        "severity": "ROUTINE",
        "safety_weight": 0.55,
        "base_duration_mins": 45,
        "max_days_allowed": 10,
        "requires_disconnection": False
    },
    {
        "type": "LC_GATE_INTERLOCK_TEST",
        "description": "Manned Level Crossing gate interlock proving and telephone line verification",
        "severity": "URGENT",
        "safety_weight": 0.75,
        "base_duration_mins": 40,
        "max_days_allowed": 4,
        "requires_disconnection": True
    }
]

def generate_smms_defects(count: int = 20, seed: int = 43) -> List[Dict[str, Any]]:
    """Generates a realistic snapshot of S&T maintenance items from SMMS."""
    random.seed(seed)
    items = []
    base_time = datetime.now()

    for i in range(1, count + 1):
        sec = random.choice(SECTIONS)
        trk = random.choice(TRACKS)
        task_tmpl = random.choice(SNT_TASKS)
        
        km_start = round(random.uniform(1.0, sec["length_km"] - 1.0), 2)
        km_end = round(km_start + 0.05, 2)
        days_overdue = random.choice([0, 0, 1, 2, 3, 5]) if task_tmpl["severity"] != "CRITICAL" else random.choice([0, 1])

        item = {
            "defect_id": f"SMMS-SNT-{2000 + i}",
            "department": "S&T",
            "system": "SMMS",
            "section": sec["code"],
            "section_name": sec["name"],
            "track_id": trk,
            "km_start": km_start,
            "km_end": km_end,
            "gear_id": f"PT-{sec['code'][:3]}-{10 + i}",
            "defect_type": task_tmpl["type"],
            "description": task_tmpl["description"],
            "severity": task_tmpl["severity"],
            "safety_weight": task_tmpl["safety_weight"],
            "psr_speed_kmph": None,
            "duration_mins": task_tmpl["base_duration_mins"],
            "days_overdue": days_overdue,
            "target_completion_days": max(1, task_tmpl["max_days_allowed"] - days_overdue),
            "gmt": sec["gmt"],
            "requires_traffic_block": task_tmpl["requires_disconnection"],
            "requires_power_block": False,
            "status": "PENDING",
            "reported_date": (base_time - timedelta(days=days_overdue + 1)).strftime("%Y-%m-%d")
        }
        items.append(item)

    return items

if __name__ == "__main__":
    data = generate_smms_defects(10)
    print(f"Generated {len(data)} sample SMMS defects.")
    print("Sample record:", data[0])
