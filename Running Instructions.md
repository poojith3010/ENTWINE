# ENTWINE — Demonstration & Running Instructions

This guide provides the exact sequence of terminal commands to demonstrate the progress across **Module 1 (Asset Registry)** and **Module 2 (Historical State Layer)**.

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
