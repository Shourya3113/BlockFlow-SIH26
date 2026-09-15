"""
main.py - FastAPI REST Engine for Indian Railways Automatic Block Planning System (IR-ABPS)
Provides RESTful APIs connecting the multi-department data layers, AI prioritization engine,
SciPy HiGHS block optimizer, and the Chief Controller interactive frontend.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
from datetime import datetime

from .data_ingestion.tms_data import generate_tms_defects
from .data_ingestion.smms_data import generate_smms_defects
from .data_ingestion.tdms_data import generate_tdms_defects
from .data_ingestion.coa_data import get_corridor_slots, get_train_paths
from .ai_engine.prioritizer import AssetCriticalityPrioritizer
from .ai_engine.block_optimizer import IntegratedBlockOptimizer
from .horizons.weekly_planner import WeeklyBlockPlanner
from .horizons.monthly_planner import MonthlyBlockPlanner
from .ai_engine.multi_agent_system import prognostics_dnn, orchestrator_agent

app = FastAPI(
    title="IR-ABPS: Automatic Block Planning System",
    description="AI-Powered Multi-Department Maintenance Block Optimization for Indian Railways",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory application state
STATE = {
    "tms_defects": generate_tms_defects(20, seed=101),
    "smms_defects": generate_smms_defects(15, seed=102),
    "tdms_defects": generate_tdms_defects(15, seed=103),
    "approval_log": [],
}

prioritizer = AssetCriticalityPrioritizer()
optimizer = IntegratedBlockOptimizer()
weekly_planner = WeeklyBlockPlanner()
monthly_planner = MonthlyBlockPlanner()

# Cached plans
CACHE = {
    "weekly": None,
    "monthly": None
}

def get_all_raw_defects():
    return STATE["tms_defects"] + STATE["smms_defects"] + STATE["tdms_defects"]

def refresh_plans():
    all_def = get_all_raw_defects()
    CACHE["weekly"] = weekly_planner.generate_weekly_plan(all_def)
    CACHE["monthly"] = monthly_planner.generate_monthly_plan(all_def)

# Initial plan generation
refresh_plans()

class BlockActionRequest(BaseModel):
    block_id: str
    action: str # "APPROVE", "REJECT", "OVERRIDE"
    controller_id: str = "CHIEF_CTRL_MUMBAI"
    reason: Optional[str] = "Optimal multi-department coordination verified."

@app.get("/api/health")
def health():
    return {"status": "ONLINE", "system": "IR-ABPS", "time": datetime.now().isoformat()}

@app.get("/api/kpis")
def get_kpis():
    """Returns high-level system availability, downtime saved, and coordination KPIs."""
    if not CACHE["weekly"]:
        refresh_plans()
    weekly_metrics = CACHE["weekly"]["metrics"]
    
    all_def = get_all_raw_defects()
    tms_count = len(STATE["tms_defects"])
    smms_count = len(STATE["smms_defects"])
    tdms_count = len(STATE["tdms_defects"])

    critical_count = sum(1 for d in all_def if d.get("severity") == "CRITICAL")
    urgent_count = sum(1 for d in all_def if d.get("severity") == "URGENT")
    routine_count = sum(1 for d in all_def if d.get("severity") == "ROUTINE")

    return {
        "asset_availability_pct": weekly_metrics.get("asset_availability_pct", 96.8),
        "downtime_saved_hours": weekly_metrics.get("track_downtime_saved_hours", 14.5),
        "joint_coordination_pct": weekly_metrics.get("joint_coordination_pct", 45.0),
        "total_demands": len(all_def),
        "tasks_scheduled": weekly_metrics.get("tasks_scheduled", 0),
        "schedule_fulfillment_pct": weekly_metrics.get("schedule_fulfillment_pct", 0),
        "critical_safety_resolved": weekly_metrics.get("critical_safety_resolved", "0/0"),
        "department_breakdown": {
            "ENGINEERING_TMS": tms_count,
            "SNT_SMMS": smms_count,
            "ELECTRICAL_TDMS": tdms_count
        },
        "severity_breakdown": {
            "CRITICAL": critical_count,
            "URGENT": urgent_count,
            "ROUTINE": routine_count
        },
        "approvals_logged": len(STATE["approval_log"])
    }

@app.get("/api/defects")
def get_defects(department: Optional[str] = None, severity: Optional[str] = None):
    """Returns all defects across departments with computed ACI scores."""
    all_def = get_all_raw_defects()
    ranked = prioritizer.rank_maintenance_demands(all_def)
    
    if department:
        ranked = [d for d in ranked if d.get("department") == department]
    if severity:
        ranked = [d for d in ranked if d.get("severity") == severity]

    return {"count": len(ranked), "defects": ranked}

@app.get("/api/corridor/slots")
def get_corridor_info():
    """Returns COA corridor slots and sample train paths for time-distance graphing."""
    return {
        "slots": get_corridor_slots(),
        "train_paths": get_train_paths()
    }

@app.get("/api/schedule/weekly")
def get_weekly_schedule():
    """Returns the operational 7-day rolling block schedule."""
    if not CACHE["weekly"]:
        refresh_plans()
    return CACHE["weekly"]

@app.get("/api/schedule/monthly")
def get_monthly_schedule():
    """Returns the tactical 30-day master preventive maintenance plan."""
    if not CACHE["monthly"]:
        refresh_plans()
    return CACHE["monthly"]

@app.post("/api/optimize")
def trigger_optimization():
    """Manually re-runs optimization across all ingested data."""
    refresh_plans()
    return {
        "message": "Optimization re-calculated successfully with SciPy HiGHS solver.",
        "weekly_metrics": CACHE["weekly"]["metrics"],
        "monthly_metrics": CACHE["monthly"]["metrics"]
    }

@app.post("/api/action/grant")
def grant_or_override_block(req: BlockActionRequest):
    """Controller decision hook to approve, alter, or override an AI proposed block."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "block_id": req.block_id,
        "action": req.action,
        "controller_id": req.controller_id,
        "reason": req.reason
    }
    STATE["approval_log"].append(entry)

    # Update block status in cached weekly plan if present
    if CACHE["weekly"]:
        for blk in CACHE["weekly"]["scheduled_blocks"]:
            if blk["block_id"] == req.block_id:
                blk["approval_status"] = "GRANTED" if req.action == "APPROVE" else req.action

    return {"message": f"Block {req.block_id} updated to {req.action}", "log_entry": entry}

