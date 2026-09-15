"""
monthly_planner.py - Long-Term Monthly Master Block Planner (Tactical Horizon)
Generates 30-day cyclical maintenance schedules, heavy track machine deployment plans,
and division-wide asset availability quotas.
"""

from typing import List, Dict, Any
from datetime import datetime, timedelta
from ..ai_engine.block_optimizer import IntegratedBlockOptimizer

class MonthlyBlockPlanner:
    """
    Manages the 30-Day Master Maintenance Schedule.
    Coordinates heavy machinery rosters (CSM, BCM, Tower Wagons) and departmental quota equity.
    """

    def __init__(self):
        self.optimizer = IntegratedBlockOptimizer()

    def generate_monthly_plan(self, defects: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Runs multi-week tactical scheduling across 4 calendar weeks (30 days)."""
        # Run 30-day optimization
        opt_result = self.optimizer.optimize_blocks(defects, horizon_days=30)
        blocks = opt_result["scheduled_blocks"]

        # Segment into 4 distinct calendar weeks
        base_date = datetime.now()
        weeks_data: Dict[str, List[Dict[str, Any]]] = {
            "Week 1 (Days 1-7)": [],
            "Week 2 (Days 8-14)": [],
            "Week 3 (Days 15-21)": [],
            "Week 4 (Days 22-30)": []
        }

        # Track machine allocation schedule
        machine_deployments = [
            {"machine": "CSM Track Tamping Machine #402", "assigned_week": "Week 1 (Days 1-7)", "target_section": "CCG-DDR / DDR-BA"},
            {"machine": "BCM Ballast Cleaner #108",        "assigned_week": "Week 2 (Days 8-14)", "target_section": "BA-AND UP_FAST"},
            {"machine": "RU-8 OHE Tower Wagon Rake",      "assigned_week": "Week 3 (Days 15-21)", "target_section": "AND-BVI OHE Corridor"},
            {"machine": "RGM Rail Grinding Machine #204",  "assigned_week": "Week 4 (Days 22-30)", "target_section": "BVI-VR Trunk Line"},
        ]

        for blk in blocks:
            blk_date = datetime.strptime(blk["date"], "%Y-%m-%d")
            delta_days = (blk_date - base_date).days
            if delta_days < 7:
                weeks_data["Week 1 (Days 1-7)"].append(blk)
            elif delta_days < 14:
                weeks_data["Week 2 (Days 8-14)"].append(blk)
            elif delta_days < 21:
                weeks_data["Week 3 (Days 15-21)"].append(blk)
            else:
                weeks_data["Week 4 (Days 22-30)"].append(blk)

        # Departmental Quota Balance
        dept_hours = {"ENGINEERING": 0.0, "S&T": 0.0, "ELECTRICAL_TRD": 0.0}
        for blk in blocks:
            for dept in blk["departments"]:
                if dept in dept_hours:
                    dept_hours[dept] += round(blk["allocated_duration_mins"] / 60.0, 1)

        return {
            "horizon": "MONTHLY",
            "horizon_days": 30,
            "generated_at": datetime.now().isoformat(),
            "calendar_weeks": weeks_data,
            "machine_deployments": machine_deployments,
            "departmental_hours_allocated": dept_hours,
            "total_blocks_in_month": len(blocks),
            "scheduled_blocks": blocks,
            "metrics": opt_result["metrics"]
        }

if __name__ == "__main__":
    from ..data_ingestion.tms_data import generate_tms_defects
    from ..data_ingestion.smms_data import generate_smms_defects
    from ..data_ingestion.tdms_data import generate_tdms_defects

    planner = MonthlyBlockPlanner()
    all_def = generate_tms_defects(15) + generate_smms_defects(12) + generate_tdms_defects(12)
    res = planner.generate_monthly_plan(all_def)
    print(f"Monthly Plan: {res['total_blocks_in_month']} blocks planned across 4 weeks.")
    print("Machine Deployments:", len(res['machine_deployments']))
    print("Dept Hours:", res['departmental_hours_allocated'])
