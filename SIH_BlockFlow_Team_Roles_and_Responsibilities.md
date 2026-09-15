# BlockFlow (IR-ABPS) — Team Roles & Responsibilities

**Smart India Hackathon (SIH 2026)**  
**Problem Statement ID:** `SIH26027`  
**Problem Statement Title:** *AI-Powered Automatic Block Planning to Maximize Asset Availability for Train Operations on Indian Railways*  
**Team Name:** BlockFlow Systems Engineering  

---

## 1. Executive Team Charter & Leadership Strategy

Our 6-person core engineering team operates under strict modular code ownership. Every engineer owns specific subdirectories, modules, data contracts, and presentation defense topics:

```
+-----------------------------------------------------------------------------------+
|                        BLOCKFLOW CORE TEAM ROSTER & OWNERSHIP                     |
+-----------------------------------------------------------------------------------+
|  1. Peter    | Team Leader, Chief Architect & Central Orchestrator                |
|  2. Chhavi   | AI, Deep Learning & Operations Research Algorithms Lead            |
|  3. Pradipti | Research, Benchmarks, Quality Assurance (QA) & Pitch Lead          |
|  4. Achintya | Backend Lead & Core Systems Architect                              |
|  5. Vinayak  | Frontend & Interactive Client Experience Lead                      |
|  6. Misha    | Data Pipeline, Storage & Mathematical Validation Lead              |
+-----------------------------------------------------------------------------------+
```

---

## 2. Individual 1-Page Profiles & Code Ownership

### 2.1 Peter — Team Leader, Chief Architect & Central Orchestrator
* **Domain Focus:** End-to-end system architecture, workflow sequencing, dynamic tool routing, safety bounds.
* **Code Ownership:** `Backend/main.py`, `solver.py`, `Backend/orchestrator.py`, CI/CD pipelines.
* **Core Deliverables:** Multi-agent tool router, parameter guardrail enforcer, live jury presentation conductor.
* **Tech Stack:** Python 3.11, FastAPI, Git, Architecture & Systems Design.

### 2.2 Chhavi — AI, Deep Learning & Core Algorithms Lead
* **Domain Focus:** Mathematical optimization, SciPy HiGHS simplex engine, ML degradation forecasting.
* **Code Ownership:** `Backend/ai_engine/block_optimizer.py`, `Backend/ai_engine/ml_degradation_model.py`, `Backend/ai_engine/prioritizer.py`.
* **Core Deliverables:** Sub-18ms dual-revised simplex solver, ACI scoring engine, shadow block co-bounding logic.
* **Tech Stack:** SciPy HiGHS, NumPy, Scikit-learn, Mathematical Programming.

### 2.3 Pradipti — Research, Benchmarks, Quality Assurance (QA) & Pitch Lead
* **Domain Focus:** Empirical benchmarks, CAG report audit validation, automated test suites, AICTE pitch deck.
* **Code Ownership:** `test_e2e.py`, `tests/`, `benchmarks/`, `SIH2026-027 PS.pptx`.
* **Core Deliverables:** 100% passing E2E test suite, official 6-slide winning PPT deck, jury Q&A defense playbook.
* **Tech Stack:** Pytest, Python-pptx, Statistical Benchmarks, Operations Research literature.

### 2.4 Achintya — Backend Lead & Core Systems Architect
* **Domain Focus:** High-performance REST gateway, Pydantic v2 data contracts, observable execution telemetry.
* **Code Ownership:** `Backend/main.py`, `Backend/schemas/`, `Backend/telemetry.py`, `Backend/reports/`.
* **Core Deliverables:** OpenAPI endpoints, auditable JSON execution trace logger, 1-click digital grant permit issuer.
* **Tech Stack:** FastAPI, Pydantic v2, Uvicorn, SQLite, RESTful Architecture.

### 2.5 Vinayak — Frontend & Interactive Client Experience Lead
* **Domain Focus:** Dark-mode glassmorphism dashboard, CesiumJS 3D digital twin, interactive string charts.
* **Code Ownership:** `Frontend/block_planner.html`, `Frontend/TTC.html`, `Frontend/src/`, `Frontend/public/`.
* **Core Deliverables:** 60 FPS Cesium 3D railway twin, SVG time-distance train graph, live telemetry inspector HUD.
* **Tech Stack:** CesiumJS, JavaScript ES6+, HTML5 Canvas/SVG, Tailwind CSS.

### 2.6 Misha — Data Pipeline, Storage & Mathematical Validation Lead
* **Domain Focus:** Ingestion parsers, WGS84 GPS coordinate snapping, invariant safety checks.
* **Code Ownership:** `Backend/data_ingestion/tms_data.py`, `smms_data.py`, `tdms_data.py`, `coa_data.py`.
* **Core Deliverables:** Unified spatial ingestion pipeline, 439-point WGS84 Western Railway waypoint database.
* **Tech Stack:** Pandas, GeoJSON, Shapely, SQLite, Mathematical Verification.

---

