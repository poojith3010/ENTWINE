# ENTWINE — Demonstration & Running Instructions

This guide provides the exact sequence of terminal commands to demonstrate the progress across **Module 1 (Asset Registry)** and **Module 2 (Historical State Layer)**.

---

## 🚀 Start From Scratch (Complete Reset)

Use this section for a full clean run — database bootstrap, ingestion, anomaly detection, and dashboard launch in one go.

### Step 1 — Start Docker (Database)

Open PowerShell in the project root (`C:\Users\Vijey\Documents\ENTWINE`):

```powershell
cd c:\Users\Vijey\Documents\ENTWINE

# Start TimescaleDB
docker compose up -d

# Wait ~15 seconds, then confirm it is healthy
docker compose ps
```

> Wait until the STATUS column shows `(healthy)` before continuing.

---

### Step 2 — Activate Environment

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = 1
```

---

### Step 3 — Initialize the Database Schema

```powershell
# Creates all tables: buildings, meters, state_telemetry, anomaly_events, etc.
python registry/init_registry.py
```

---

### Step 4 — Run the Full Block A Pipeline

```powershell
# Registers assets + ingests 502,920 rows + detects anomalies + explains + verifies
python run_block_a_pipeline.py --reset --verify
```

This single command does **everything**:

| Stage | What it does |
|---|---|
| **Seed** | Registers PH-A-BLK building and PH-A-MAIN meter |
| **Ingest** | Loads all 502,920 Block A XLS rows into `state_telemetry` |
| **CAFA** | XGBoost + Isolation Forest anomaly detection |
| **GrCF** | DDPM-warmed LSTM counterfactual explanation |
| **GridReason** | Writes natural language operator alert to the database |
| **Verify** | Asserts all 5 pipeline outputs are present in the database |

> ⏱️ **Expected runtime: ~7–8 minutes** (ingestion of 502k rows is the bottleneck)

Expected verification output:
```
[PASS] PH-A-BLK building registered         — result: 1
[PASS] PH-A-MAIN meter registered            — result: 1
[PASS] state_telemetry has Block A data      — result: 502920
[PASS] anomaly_events has ≥1 event           — result: 1
[PASS] GridReason explanation written        — result: 1
Verification: ALL CHECKS PASSED ✓
```

---

### Step 5 — Launch Live Services

**Option A — All-in-one (recommended):**

```powershell
python run_block_a_pipeline.py --skip-models --start-services
```

**Option B — Two separate terminals:**

```powershell
# Terminal 1: FastAPI REST backend
uvicorn api.main:app --host 127.0.0.1 --port 8000

# Terminal 2: Gradio dashboard
python dashboard/app.py
```

Open in your browser:

| Service | URL |
|---|---|
| Gradio Dashboard | http://127.0.0.1:7860 |
| FastAPI Interactive Docs | http://127.0.0.1:8000/docs |
| Health Check | http://127.0.0.1:8000/health |
| Anomaly Events | http://127.0.0.1:8000/api/v1/anomalies/PH-A-MAIN |
| Telemetry | http://127.0.0.1:8000/api/v1/telemetry/PH-A-MAIN |

---

### TL;DR — Full Sequence Copy-Paste

```powershell
cd c:\Users\Vijey\Documents\ENTWINE
docker compose up -d
# wait ~15 seconds for (healthy)
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = 1
python registry/init_registry.py
python run_block_a_pipeline.py --reset --verify
python run_block_a_pipeline.py --skip-models --start-services
```

---

## Step 1: Environment & Character Encoding Setup

Open PowerShell in the project root (`C:\Users\Vijey\Documents\ENTWINE`):

```powershell
# Activate virtual environment and enable UTF-8 character encoding
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = 1
```

---

## Step 2: Show Automated Test Suite (20 Tests Passing)

Demonstrates unit tests for timestamp conversions, mapping CSV validations, XLS/CSV parsers, deduplication fingerprints, and database migration idempotency:

```powershell
python -m unittest discover tests -v
```

> **Expected Result:** `Ran 20 tests in ... OK`

---

## Step 3: Show Module 1 Asset Registry Validation

Demonstrates that the foundational asset registry schema, seed data, and foreign key relationships are intact:

```powershell
python registry/validate_registry.py
```

> **Expected Output:**
> ```text
> ============================================================
>   MODULE 1: PASS
> ============================================================
> ```

---

## Step 4: Show Module 2 Automated Quality Gates (15/15 Passed)

Demonstrates the 15 integrity assertions across TimescaleDB hypertables, UTC timestamps, feeder quarantining, and Module 1 isolation:

```powershell
python -m ingestion.run --validate
```

> **Expected Output:**
> ```text
> [PASS] All 29 source files in source_files — found 29
> [PASS] No NULL processing_status in source_files — 0 NULL rows
> [PASS] All unresolved files have unresolved_reason — 0 missing
> [PASS] POWERHOUSE_1_INCOMER has rows_curated = 0 — rows_curated = 0
> [PASS] measurement_sources has rows — 12 rows
> [PASS] FROM/TO_POWERHOUSE_2 measurement_sources are quarantined — 2/2 quarantined
> [PASS] No NULL measurement_source_id in interval_telemetry — 0 NULL rows
> [PASS] interval_telemetry is a TimescaleDB hypertable
> [PASS] All interval_telemetry timestamps are valid UTC TIMESTAMPTZ — 0 invalid
> [PASS] No NULL row_fingerprint in operational_events — 0 NULL
> [PASS] Row totals reconcile (read = curated + rejected + skipped) — 0 files with mismatched totals
> [PASS] Module 1 buildings table has rows (not wiped) — 1 rows
> [PASS] Module 1 meters table has rows (not wiped) — 8 rows
> [PASS] Module 1 equipment table has rows (not wiped) — 2 rows
> [PASS] Module 1 baseline_parameters table exists
> Quality gates: 15/15 passed ✓ ALL PASS
> ```

---

## Step 5: Show Ingestion Idempotency & Reconciliation Summary

Demonstrates that all 29 historical source files are processed, cataloged by SHA-256, and safely skipped upon re-running:

```powershell
python -m ingestion.run --rerun
```

> **Key Metrics Demonstrated:**
> - Total files: **29** (12 interval XLS, 7 daily XLS, 10 alarms/events CSV)
> - Total rows read: **351,519**
> - Curated records: **284,462** (281,895 15-min interval readings + 2,130 daily reports + 345 operational events + 92 1st-floor CSV)
> - Quarantined feeder records: **67,057** safely routed to `rejected_records`
> - Zero-data files (`POWERHOUSE_1_INCOMER`) properly handled with status `no_data`.

---

## Step 6: Generate & View Reconciliation Report

Generate and view the audit report directly:

```powershell
python -m ingestion.run --reconcile
```

Open the latest report generated inside `logs/` to review the complete breakdown by source name, date ranges, and alarm classifications.

---

## Step 7: Live Database Query Demonstration

Run a quick Python snippet to show live readings from TimescaleDB:

```powershell
python -c "
import psycopg2, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(f'host={os.getenv(\"DB_HOST\")} dbname={os.getenv(\"DB_NAME\")} user={os.getenv(\"DB_USER\")} password={os.getenv(\"DB_PASSWORD\")}')
cur = conn.cursor()

