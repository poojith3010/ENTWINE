# ENTWINE — Asset Registry (Module 1)

## Overview

Module 1 establishes the **static Asset Registry** — the structural foundation of the ENTWINE digital twin. It answers the question:

> **"What physical assets exist, and how are they related?"**

The registry contains four tables and one view:

| Object | Purpose |
|---|---|
| `buildings` | One row per campus building |
| `meters` | Physical metering points; supports a main → sub-panel hierarchy via `parent_meter_id` |
| `equipment` | Major loads/generation assets (HVAC, generators, switchgear, …) |
| `baseline_parameters` | Slowly-changing reference values for the model layer |
| `twin_instances` | Convenience view: building + its main meter |

**What Module 1 does NOT contain:**
- Time-series energy/current/voltage measurements (→ Module 2 State Layer)
- Alarm or event history (→ Module 2)
- GridReason / GrCF / CAFA logic (→ Module 3)

---

## Prerequisites

- Docker Desktop (for the TimescaleDB container)
- Python 3.10+
- pip packages from `requirements.txt`
- A `.env` file in the project root (see below)

---

## 1. Start the Database

```powershell
# From the project root
docker compose up -d
docker compose ps          # wait until health = healthy
```

---

## 2. Create `.env`

Copy the template below and fill in the password you chose when starting Docker:

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

The `DB_PASSWORD` value must match `${DB_PASSWORD}` in `docker-compose.yml`.

---

## 3. Install Python Dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## 4. Initialise the Registry

```powershell
python registry/init_registry.py
```

This runs **`schema.sql`** (creates tables, indexes, view) and then **`seed.sql`** (inserts PH-01 data), in that order. Both files are idempotent — safe to run multiple times.

Expected output:
```
INFO | registry.init_registry | === ENTWINE Asset Registry Initialization ===
INFO | registry.init_registry | Executing schema.sql (schema.sql) …
INFO | registry.init_registry | schema.sql executed successfully.
INFO | registry.init_registry | Executing seed.sql (seed.sql) …
INFO | registry.init_registry | seed.sql executed successfully.
INFO | registry.init_registry | === Registry initialization complete. ===
```

Logs are also written to `logs/init_registry.log`.

---

## 5. Validate the Registry

```powershell
python registry/validate_registry.py
```

A successful run prints `MODULE 1: PASS`. Example output:

```
── PH-01 Building Seed ───────────────────────────────────
  [PASS] buildings.building_code = 'PH-01'
  [PASS] buildings.building_name = 'Main Powerhouse Block'
  [PASS] buildings.occupancy_type = 'utility'
  [PASS] buildings.floor_area_sqm = 2400.0
  [PASS] buildings.floor_count = 2

── PH-01-MAIN Meter Seed ─────────────────────────────────
  [PASS] meters.meter_code = 'PH-01-MAIN'
  [PASS] meters.meter_type = 'main'
  [PASS] meters.protocol = 'manual_export'
  [PASS] meters.sampling_interval_seconds = 900
  [PASS] meters.is_active = True

MODULE 1: PASS
```

---

## 6. Manual Validation Queries

```sql
-- Building
SELECT building_code, building_name, occupancy_type, floor_area_sqm, floor_count
FROM buildings;

-- Meters
SELECT meter_code, meter_type, protocol, sampling_interval_seconds, is_active
FROM meters;

-- Twin instance summary
SELECT * FROM twin_instances;

-- Relationships
SELECT b.building_code, m.meter_code, m.meter_type
FROM buildings b
JOIN meters m ON m.building_id = b.building_id;
```

---

## 7. PH-01 Asset Information

| Field | Value | Source |
|---|---|---|
| `building_code` | `PH-01` | Mentor seed |
| `building_name` | `Main Powerhouse Block` | Mentor seed |
| `occupancy_type` | `utility` | Mentor seed |
| `floor_area_sqm` | `2400.0` | Mentor seed |
| `floor_count` | `2` | Mentor seed |
| `typical_occupancy` | NULL | Not provided |
| `year_commissioned` | NULL | Not provided |

Main meter:

| Field | Value | Source |
|---|---|---|
| `meter_code` | `PH-01-MAIN` | Mentor seed |
| `meter_type` | `main` | Mentor seed |
| `protocol` | `manual_export` | Mentor seed |
| `sampling_interval_seconds` | `900` | Mentor seed |
| `rated_capacity_kw` | NULL | Not confirmed |
| `install_date` | NULL | Not confirmed |

---

## 8. Raw Dataset Mapping

The real KCT Powerhouse dataset contains 12 monitored endpoints. Their mapping to ENTWINE identifiers is documented in:

```
registry/asset_mapping.csv
```

Each entry has a `confidence` field:
- `confirmed` — directly from mentor documents
- `strong_inference` — supported by dataset structure and naming
- `pending_mentor_confirmation` — plausible but not yet verified

Do not treat `strong_inference` or `pending_mentor_confirmation` entries as facts for database seeding.

---

## 9. Known Uncertainties (pending mentor confirmation)

| Question | Status |
|---|---|
| Is `POWERHOUSE_1.POWERHOUSE_1_INCOMER` the physical source for `PH-01-MAIN`? | pending |
| Are the 8 block endpoints (A–E, B-UPS, LIGHTING, DG_1) the mentor's "8 meters"? | pending |
| Should `DG_1` be an `equipment` record, a `meter`, or both? | pending |
| How should `FROM_POWERHOUSE_2` and `TO_POWERHOUSE_2` be represented? | pending |
| What is the rated capacity of any meter or equipment? | pending |

---

## 10. Resetting the Database

To start completely fresh:

```powershell
docker compose down -v           # removes the database volume
docker compose up -d             # recreates fresh container
python registry/init_registry.py # applies schema + seed
python registry/validate_registry.py
```
