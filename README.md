# ENTWINE Energy Digital Twin

ENTWINE is a full-lifecycle energy digital twin platform integrating structural asset registration, high-frequency historical state ingestion, physics-informed AI modeling, forecasting, and natural language interrogation.

---

## 1. Prerequisites

- Python 3.10+
- Docker Desktop with Docker Compose
- PowerShell, Bash, or terminal shell with UTF-8 support

---

## 2. Start PostgreSQL / TimescaleDB

1. Open a terminal in the project root.
2. Set the database password (or configure `.env`):
   ```powershell
   $env:DB_PASSWORD = "replace_with_a_strong_password"
   ```
3. Start the containerized database service:
   ```powershell
   docker compose up -d
   ```
4. Verify service status:
   ```powershell
   docker compose ps
   ```

---

## 3. Environment & Virtual Environment Setup

1. Create and activate the Python virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. Install pinned dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
3. Configure your `.env` file:
   ```env
   DB_HOST=localhost
   DB_PORT=5432
   DB_NAME=entwine_twin
   DB_USER=entwine_admin
   DB_PASSWORD=replace_with_a_strong_password
   DB_SSLMODE=prefer
   DB_POOL_SIZE=5
   DB_MAX_OVERFLOW=10
   DB_POOL_TIMEOUT=30
   DB_POOL_RECYCLE=1800
   ```

---

## 4. Module 1: Asset Registry Initialization

```powershell
# 1. Initialize schema and static seed data
python registry/init_registry.py

# 2. Validate Module 1 integrity
$env:PYTHONUTF8=1
python registry/validate_registry.py
```

Expected output: `MODULE 1: PASS`

---

## 5. Module 2: Historical State Layer Ingestion

Module 2 ingests all historical energy telemetry, daily summary workbooks, alarms, events, and incidents from `real time energy data/` into TimescaleDB and PostgreSQL.

### Execution Workflow

```powershell
# 1. Apply Module 2 database migrations (creates hypertables, measurement_sources, audit tables)
$env:PYTHONUTF8=1; python -m ingestion.run --migrate

# 2. Register mapped assets and create mapping snapshots with approval provenance
$env:PYTHONUTF8=1; python -m ingestion.run --register-assets

# 3. Dry-run ingestion simulation (profiles and validates parsing without state writes)
$env:PYTHONUTF8=1; python -m ingestion.run --dry-run

# 4. Full historical ingestion of all 29 source files
$env:PYTHONUTF8=1; python -m ingestion.run --ingest

# 5. Run Module 2 automated quality gates (15 assertions)
$env:PYTHONUTF8=1; python -m ingestion.run --validate

# 6. Generate structured reconciliation report
$env:PYTHONUTF8=1; python -m ingestion.run --reconcile

# 7. Idempotent rerun (safely skips already-ingested files using SHA-256 checksums)
$env:PYTHONUTF8=1; python -m ingestion.run --rerun
```

### Run Unit and Integration Tests

```powershell
$env:PYTHONUTF8=1; python -m unittest discover tests
```

---

## 6. Architecture & Data Flow

```
real time energy data/
├── POWERHOUSE_1/*.xls         ──> [ tabular_xls.py ]    ──> interval_telemetry (TimescaleDB hypertable)
├── daily energy report/*.xls  ──> [ daily_xls.py ]      ──> daily_energy_reports (PostgreSQL)
├── Alarms/1st_fllor_*.csv     ──> [ measurement_csv.py] ──> interval_telemetry (narrow format)
└── Alarms/Alarm|Event|*.csv   ──> [ alarm_csv.py ]      ──> operational_events (SHA-256 fingerprinted)
                                           │
                                    [ writer.py ] (Batch tx, 1000 rows/batch, ON CONFLICT DO NOTHING)
                                           │
                                 [ measurement_sources ] ──> meters / equipment (Module 1 lookup)
                                 [ rejected_records ]    <── Quarantined feeders (FROM/TO_POWERHOUSE_2)
                                 [ source_files ]        <── SHA-256 manifest & processing status
```

---

## 7. Repository Structure

```
ENTWINE/
├── registry/          # Module 1: Asset Registry (schema, seed, mapping, validation)
├── migrations/        # Versioned SQL migrations (Module 2 state layer DDL)
├── ingestion/         # Module 2: Historical State Layer Ingestion Engine
│   ├── readers/       # Parsers for tabular XLS, daily XLS, alarms/events CSV, and measurement CSV
│   ├── config.py      # Database settings and paths
│   ├── discover.py    # Source file discovery and SHA-256 calculation
│   ├── mapping.py     # asset_mapping.csv validator and loader
│   ├── migrate.py     # Checksum-protected migration runner
│   ├── normalize.py   # IST -> UTC timestamp normalisation and unit extraction
│   ├── quality.py     # 15 automated data quality gate checks
│   ├── reconcile.py   # Reconciliation report generator
│   ├── register_assets.py # Mapping-driven static asset registration
│   ├── run.py         # Unified CLI entry point
│   └── writer.py      # Transactional batch writer
├── tests/             # Comprehensive unit and integration test suite
├── logs/              # Source profiles, validation logs, and reconciliation reports
├── models/            # Module 3: GridReason / GrCF / CAFA (future)
├── forecasting/       # 3-Month Load Forecasting Layer (future)
├── interrogation/     # Agentic & RAG Natural Language Interrogation (future)
├── dashboard/         # Real-time Web Twin Visualization (future)
├── Documents/         # Mentor reference specifications (read-only)
├── docker-compose.yml # TimescaleDB & PostgreSQL container configuration
└── requirements.txt   # Pinned project dependencies
```

---

## 8. Stopping the Database

```powershell
docker compose down          # stops container, preserves volume data
```
