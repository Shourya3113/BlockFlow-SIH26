# BlockFlow (IR-ABPS) — Builders Guidebook & Deployment Manual

**Smart India Hackathon (SIH 2026)**  
**Problem Statement ID:** `SIH26027`  
**Problem Statement Title:** *AI-Powered Automatic Block Planning to Maximize Asset Availability for Train Operations on Indian Railways*  
**Platform:** BlockFlow (IR-ABPS)  

---

## 1. System Requirements & Environment Setup

BlockFlow is engineered to execute completely **air-gapped** on standard Indian Railways divisional workstation hardware without external cloud or GPU requirements.

### Minimum Hardware Specifications:
* **Processor:** Intel Core i3 (8th Gen+) or AMD Ryzen 3
* **RAM:** 4 GB DDR4 (BlockFlow consumes `<250 MB`)
* **Storage:** 500 MB free disk space
* **OS:** Windows 10/11 or Ubuntu Linux 20.04+
* **Network:** Completely offline compatible (Zero external API dependencies)

### Software Prerequisites:
* Python 3.11+
* Modern Web Browser (Google Chrome, Microsoft Edge, or Mozilla Firefox with WebGL enabled)

---

## 2. Step-by-Step Service Startup Guide

### Step 1: Install Python Dependencies
Open PowerShell or Terminal inside the repository root (`D:\projects\TTC`):
```powershell
pip install -r Backend/requirements.txt
```

### Step 2: Start the FastAPI Backend Gateway
```powershell
python -m uvicorn Backend.main:app --host 127.0.0.1 --port 8000
```
* **Interactive API Documentation (Swagger):** `http://127.0.0.1:8000/docs`
* **Health Check Endpoint:** `http://127.0.0.1:8000/api/health`

### Step 3: Launch the Control Office Console & 3D Twin
Open your browser and navigate to:
```
http://127.0.0.1:8000
```
This loads `Frontend/block_planner.html` directly via FastAPI's static file mount, rendering:
1. **CesiumJS 3D Railway Digital Twin:** Western Railway 60km suburban corridor (Churchgate to Virar, 29 stations).
2. **Interactive Time-Distance String Graph:** Real-time train trajectories against scheduled joint mega blocks.
3. **Weekly & Monthly Multi-Horizon Schedulers:** Operational crossover diversions and heavy machine rosters.
4. **Chief Controller Telemetry HUD:** Live ACI scores, downtime counters, and 1-click digital grant permits.

### Step 4: Run the End-to-End Automated Test Suite
In a separate terminal window:
```powershell
python test_e2e.py
```
Expected output:
```
[PASS] Ingestion Layer: 50 CRIS requisitions validated
[PASS] ACI Engine: 100% Critical P1 flaws scored >= 80
[PASS] HiGHS Simplex Solver: Solved in 5.4ms (0 conflicts)
[PASS] Shadow Bundling: 45% multi-department coordination
[PASS] Crossover Diversion: 0 train cancellations
=====================================================
100% TESTS PASSED - SYSTEM READY FOR DEPLOYMENT
```

---

## 3. Universal API Documentation & JSON Payloads

### 1. Ingest Requisitions & Optimize Blocks
* **Endpoint:** `POST /api/optimize`
* **Sample Request Payload:**
```json
{
  "corridor": "Churchgate - Virar",
  "planning_horizon_days": 7,
  "requisitions": [
    {
      "asset_id": "TMS-ENG-1006",
      "department": "CIVIL",
      "km_start": 19.4,
      "km_end": 21.2,
      "line": "DN_FAST",
      "requested_duration_mins": 180,
      "work_type": "USFD_IMR_WELD",
      "fault_code": "USFD_IMR",
      "speed_restriction_psr": 30,
      "overdue_days": 4
    },
    {
      "asset_id": "SMMS-SNT-2012",
      "department": "SNT",
      "km_start": 19.8,
      "km_end": 20.2,
      "line": "DN_FAST",
      "requested_duration_mins": 90,
      "work_type": "POINT_MACHINE_TEST",
      "fault_code": "PM_OBSTRUCTION",
      "gear_id": "104B",
      "requires_disconnection": true
    },
    {
      "asset_id": "TDMS-TRD-1003",
      "department": "TRD",
      "km_start": 19.0,
      "km_end": 22.0,
      "line": "DN_FAST",
      "requested_duration_mins": 120,
      "work_type": "CONTACT_WIRE_HOTSPOT",
      "fault_code": "OHE_HOTSPOT",
      "hotspot_temp_c": 84.5,
      "feeding_post": "JOS"
    }
  ]
}
```

> **Inputs are engineering, not statutory.** A requisition says *what work, where, how long, how urgent*.
> It **must not** carry a `statutory_form` (Form T/351, Form T/352, ACTM PTW): those are legal instruments a
> Sectional Controller or Traction Power Controller issues only **after** a block has been scheduled and
> authorised, so a prospective requisition cannot hold one. BlockFlow derives which instruments the work
> will need (from `work_type`, the directorate and the declared operating context) and prefills them
> downstream, once the optimizer has allocated a window.

* **Sample Response Payload:**
```json
{
  "status": "OPTIMAL",
  "solver_latency_ms": 6.8,
  "total_downtime_saved_mins": 195,
  "passenger_cancellations": 0,
  "joint_blocks": [
    {
      "block_id": "JB-2026-04-WR",
      "corridor_section": "Bandra (BA) - Andheri (AND)",
      "line": "DN_FAST",
      "window_start": "01:15",
      "window_end": "04:30",
      "allocated_duration_mins": 195,
      "bundled_departments": ["CIVIL", "SNT", "TRD"],
      "primary_task": "TMS-ENG-1006 (USFD IMR Tamping)",
      "shadow_tasks": ["SMMS-SNT-2012", "TDMS-TRD-1003"],
      "traffic_diversion": {
        "crossover_station": "Bandra (BA)",
        "diverted_trains": ["EMU-90241"],
        "alternate_line": "DN_SLOW",
        "added_delay_mins": 0
      },
      "statutory_compliance": {
        "earthing_buffer_mins": 15,
        "forms_generated": ["T_351", "T_352", "ACTM_PTW"]
      }
    }
  ]
}
```

---

## 4. Offline Fallback & Live Jury Demo Resilience

To guarantee **zero downtime** during the live SIH jury evaluation round (even if WiFi completely fails):
1. **Local Self-Contained Assets:** All CesiumJS libraries, 3D building models, station coordinates, and satellite imagery tiles are cached locally in `Frontend/`.
2. **Pre-Compiled Simplex Binaries:** SciPy HiGHS runs natively via Python C++ extensions with zero network calls.
3. **Synthetic Live Telemetry Generator:** If live CRIS feeds are disconnected, BlockFlow automatically engages its built-in realistic Western Railway simulation engine, generating live train movements and defect alerts across the 60km corridor.
