"""
block_optimizer.py - Multi-Department Integrated Block Optimizer & Shadow Bundler
Solves the joint maintenance block scheduling problem using Mixed-Integer Programming (SciPy HiGHS)
and Indian Railways Joint/Shadow Blocking heuristics.
"""

from typing import List, Dict, Any, Tuple
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
from datetime import datetime, timedelta
from .prioritizer import AssetCriticalityPrioritizer
from ..data_ingestion.coa_data import get_corridor_slots, evaluate_slot_disruption

class IntegratedBlockOptimizer:
    """
    Coordinates and schedules maintenance blocks across Engineering, S&T, and TRD.
    Eliminates decentralized siloed planning by grouping overlapping spatial demands
    into single 'Joint Shadow Mega Blocks'.
    """

    def __init__(self):
        self.prioritizer = AssetCriticalityPrioritizer()

    def optimize_blocks(self,
                        defects: List[Dict[str, Any]],
                        horizon_days: int = 7,
                        max_blocks_per_day: int = 2) -> Dict[str, Any]:
        """
        Executes multi-department joint optimization.
        1. Ranks all defects using Asset Criticality Index (ACI).
        2. Clusters demands by Spatial Corridor (Section + Track).
        3. Applies Joint Shadow Blocking (Bundling S&T + TRD + Engg).
        4. Solves slot-allocation MILP to assign bundled blocks to lowest-impact COA corridor windows.
        """
        if not defects:
            return {"scheduled_blocks": [], "unassigned_tasks": [], "metrics": {}}

        # Step 1: Prioritize all incoming tasks
        ranked_tasks = self.prioritizer.rank_maintenance_demands(defects)

        # Step 2: Spatial Clustering for Joint Bundling
        # Group tasks by (section, track_id)
        spatial_clusters: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for task in ranked_tasks:
            key = (task["section"], task["track_id"])
            if key not in spatial_clusters:
                spatial_clusters[key] = []
            spatial_clusters[key].append(task)

        # Step 3: Create Candidate Joint Blocks
        candidate_blocks = []
        block_counter = 1

        for (section, track_id), cluster_tasks in spatial_clusters.items():
            # Group into bundles where departments coordinate
            depts_in_cluster = {t["department"] for t in cluster_tasks}
            
            # Sort cluster by ACI descending
            cluster_tasks.sort(key=lambda x: x["aci_score"], reverse=True)

            # We create bundles of up to 4 tasks (e.g. 1 Engg + 1 S&T + 1 TRD)
            while cluster_tasks:
                bundle = []
                used_depts = set()
                remaining = []

                for t in cluster_tasks:
                    # Prefer multi-department combinations
                    if t["department"] not in used_depts or len(used_depts) == len(depts_in_cluster):
                        # Add to current bundle if total duration fits in a standard window (<= 240 mins)
                        bundle_duration = max([b["duration_mins"] for b in bundle] + [t["duration_mins"]])
                        if bundle_duration <= 240:
                            bundle.append(t)
                            used_depts.add(t["department"])
                        else:
                            remaining.append(t)
                    else:
                        remaining.append(t)

                cluster_tasks = remaining
                if not bundle:
                    break

                # Determine primary work (the one with largest duration)
                primary_task = max(bundle, key=lambda x: x["duration_mins"])
                shadow_tasks = [t for t in bundle if t["defect_id"] != primary_task["defect_id"]]
                
                # Standalone hours vs Joint hours
                standalone_mins = sum(t["duration_mins"] for t in bundle)
                joint_mins = max(t["duration_mins"] for t in bundle)
                saved_mins = standalone_mins - joint_mins

                candidate_blocks.append({
                    "candidate_id": f"CAND-BLK-{block_counter:03d}",
                    "section": section,
                    "section_name": primary_task.get("section_name", section),
                    "track_id": track_id,
                    "primary_department": primary_task["department"],
                    "participating_departments": list(used_depts),
                    "is_joint_block": len(used_depts) > 1,
                    "tasks": bundle,
                    "task_count": len(bundle),
                    "max_aci_score": max(t["aci_score"] for t in bundle),
                    "total_aci": sum(t["aci_score"] for t in bundle),
                    "duration_mins": joint_mins,
                    "standalone_mins": standalone_mins,
                    "saved_mins": saved_mins,
                    "has_critical": any(t["priority_tier"] == "P1_CRITICAL" for t in bundle),
                    "earliest_due_days": min(t["target_completion_days"] for t in bundle),
                    "requires_power_block": any(t.get("requires_power_block", False) for t in bundle),
                    "requires_traffic_block": any(t.get("requires_traffic_block", True) for t in bundle),
                })
                block_counter += 1

        # Step 4: Available Corridor Slots over the Planning Horizon
        base_date = datetime.now()
        corridor_templates = get_corridor_slots()
        available_slots = []
        slot_idx = 0

        for day_offset in range(horizon_days):
            day_dt = base_date + timedelta(days=day_offset)
            day_name = day_dt.strftime("%a").upper() # MON, TUE, etc.
            date_str = day_dt.strftime("%Y-%m-%d")

            for tmpl in corridor_templates:
                if day_name in tmpl["allowed_days"]:
                    available_slots.append({
                        "slot_index": slot_idx,
                        "slot_id": f"{tmpl['slot_id']}-{date_str}",
                        "date": date_str,
                        "day_name": day_name,
                        "day_offset": day_offset,
                        "start_time": tmpl["start_time"],
                        "end_time": tmpl["end_time"],
                        "duration_mins": tmpl["duration_mins"],
                        "slot_type": tmpl["slot_type"],
                        "traffic_impact_factor": tmpl["traffic_impact_factor"]
                    })
                    slot_idx += 1

        # Step 5: Optimization formulation using SciPy MILP
        # Variables: x[b, s] = 1 if candidate block b is scheduled in slot s
        N_blocks = len(candidate_blocks)
        N_slots = len(available_slots)

        if N_blocks == 0 or N_slots == 0:
            return {"scheduled_blocks": [], "unassigned_tasks": [], "metrics": {}}

        # Flattened variables: length = N_blocks * N_slots
        num_vars = N_blocks * N_slots

        # Objective: Maximize (Score - Penalties) <=> Minimize -(Score - Penalties)
        # c_obj vector
        c_obj = np.zeros(num_vars)

        for b_idx, block in enumerate(candidate_blocks):
            for s_idx, slot in enumerate(available_slots):
                var_idx = b_idx * N_slots + s_idx
                
                # Benefit: Total ACI resolved + Joint Bonus
                benefit = block["total_aci"] * 1.5
                if block["is_joint_block"]:
                    benefit += 50.0 * len(block["participating_departments"])
                if block["has_critical"]:
                    benefit += 100.0

                # Penalties:
                # 1. Traffic disruption penalty
                disruption_cost = slot["traffic_impact_factor"] * 80.0
                # 2. Due date penalty if scheduled too late
                lateness = max(0, slot["day_offset"] - block["earliest_due_days"])
                lateness_cost = lateness * 40.0
                # 3. Night block preference for heavy jobs
                if block["duration_mins"] > 150 and slot["slot_type"] != "NIGHT_MAJOR" and slot["slot_type"] != "SUNDAY_MEGA":
                    disruption_cost += 50.0

                net_cost = -(benefit - disruption_cost - lateness_cost)
                c_obj[var_idx] = net_cost

        # Constraints:
        # Constraint 1: Each candidate block scheduled at most ONCE: sum_s x[b, s] <= 1
        A_rows = []
        b_l = []
        b_u = []

        for b_idx in range(N_blocks):
            row = np.zeros(num_vars)
            for s_idx in range(N_slots):
                row[b_idx * N_slots + s_idx] = 1.0
            A_rows.append(row)
            b_l.append(0.0)
            b_u.append(1.0)
        # Constraint 2: Daily block capacity
        # Limits the total number of maintenance blocks scheduled on each day.
        for day_offset in range(horizon_days):
            row = np.zeros(num_vars)
            has_member = False

            for b_idx, block in enumerate(candidate_blocks):
                for s_idx, slot in enumerate(available_slots):
                    if slot["day_offset"] == day_offset:
                        row[b_idx * N_slots + s_idx] = 1.0
                        has_member = True

            if has_member:
                A_rows.append(row)
                b_l.append(0.0)
                b_u.append(float(max_blocks_per_day))


        # Constraint 2: Each slot can host at most 1 block per section (no overlapping blocks in same slot/section)
        for s_idx in range(N_slots):
            for section in {b["section"] for b in candidate_blocks}:
                row = np.zeros(num_vars)
                has_member = False
                for b_idx, block in enumerate(candidate_blocks):
                    if block["section"] == section:
                        row[b_idx * N_slots + s_idx] = 1.0
                        has_member = True
                if has_member:
                    A_rows.append(row)
                    b_l.append(0.0)
                    b_u.append(1.0) # max 1 block per section per slot

        # Constraint 3: Duration constraint: block duration <= slot duration
        for b_idx, block in enumerate(candidate_blocks):
            for s_idx, slot in enumerate(available_slots):
                var_idx = b_idx * N_slots + s_idx
                if block["duration_mins"] > slot["duration_mins"]:
                    # Cannot schedule here: x[b, s] == 0
                    row = np.zeros(num_vars)
                    row[var_idx] = 1.0
                    A_rows.append(row)
                    b_l.append(0.0)
                    b_u.append(0.0)

        A_mat = np.array(A_rows)
        constraints = LinearConstraint(A_mat, b_l, b_u)
        integrality = np.ones(num_vars) # All binary variables
        bounds = Bounds(0, 1)

        # Solve MILP with HiGHS
        res = milp(c=c_obj, integrality=integrality, constraints=constraints, bounds=bounds)

        # Step 6: Extract solution
        scheduled_blocks = []
        scheduled_block_indices = set()

        if res.success:
            sol = res.x.round().astype(int)
            for b_idx, block in enumerate(candidate_blocks):
                for s_idx, slot in enumerate(available_slots):
                    var_idx = b_idx * N_slots + s_idx
                    if sol[var_idx] == 1:
                        scheduled_block_indices.add(b_idx)
                        traffic_info = evaluate_slot_disruption(slot["slot_id"].split("-")[0], block["section"], block["track_id"])
                        
                        block_record = {
                            "block_id": f"IR-BLK-{2026000 + len(scheduled_blocks) + 1}",
                            "date": slot["date"],
                            "day_name": slot["day_name"],
                            "slot_name": slot["slot_id"].split("-")[0] + " (" + slot["slot_type"] + ")",
                            "start_time": slot["start_time"],
                            "end_time": slot["end_time"],
                            "section": block["section"],
                            "section_name": block["section_name"],
                            "track_id": block["track_id"],
                            "allocated_duration_mins": block["duration_mins"],
                            "slot_window_mins": slot["duration_mins"],
                            "primary_department": block["primary_department"],
                            "departments": block["participating_departments"],
                            "is_joint_block": block["is_joint_block"],
                            "coordination_degree": f"{len(block['participating_departments'])}/3 Depts",
                            "requires_power_block": block["requires_power_block"],
                            "requires_traffic_block": block["requires_traffic_block"],
                            "tasks_bundled": block["tasks"],
                            "task_count": block["task_count"],
                            "standalone_duration_mins": block["standalone_mins"],
                            "downtime_saved_mins": block["saved_mins"],
                            "traffic_impact": traffic_info,
                            "approval_status": "PROPOSED",
                            "ai_confidence_pct": round(
                                min(
                                    99.0,
                                    88.0
                                    + (block["max_aci_score"] * 0.08)
                                    + (5.0 if block["is_joint_block"] else 0.0)
                                    + (3.0 if block["duration_mins"] <= slot["duration_mins"] else 0.0)
                                ),
                                1
                            )
                        }
                        scheduled_blocks.append(block_record)

        # Sort scheduled blocks by date & start time
        scheduled_blocks.sort(key=lambda x: (x["date"], x["start_time"]))

        # Identify unassigned tasks
        unassigned_tasks = []
        for b_idx, block in enumerate(candidate_blocks):
            if b_idx not in scheduled_block_indices:
                unassigned_tasks.extend(block["tasks"])

        # Step 7: Calculate System KPIs
        total_tasks = len(defects)
        scheduled_tasks_count = sum(b["task_count"] for b in scheduled_blocks)
        total_saved_hours = round(sum(b["downtime_saved_mins"] for b in scheduled_blocks) / 60.0, 1)
        joint_blocks_count = sum(1 for b in scheduled_blocks if b["is_joint_block"])
        joint_ratio = round((joint_blocks_count / len(scheduled_blocks) * 100.0) if scheduled_blocks else 0, 1)
        critical_total = sum(1 for t in ranked_tasks if t.get("priority_tier") == "P1_CRITICAL")
        critical_resolved = sum(1 for b in scheduled_blocks for t in b["tasks_bundled"] if t.get("priority_tier") == "P1_CRITICAL")
        critical_resolved = min(critical_resolved, critical_total)

        metrics = {
            "total_demands": total_tasks,
            "tasks_scheduled": scheduled_tasks_count,
            "schedule_fulfillment_pct": round((scheduled_tasks_count / total_tasks * 100.0) if total_tasks else 100, 1),
            "total_blocks_planned": len(scheduled_blocks),
            "joint_blocks_count": joint_blocks_count,
            "joint_coordination_pct": joint_ratio,
            "track_downtime_saved_hours": total_saved_hours,
            "critical_safety_resolved": f"{critical_resolved}/{critical_total}",
            "asset_availability_pct": round(95.0 + min(4.5, (total_saved_hours * 0.4)), 1),
            "solver_engine": "SciPy HiGHS Mixed-Integer Linear Programming + Shadow Bundler"
        }

        return {
            "scheduled_blocks": scheduled_blocks,
            "unassigned_tasks": unassigned_tasks,
            "metrics": metrics
        }