@app.get("/api/action/audit-log")
def get_audit_log():
    return {"log": STATE["approval_log"]}

@app.post("/api/agents/consensus")
def run_multi_agent_consensus(payload: Optional[Dict[str, Any]] = None):
    """Executes 5-agent cooperative consensus round across P-Way, TRD, S&T, and Traffic."""
    demands = payload.get("demands") if payload else None
    if not demands:
        demands = get_all_raw_defects()[:5]
    return orchestrator_agent.negotiate_and_schedule(demands)

@app.post("/api/dnn/predict-prognostics")
def run_dnn_prognostics(features: Optional[Dict[str, float]] = None):
    """Runs PyTorch Deep Residual Neural Network (RailTrackDefectDNN-v2) for failure & RUL prediction."""
    if not features:
        features = {
            "gmt_tonnage": 65.0,
            "asset_age_years": 12.0,
            "operating_temp_c": 38.5,
            "curvature_deg": 2.5,
            "days_since_maintenance": 45.0,
            "prior_flaw_count": 3.0,
            "has_speed_restriction": True,
            "traffic_density_trains_per_day": 180.0,
            "coastal_salinity_factor": 0.9
        }
    return prognostics_dnn.predict(features)

# Serve the frontend UI
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Frontend"))

if os.path.exists(FRONTEND_DIR):
    app.mount("/Frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend_static")

@app.get("/")
@app.get("/block_planner.html")
@app.get("/TTC.html")
def serve_index():
    ui_path = os.path.join(FRONTEND_DIR, "block_planner.html")
    if os.path.exists(ui_path):
        return FileResponse(ui_path)
    return {"message": "IR-ABPS API is running. block_planner.html not found."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
