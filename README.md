# ENTWINE Energy Digital Twin - Phase 0 Setup

This repository contains the foundational infrastructure for **Phase 0 (Environment Setup)** and **Module 1 (Asset Registry)**.

## 1. Prerequisites

- Python 3.10+
- Docker Desktop with Docker Compose support
- Git Bash, PowerShell, or another shell

## 2. Start the PostgreSQL/TimescaleDB Container

1. Open a terminal in the project root.
2. Set the database password:
   - PowerShell:
     ```powershell
     $env:DB_PASSWORD = "replace_with_a_strong_password"
     ```
3. Start the database service:
   ```powershell
   docker compose up -d
   ```
4. Verify service health:
   ```powershell
   docker compose ps
   ```

## 3. Activate the Python Virtual Environment

1. Create the virtual environment:
   ```powershell
   python -m venv .venv
   ```
2. Activate it:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```
3. Install pinned dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

## 4. Configure Environment Variables

Create a `.env` file in the project root:

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

## 5. Initialize the Asset Registry

```powershell
python registry/init_registry.py
```

This runs `registry/schema.sql` (creates tables, indexes, view) then `registry/seed.sql`
(inserts PH-01 data). Both files are idempotent.

Expected output:

```text
INFO | registry.init_registry | === ENTWINE Asset Registry Initialization ===
INFO | registry.init_registry | Executing schema.sql (schema.sql) …
INFO | registry.init_registry | schema.sql executed successfully.
INFO | registry.init_registry | Executing seed.sql (seed.sql) …
INFO | registry.init_registry | seed.sql executed successfully.
INFO | registry.init_registry | === Registry initialization complete. ===
```

## 6. Validate the Registry

```powershell
$env:PYTHONUTF8=1
python registry/validate_registry.py
```

A passing run prints `MODULE 1: PASS`.

> **Windows note:** `$env:PYTHONUTF8=1` is required in PowerShell to correctly render
> the Unicode box-drawing characters in the output. Without it you will see a
> `UnicodeEncodeError` on cp1252 consoles.

For detailed setup instructions, validation queries, PH-01 metadata, and
dataset mapping notes, see [`registry/README.md`](registry/README.md).

## 7. Repository Structure

```
entwine/
  registry/        # Module 1 — Asset Registry (schema, seed, mapping, validation)
  ingestion/       # Module 2 — State Layer data loading (future)
  models/          # Module 3 — GridReason / GrCF / CAFA (future)
  forecasting/     # 3-month load prediction (future)
  interrogation/   # Agentic / RAG layer (future)
  dashboard/       # Frontend (future)
  logs/            # Experiment and validation logs
  Documents/       # Mentor reference documents (read-only)
```

## 8. Stop the Database Service

```powershell
docker compose down          # stops container, keeps volume
docker compose down -v       # stops container AND deletes volume (full reset)
```