if __name__ == "__main__":
    from Backend.data_ingestion.tms_data import generate_tms_defects
    from Backend.data_ingestion.smms_data import generate_smms_defects
    from Backend.data_ingestion.tdms_data import generate_tdms_defects

    optimizer = IntegratedBlockOptimizer()
    tms = generate_tms_defects(12)
    smms = generate_smms_defects(10)
    tdms = generate_tdms_defects(10)

    result = optimizer.optimize_blocks(tms + smms + tdms, horizon_days=7)
    print("\n--- Optimization Complete ---")
    print(f"Total Blocks Scheduled: {len(result['scheduled_blocks'])}")
    print(f"Joint Blocks: {result['metrics']['joint_blocks_count']} ({result['metrics']['joint_coordination_pct']}%)")
    print(f"Downtime Saved: {result['metrics']['track_downtime_saved_hours']} hours")
    print(f"Asset Availability: {result['metrics']['asset_availability_pct']}%")
    
    if result['scheduled_blocks']:
        first_b = result['scheduled_blocks'][0]
        print(f"\nSample Block: {first_b['block_id']} on {first_b['date']} ({first_b['start_time']}-{first_b['end_time']})")
        print(f"  Section: {first_b['section']} {first_b['track_id']} | Depts: {first_b['departments']}")
        print(f"  Bundled Tasks: {first_b['task_count']} | Saved: {first_b['downtime_saved_mins']} mins")
