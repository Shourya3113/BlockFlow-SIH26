"""
weekly_planner.py - Short-Term Weekly Rolling Block Planner (Operational Horizon)
Generates granular 7-day schedules with exact time windows, train regulation plans,
and departmental Permit-To-Work (PTW) coordination.
"""

from typing import List, Dict, Any
from datetime import datetime, timedelta
from ..ai_engine.block_optimizer import IntegratedBlockOptimizer

class WeeklyBlockPlanner:
    """
    Manages the 7-Day Operational Rolling Block Schedule.
    Maps optimized blocks to specific train regulation instructions for the Control Office.
    """

    def __init__(self):
        self.optimizer = IntegratedBlockOptimizer()

    def generate_weekly_plan(self, defects: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Runs optimization for a 7-day horizon and enriches with operational instructions."""
        opt_result = self.optimizer.optimize_blocks(defects, horizon_days=7)
        blocks = opt_result["scheduled_blocks"]

        enriched_blocks = []
        for blk in blocks:
            blk_copy = dict(blk)
            # Add Control Office operational train regulation guidelines
            track = blk_copy["track_id"]
            sec_name = blk_copy["section_name"]
            
            regulation_orders = []
            if "FAST" in track:
                alt_track = track.replace("FAST", "SLOW")
                regulation_orders.append(
                    f"Traffic on {track} diverted to {alt_track} line between {sec_name}."
                )
                regulation_orders.append(
                    "Speed restriction of 30 km/h over crossover junctions for diverted services."
                )
            else:
                regulation_orders.append(
                    f"Single Line Working (SLW) or platform re-allocation enforced at {sec_name}."
                )

            if blk_copy["requires_power_block"]:
                regulation_orders.append(
                    f"OHE Power Block granted by Traction Power Controller (TPC). Permit-to-Work (PTW) issued."
                )

            blk_copy["train_regulation_orders"] = regulation_orders
            enriched_blocks.append(blk_copy)

        return {
            "horizon": "WEEKLY",
            "horizon_days": 7,
            "generated_at": datetime.now().isoformat(),
            "scheduled_blocks": enriched_blocks,
            "unassigned_tasks": opt_result["unassigned_tasks"],
            "metrics": opt_result["metrics"]
        }

if __name__ == "__main__":
    from ..data_ingestion.tms_data import generate_tms_defects
    from ..data_ingestion.smms_data import generate_smms_defects
    from ..data_ingestion.tdms_data import generate_tdms_defects

    planner = WeeklyBlockPlanner()
    all_def = generate_tms_defects(10) + generate_smms_defects(8) + generate_tdms_defects(8)
    res = planner.generate_weekly_plan(all_def)
    print(f"Weekly Plan: {len(res['scheduled_blocks'])} blocks planned.")
    print(f"Fulfillment: {res['metrics']['schedule_fulfillment_pct']}%")