print('\n=== LIVE TIMESCALEDB TELEMETRY SAMPLE (A_BLOCK) ===')
cur.execute('SELECT ts, source_name, real_power_kw, current_avg_a, frequency_hz FROM interval_telemetry WHERE source_name=\'POWERHOUSE_1.A_BLOCK\' ORDER BY ts DESC LIMIT 5;')
for r in cur.fetchall():
    print(r)

print('\n=== OPERATIONAL ALARMS/INCIDENTS SUMMARY ===')
cur.execute('SELECT event_class, COUNT(*) FROM operational_events GROUP BY event_class;')
for r in cur.fetchall():
    print(f'  {r[0]}: {r[1]} events')
"
```

---

## Section 8: Block A End-to-End Digital Twin Demonstration

This section demonstrates the complete ENTWINE intelligence pipeline for
**Powerhouse 1 Block A** — from asset registration through anomaly detection,
counterfactual explanation, and live dashboard visualization.

### Prerequisites

Ensure Docker is running and the database is healthy:

```powershell
docker compose ps    # STATUS should show (healthy)
```

Activate the virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = 1
```

---

### Option A — Unified Pipeline Runner (Recommended)

The `run_block_a_pipeline.py` script executes all 5 stages end-to-end
and verifies the result:

```powershell
# Fresh run: wipes Block A data and re-runs everything from scratch
python run_block_a_pipeline.py --reset --verify

# Idempotent re-run: skips existing data, adds nothing if already complete
python run_block_a_pipeline.py --verify

# Launch live services (FastAPI + Gradio) after pipeline completes
python run_block_a_pipeline.py --start-services
```

> **Expected Verification Output:**
> ```
> [PASS] PH-A-BLK building registered — result: 1
> [PASS] PH-A-MAIN meter registered — result: 1
> [PASS] state_telemetry has Block A data — result: 502920
> [PASS] anomaly_events has at least 1 event for PH-A-MAIN — result: ≥1
> [PASS] GridReason explanation written — result: ≥1
> Verification: ALL CHECKS PASSED ✓
> ```

---

### Option B — Manual Step-by-Step

Run each stage individually for inspection:

```powershell
# Step 1: Register Block A assets
python registry/seed_a_block.py

# Step 2: Ingest A Block historical telemetry (idempotent)
python -m ingestion.ingest_a_block

# Step 3: CAFA anomaly detection + GrCF counterfactual explanation
python -m models.orchestrator

# Step 4: GridReason natural language translation
python -m models.gridreason_llm

# Step 5: Verify all pipeline outputs
python run_block_a_pipeline.py --skip-models --verify
```

---

### Step 9: View Live Digital Twin Dashboard

Start the FastAPI REST backend:

```powershell
uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

In a second terminal, start the Gradio dashboard:

```powershell
python dashboard/app.py
```

Open the dashboard at **http://127.0.0.1:7860** and click **Refresh Data** to
load the latest CAFA/GrCF anomaly event with its GridReason explanation and
16-step counterfactual tensor.

REST API endpoints:
- **Health**: http://127.0.0.1:8000/health
- **Telemetry**: http://127.0.0.1:8000/api/v1/telemetry/PH-A-MAIN
- **Anomalies**: http://127.0.0.1:8000/api/v1/anomalies/PH-A-MAIN
- **Interactive Docs**: http://127.0.0.1:8000/docs

---

### Step 10: Run Full Test Suite (23 Unit + End-to-End Tests)

```powershell
# All 23 existing unit + integration tests
python -m pytest tests/ -v

# Block A end-to-end integration tests only
python -m pytest tests/test_block_a_e2e.py -v
```

> **Expected:** All tests pass (green).
