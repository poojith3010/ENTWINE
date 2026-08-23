# ENTWINE Energy Digital Twin - Phase 0 Setup

This repository contains the foundational infrastructure for **Phase 0 (Environment Setup)** and **Module 1 (Asset Registry)**.

## 1. Prerequisites

- Python 3.14+
- Docker Desktop with Docker Compose support
- Git Bash, PowerShell, or another shell

## 2. Start the PostgreSQL/TimescaleDB Container

1. Open a terminal in the project root.
2. Set the same strong database password used by the migration script:
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

1. Create the virtual environment (already done during setup if present):
   ```powershell
   C:/Users/HAI/AppData/Local/Programs/Python/Python314/python.exe -m venv .venv
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

Create a `.env` file in the project root with the following values:

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

The `DB_PASSWORD` value is used by both Docker Compose and the migration script.
If the database volume already exists and was created with another password, reset
the database role password inside the running container or recreate the volume.

## 5. Initialize the Asset Registry Schema

Run the migration script:

```powershell
python registry/init_registry.py
```

Expected successful output:

```text
INFO | registry.init_registry | Starting registry schema initialization.
INFO | registry.init_registry | Registry schema initialized successfully.
```

This creates the schema idempotently, inserts the `KCT Powerhouse Block`
building and `PH-01` meter seed data, and writes detailed logs to
`logs/init_registry.log`.

## 6. Stop the Database Service (Optional)

```powershell
docker compose down
```

To remove the persisted database volume as well:

```powershell
docker compose down -v
```
