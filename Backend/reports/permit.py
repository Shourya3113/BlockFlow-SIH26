from datetime import datetime
from pathlib import Path
import json


REPORT_DIR = Path("Backend") / "reports" / "generated"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def generate_grant_permit(block, trace_id, controller_id):
    """Generate an auditable digital block grant permit."""

    permit = {
        "permit_type": "DIGITAL_BLOCK_GRANT",
        "permit_id": f"PERMIT-{block['block_id']}",
        "block_id": block["block_id"],
        "section": block["section"],
        "track_id": block["track_id"],
        "window": {
            "date": block["date"],
            "start_time": block["start_time"],
            "end_time": block["end_time"]
        },
        "departments": block["departments"],
        "requires_power_block": block["requires_power_block"],
        "requires_traffic_block": block["requires_traffic_block"],
        "controller_id": controller_id,
        "status": "GRANTED",
        "trace_id": trace_id,
        "issued_at": datetime.now().isoformat()
    }

    path = REPORT_DIR / f"{permit['permit_id']}.json"

    with path.open("w", encoding="utf-8") as f:
        json.dump(permit, f, indent=2)

    return permit