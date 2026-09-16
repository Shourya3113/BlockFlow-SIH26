from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class MaintenanceDemand(BaseModel):
    defect_id: str
    department: str
    system: str

    section: str
    section_name: str
    track_id: str

    km_start: float
    km_end: float

    defect_type: str
    description: str
    severity: str
    safety_weight: float = Field(ge=0.0, le=1.0)

    psr_speed_kmph: Optional[float] = None
    duration_mins: int = Field(gt=0)

    days_overdue: int = Field(ge=0)
    target_completion_days: int = Field(gt=0)
    gmt: float

    requires_traffic_block: bool
    requires_power_block: bool

    status: str
    reported_date: str

    gear_id: Optional[str] = None
    substation: Optional[str] = None

    aci_score: Optional[float] = None
    priority_tier: Optional[str] = None
    ml_telemetry: Optional[Dict[str, Any]] = None


class BlockActionRequest(BaseModel):
    block_id: str
    action: str
    controller_id: str
    reason: Optional[str] = None


class ScheduledBlock(BaseModel):
    block_id: str
    date: str
    day_name: str
    slot_name: str
    start_time: str
    end_time: str

    section: str
    section_name: str
    track_id: str

    allocated_duration_mins: int
    slot_window_mins: int

    primary_department: str
    departments: List[str]

    is_joint_block: bool
    coordination_degree: str

    requires_power_block: bool
    requires_traffic_block: bool

    tasks_bundled: List[Dict[str, Any]]
    task_count: int

    standalone_duration_mins: int
    downtime_saved_mins: int

    traffic_impact: Dict[str, Any]

    approval_status: str
    ai_confidence_pct: Optional[float] = None

    train_regulation_orders: List[str] = []


class WeeklyScheduleResponse(BaseModel):
    horizon: str
    horizon_days: int
    generated_at: str

    scheduled_blocks: List[ScheduledBlock]
    unassigned_tasks: List[Dict[str, Any]]
    metrics: Dict[str, Any]


class MonthlyScheduleResponse(BaseModel):
    horizon: str
    horizon_days: int
    generated_at: str

    calendar_weeks: Dict[str, List[ScheduledBlock]]
    machine_deployments: List[Dict[str, str]]
    departmental_hours_allocated: Dict[str, float]

    total_blocks_in_month: int
    scheduled_blocks: List[ScheduledBlock]
    metrics: Dict[str, Any]