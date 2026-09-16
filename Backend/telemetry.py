import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


TRACE_DIR = Path("Backend") / "traces"
TRACE_DIR.mkdir(parents=True, exist_ok=True)


class ExecutionTelemetry:
    """Creates auditable JSON execution traces for backend operations."""

    def __init__(self):
        self.traces: Dict[str, Dict[str, Any]] = {}

    def start_trace(
        self,
        operation: str,
        input_summary: Optional[Dict[str, Any]] = None
    ) -> str:
        trace_id = f"blockflow-exec-{uuid.uuid4().hex[:8]}"

        trace = {
            "trace_id": trace_id,
            "operation": operation,
            "started_at": datetime.now().isoformat(),
            "status": "RUNNING",
            "input_summary": input_summary or {},
            "steps": [],
            "output_summary": {},
            "metrics": {}
        }

        self.traces[trace_id] = trace
        return trace_id

    def start_step(
        self,
        trace_id: str,
        step_name: str,
        details: Optional[Dict[str, Any]] = None
    ) -> float:
        start = time.perf_counter()

        self.traces[trace_id]["steps"].append({
            "step_name": step_name,
            "started_at": datetime.now().isoformat(),
            "status": "RUNNING",
            "details": details or {}
        })

        return start

    def finish_step(
        self,
        trace_id: str,
        start_time: float,
        status: str = "SUCCESS",
        details: Optional[Dict[str, Any]] = None
    ):
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        step = self.traces[trace_id]["steps"][-1]
        step["status"] = status
        step["duration_ms"] = duration_ms

        if details:
            step["details"].update(details)

    def finish_trace(
        self,
        trace_id: str,
        status: str = "SUCCESS",
        output_summary: Optional[Dict[str, Any]] = None
    ):
        trace = self.traces[trace_id]

        trace["status"] = status
        trace["completed_at"] = datetime.now().isoformat()
        trace["output_summary"] = output_summary or {}

        total_ms = sum(
            step.get("duration_ms", 0)
            for step in trace["steps"]
        )

        trace["metrics"]["total_execution_ms"] = round(total_ms, 2)

        self._persist(trace)

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        return self.traces.get(trace_id)

    def _persist(self, trace: Dict[str, Any]):
        path = TRACE_DIR / f"{trace['trace_id']}.json"

        with path.open("w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, default=str)


telemetry = ExecutionTelemetry()