## 3. Day-1 'Zero-Blocker' Mock Contract Matrix

To ensure that AI, Backend, Frontend, and Research tracks build concurrently without waiting on each other, strict universal schemas are frozen on Day 1:

```
┌──────────────┐             Pydantic v2 Contract             ┌──────────────┐
│    MISHA     ├─────────────────────────────────────────────►│    CHHAVI    │
│(Data Pipeline│   TMS / SMMS / TDMS Normalized Requisitions  │ (HiGHS Solver│
│  & Waypoints)│   [asset_id, dept, km_start, km_end, aci]    │ & Bundling)  │
└──────┬───────┘                                              └──────┬───────┘
       │                                                             │
       │ Pydantic v2 Contract                         Output Matrix  │
       ▼                                                             ▼
┌──────────────┐       Scheduled Blocks & Divert Paths        ┌──────────────┐
│   ACHINTYA   ├─────────────────────────────────────────────►│   VINAYAK    │
│(FastAPI REST │   /api/schedule/weekly, /api/simulate        │  (Cesium 3D  │
│  & Telemetry)│   [block_id, window, status, trace, metrics] │ & SVG Graph) │
└──────────────┘                                              └──────────────┘
```

---

## 4. 36-Hour Hackathon Hour-by-Hour Battle Plan

```
+-----------------------------------------------------------------------------------+
|                     36-HOUR SIH HACKATHON TIMELINE & MILESTONES                   |
+-----------------------------------------------------------------------------------+
| Hours 00–06 | PHASE 1: Data Contracts, Waypoint Mapping & Environment Lockdown    |
|             | - Misha locks Pydantic schemas; Peter configures repo & CI/CD       |
|             | - Chhavi benchmarks HiGHS dummy solver; Achintya stubs REST gateway  |
|             | - Vinayak initialises Cesium 3D globe with Mumbai satellite tiles   |
|-------------|---------------------------------------------------------------------|
| Hours 06–14 | PHASE 2: Core Algorithm Development & 3D Spatial Canvas Wiring      |
|             | - Chhavi implements ACI scoring & shadow bundling simplex model     |
|             | - Misha ingests 50 real-world Western Railway defect requisitions   |
|             | - Achintya connects HiGHS engine to FastAPI /api/optimize endpoint   |
|             | - Vinayak renders 4-track quad corridor and 29 station pins         |
|-------------|---------------------------------------------------------------------|
| Hours 14–22 | PHASE 3: Integration, Scissors Crossover Pathfinding & HUD Telemetry|
|             | - Peter wires orchestrator; Chhavi implements Bandra crossover SLW  |
|             | - Vinayak adds 3D extruded purple block envelopes & train icons      |
|             | - Pradipti benchmarks solver latency (<18ms) and downtime savings   |
|-------------|---------------------------------------------------------------------|
| Hours 22–28 | PHASE 4: Automated Verification, Stress Testing & Edge Cases        |
|             | - Pradipti executes test_e2e.py across 500 blocks (100% pass rate)  |
|             | - Achintya logs immutable JSON execution traces for jury audit      |
|             | - Misha stress-tests malformed payloads and out-of-boundary KMs     |
|-------------|---------------------------------------------------------------------|
| Hours 28–32 | PHASE 5: Presentation Deck Finalization & Live Demo Rehearsal       |
|             | - Pradipti & Peter polish 6-slide PPT deck with large typography    |
|             | - Verify real 3D Cesium screenshot on Slide 2                       |
|             | - Rehearse 3-minute pitch and 5-minute technical jury defense        |
|-------------|---------------------------------------------------------------------|
| Hours 32–36 | PHASE 6: Grand Finale Jury Evaluation Round                         |
|             | - Flawless 100% offline air-gapped demo to Ministry of Railways jury |
|             | - Answer judge questions with CAG Report No. 22 audit data          |
+-----------------------------------------------------------------------------------+
```

---

## 5. Individual Jury Q&A Battle Cards

* **Peter (Orchestrator):** Defends system modularity, the decision to avoid generative AI in safety-critical dispatch, and CRIS BDMS/COA interoperability.
* **Chhavi (AI/Algorithms):** Defends the HiGHS dual-revised simplex formulation, shadow bundling mathematical proof, and sub-18ms latency curves.
* **Pradipti (Research/QA):** Defends CAG Report No. 22 audit data (62% machine idling failure), IRPWM Para 268b/284 compliance, and the 100% automated test pass rate.
* **Achintya (Backend/Systems):** Defends Pydantic v2 universal contracts, the auditable JSON execution trace logger, and `<250MB RAM` sovereign air-gapped deployment.
* **Vinayak (Frontend/3D Twin):** Defends the 60 FPS CesiumJS WGS84 3D twin, 4-track quad visualization, and the real-time time-distance string chart.
* **Misha (Data/Validation):** Defends 439 WGS84 surveyed waypoints, ACTM Para 204 electrical earthing buffers, and IRSEM Form T/351 disconnection memos.
