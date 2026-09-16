"""
tdms_data.py - Traction Distribution Management System (TDMS) Data Ingestion
Models Electrical/TRD (Traction Distribution) maintenance demands, OHE 25kV power blocks,
and contact/catenary wire maintenance for Indian Railways.
"""

import random
from datetime import datetime, timedelta
from typing import List, Dict, Any
from .tms_data import SECTIONS, TRACKS

TRD_TASKS = [
    {
        "type": "OHE_CANTILEVER_OVERHAUL",
        "description": "25kV AC Cantilever assembly overhaul, bracket insulator replacement & stagger adjustment",
        "severity": "CRITICAL",
        "safety_weight": 0.85,
        "base_duration_mins": 90,
        "max_days_allowed": 3,
        "requires_power_block": True,
        "requires_traffic_block": True
    },
    {
        "type": "CONTACT_WIRE_HOTSPOT",
        "description": "Thermovision detected hotspot (>80°C) on dropper jumper / PG clamp",
        "severity": "CRITICAL",
        "safety_weight": 0.95,
        "base_duration_mins": 60,
        "max_days_allowed": 2,
        "requires_power_block": True,
        "requires_traffic_block": True
    },
    {
        "type": "NEUTRAL_SECTION_INSPECTION",
        "description": "PTFE Section insulator contact runner wear check & arc trap cleaning",
        "severity": "URGENT",
        "safety_weight": 0.75,
        "base_duration_mins": 75,
        "max_days_allowed": 5,
        "requires_power_block": True,
        "requires_traffic_block": True
    },
    {
        "type": "OHE_HEIGHT_STAGGER_REC",
        "description": "Annual Tower Wagon (RU) recording of OHE height and stagger profile",
        "severity": "ROUTINE",
        "safety_weight": 0.50,
        "base_duration_mins": 120,
        "max_days_allowed": 15,
        "requires_power_block": True,
        "requires_traffic_block": True
    },
    {
        "type": "ISOLATOR_INTERRUPTER_TEST",
        "description": "25kV motorized isolator interrupter contact resistance & motor gear inspection",
        "severity": "URGENT",
        "safety_weight": 0.65,
        "base_duration_mins": 60,
        "max_days_allowed": 7,
        "requires_power_block": True,
        "requires_traffic_block": False
    },
    {
        "type": "EARTHING_BOND_AUDIT",
        "description": "Traction mast structure bond & earth continuity inspection (preventive)",
        "severity": "ROUTINE",
        "safety_weight": 0.40,
        "base_duration_mins": 45,
        "max_days_allowed": 20,
        "requires_power_block": False,
        "requires_traffic_block": False
    }
]

def generate_tdms_defects(count: int = 20, seed: int = 44) -> List[Dict[str, Any]]:
    """Generates a realistic snapshot of Traction Distribution maintenance demands."""
    random.seed(seed)
    items = []
    base_time = datetime.now()

    for i in range(1, count + 1):
        sec = random.choice(SECTIONS)
        trk = random.choice(TRACKS)
        task_tmpl = random.choice(TRD_TASKS)
        
        km_start = round(random.uniform(1.0, sec["length_km"] - 1.0), 2)
        km_end = round(km_start + random.choice([0.1, 0.5, 1.2]), 2)
        days_overdue = random.choice([0, 0, 1, 2, 4]) if task_tmpl["severity"] != "CRITICAL" else random.choice([0, 1])

        item = {
            "defect_id": f"TDMS-TRD-{3000 + i}",
            "department": "ELECTRICAL_TRD",
            "system": "TDMS",
            "section": sec["code"],
            "section_name": sec["name"],
            "track_id": trk,
            "km_start": km_start,
            "km_end": km_end,
            "substation": f"TSS-{sec['code'][:3]}",
            "defect_type": task_tmpl["type"],
            "description": task_tmpl["description"],
            "severity": task_tmpl["severity"],
            "safety_weight": task_tmpl["safety_weight"],
            "psr_speed_kmph": None,
            "duration_mins": task_tmpl["base_duration_mins"],
            "days_overdue": days_overdue,
            "target_completion_days": max(1, task_tmpl["max_days_allowed"] - days_overdue),
            "gmt": sec["gmt"],
            "requires_traffic_block": task_tmpl["requires_traffic_block"],
            "requires_power_block": task_tmpl["requires_power_block"],
            "status": "PENDING",
            "reported_date": (base_time - timedelta(days=days_overdue + 1)).strftime("%Y-%m-%d")
        }
        items.append(item)

    return items

# --------------------------------------------------------------------------- #
#  CRIS TDMS ingestion entry points
# --------------------------------------------------------------------------- #
def parse_tdms_payload(payload, scorer=None):
    """
    Ingest a raw CRIS **TDMS** (Traction Distribution) feed.

    Every 25 kV job is normalised to ``requires_power_block: true`` unless the
    silo explicitly states otherwise, so the ACTM Vol II Para 203/204 invariant
    (TPC isolation + 15-minute earthing buffers) always has a window to attach
    itself to.
    """
    from .pipeline import ingest_feed

    return ingest_feed(payload, source="TDMS", scorer=scorer)


def normalize_tdms_defect(defect):
    """Convert one synthetic/legacy TDMS defect dict into a BlockRequisition."""
    from .pipeline import normalize_legacy_defect

    return normalize_legacy_defect(defect, source="TDMS")


if __name__ == "__main__":
    data = generate_tdms_defects(10)
    print(f"Generated {len(data)} sample TDMS defects.")
    print("Sample record:", data[0])

    requisition = normalize_tdms_defect(data[0])
    print(f"LRS span        : {requisition.to_lrs_dict()}")
    requisition.project_display()
    print(
        f"Feeding post    : "
        f"{requisition.feeding_post or requisition.display_start.station_code}"
    )
    print(
        f"Display only    : {requisition.display_start.coordinates} "
        "[read-only projection, never a solver input]"
    )
