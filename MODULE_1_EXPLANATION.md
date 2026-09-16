# ENTWINE Energy Digital Twin
## Project Explanation Through the Present Stage

**Project:** ENTWINE Energy Digital Twin  
**Current stage:** Phase 0 complete, Module 1 complete, Module 2 implemented and integrated  
**Next stage:** Module 3 validation, forecasting, interrogation, and dashboard layers

---

## 1. Project idea in simple terms

ENTWINE is being developed as a digital twin for the KCT campus energy system.
A digital twin is a software representation of a real physical system. In this
project, the real system includes buildings, electricity meters, equipment, and
energy measurements.

The project will eventually connect three kinds of information:

1. **Static information:** What assets exist and how they are connected.
2. **Operational information:** What those assets measured over time.
3. **Intelligence:** Forecasting, anomaly detection, explanations, and audits.

We started with the static information because the system must know what an
energy reading belongs to before it can analyse that reading correctly.

---

## 2. What has been completed so far

### Phase 0 - Environment and infrastructure setup

Phase 0 created the development foundation required for the rest of the project.

Completed work:

- Created a Python virtual environment in `.venv/`.
- Added strictly pinned Python dependencies in `requirements.txt`.
- Added PostgreSQL/TimescaleDB configuration in `docker-compose.yml`.
- Created the project directories for registry, ingestion, models, forecasting,
  interrogation, dashboard, and logs.
- Added `.gitignore` so local credentials, the virtual environment, and log files
  are not committed.
- Added a root `.env` file for local database configuration.
- Added setup instructions in `README.md`.
- Pushed the project to the ENTWINE GitHub repository.

The database runs locally in Docker. PostgreSQL stores the data, while
TimescaleDB is available for the time-series measurements that are now being
handled in Module 2.

### Module 1 - Asset Registry

Module 1 creates the structural registry of the energy system. It answers:

> What physical assets exist, and how are they related?

The registry is the foundation for the later state layer and model layer.

Completed work:

- Defined the relational database schema.
- Created tables for buildings, meters, equipment, and baseline parameters.
- Created the `twin_instances` view for a convenient building and main-meter
  summary.
- Added the PH-01 mentor-approved seed data.
- Added indexes and foreign-key relationships.
- Added an idempotent initialization process, so the database can be initialized
  repeatedly without creating duplicate seed records.
- Added a validation script that checks the connection, tables, columns, indexes,
  view, and seed values.
- Added registry documentation and an asset mapping file.
- Recorded known uncertainties instead of inventing unconfirmed asset metadata.

### Module 2 - Historical State Layer Ingestion

Module 2 extends the project from static asset mapping into operational data
ingestion. This phase reads the raw historical energy and alarm data and maps it
into verified database tables for later analysis.

Completed work includes:

- historical source discovery and SHA-256 checksum auditing;
- ingestion readers for Excel and CSV source formats;
- time normalization to standardize timestamps and handle timezone issues;
- quality validation and audit logic for invalid or incomplete records;
- asset registration based on `registry/asset_mapping.csv`;
- state-layer migration with separate Module 2 tables;
- reconciliation and validation reports in `logs/`;
- CLI-based execution flow in `ingestion/run.py`.

This phase separates raw source files from the trusted registry layer to keep the
project auditable and reproducible.

---

## 3. Why a database is being used

The source files supplied for the project are Excel reports and alarm/event CSV
files. Those files are useful as source data, but they are not the final digital
twin database.

A database is needed because it can:

- keep relationships between buildings, meters, and equipment;
- enforce data integrity using primary keys and foreign keys;
- prevent duplicate asset records;
- support reliable queries from Python and future applications;
- store historical time-series measurements efficiently;
- provide a consistent source of truth for forecasting and anomaly detection.

The Excel and CSV files remain the raw source material. Module 2 will read those
files, clean the data, and load the historical measurements into the state layer.

---

## 4. Current Module 1 database design

The current schema is defined in `registry/schema.sql`. Seed values are kept
separately in `registry/seed.sql` so that structure and data are easy to review
and maintain.

### `buildings`

This table represents physical buildings or facilities on the campus.

Important fields include:

- `building_id`: unique database identifier;
- `building_code`: stable project identifier, such as `PH-01`;
- `building_name`: human-readable name;
- `floor_area_sqm`: area used later for normalized energy analysis;
- `floor_count`: number of floors;
- `occupancy_type`: building category, such as `utility`;
- `typical_occupancy` and `year_commissioned`: optional context fields;
- timestamps and notes for traceability.

### `meters`

This table represents physical electricity metering points.

Important fields include:

- `meter_id`: unique database identifier;
- `meter_code`: stable identifier for the meter;
- `building_id`: foreign key linking the meter to its building;
- `parent_meter_id`: supports a main-meter and sub-meter hierarchy;
- `meter_type`: for example, `main` or `sub_panel`;
- `protocol`: how the data is obtained, such as `manual_export`;
- `sampling_interval_seconds`: expected measurement interval;
- `is_active`: indicates whether the meter is currently active.

A building can have more than one meter. The parent relationship is important
because a campus energy system may contain a main incomer and multiple downstream
sub-panels.

### `equipment`

This table represents major loads or generation assets associated with a
building, such as HVAC systems, generators, lighting systems, or switchgear.

The table can link equipment to both a building and a meter. Fields such as
rated power and duty cycle are optional until they are confirmed from reliable
sources.

At the current stage, no equipment values are seeded because the available
mentor information did not confirm enough equipment metadata. This is deliberate:
we avoid presenting assumptions as verified facts.

### `baseline_parameters`

This table stores reference values used later by model calculations. Examples
could include a weekday baseline or a seasonal expected value.

The values are time-bounded using `valid_from` and `valid_to`, allowing a baseline
to change while preserving historical traceability.

At the current stage, this table is intentionally empty because a proper baseline
must be derived from validated historical measurements in Module 2 rather than
being fabricated during registry setup.

### `twin_instances`

This view joins buildings and their main meters. It gives later model and
interrogation layers a simple way to retrieve the primary twin instance without
repeating join logic throughout the codebase.

A view does not duplicate data. It presents related table data in a convenient,
read-only query shape.

---

## 5. Verified seed data

The current trusted seed represents the first registry instance:

| Entity | Field | Value |
|---|---|---|
| Building | `building_code` | `PH-01` |
| Building | `building_name` | `Main Powerhouse Block` |
| Building | `occupancy_type` | `utility` |
| Building | `floor_area_sqm` | `2400.0` |
| Building | `floor_count` | `2` |
| Meter | `meter_code` | `PH-01-MAIN` |
| Meter | `meter_type` | `main` |
| Meter | `protocol` | `manual_export` |
| Meter | `sampling_interval_seconds` | `900` seconds, or 15 minutes |
| Meter | `is_active` | `true` |

The raw dataset contains multiple endpoints such as A Block, B Block, B Block
UPS, C Block, D Block, DG 1, lighting, and power-house connections. Those names
have been recorded in `registry/asset_mapping.csv`, but they have not all been
seeded as confirmed registry assets yet. Their exact meaning and relationship to
mentor-defined meters still require confirmation.

This distinction is important: Module 1 establishes trusted asset metadata; it
does not guess the identity of every raw-data endpoint.

---

## 6. How the implementation works

### `registry/schema.sql`

Creates the database structure:

- TimescaleDB extension availability;
- tables;
- primary keys;
- foreign keys;
- indexes;
- `twin_instances` view.

It uses `IF NOT EXISTS` and `CREATE OR REPLACE VIEW`, which makes the schema
safe to apply repeatedly.

### `registry/seed.sql`

Inserts the confirmed PH-01 building and main meter. It uses conflict handling,
so running it again updates the known record instead of creating a duplicate.
Unconfirmed equipment and baseline values are intentionally not inserted.

### `registry/init_registry.py`

This is the database initialization entry point. It:

1. loads credentials from the root `.env` file;
2. validates the required configuration;
3. creates a SQLAlchemy PostgreSQL engine with connection pooling;
4. executes `schema.sql` and then `seed.sql` in transactions;
5. logs progress and errors to the console and `logs/init_registry.log`;
6. disposes of the database engine when complete.

### `registry/validate_registry.py`

This verifies that Module 1 is really present in the running database. It checks
that the database is reachable, required objects exist, critical columns and
indexes are present, the view is correct, and the PH-01 seed values match the
expected values.

---

## 7. How to run and verify Module 1

From `C:\Final_Year_Project`, with Docker running and the virtual environment
activated:

```powershell
 docker compose up -d
 python registry/init_registry.py
 $env:PYTHONUTF8=1
 python registry/validate_registry.py
```

The final validation result should be:

```text
MODULE 1: PASS
```

The initialization can be run again safely. `docker compose down` stops the
container but keeps the database volume. `docker compose down -v` removes the
volume and permanently resets the local database data.

---

## 8. Mentor explanation in presentation form

The following is a concise explanation that can be given to a mentor:

> We completed the environment foundation, Module 1, and the initial Module 2
> ingestion framework for the ENTWINE Energy Digital Twin. We created a local
> PostgreSQL/TimescaleDB database in Docker and connected it to Python using
> SQLAlchemy. The project now has a static Asset Registry that defines the
> physical structure of the campus energy system, including buildings, meters,
> equipment, and baseline metadata.
>
> In Module 1, we implemented the registry schema, the PH-01 seed data, and a
> validation framework to confirm the database structure is correct and the asset
> records are consistent. The registry acts as the trusted source of asset
> identity for the rest of the system. We intentionally did not insert every raw
> block endpoint as a confirmed asset without validation; instead, we kept a
> clear mapping file that tracks what is confirmed, strongly inferred, and still
> pending mentor review.
>
> In Module 2, we moved beyond static definitions and implemented a historical
> ingestion pipeline that discovers raw CSV and XLS files, normalizes timestamps,
> validates quality, maps records to the registry, and writes historical
> operational data into the state layer. This gives the system a reproducible way
> to bring raw energy data into the digital twin without discarding provenance or
> silently accepting bad input.
>
> The overall architecture is now clear: Module 1 defines the assets, Module 2
> ingests the historical operational data, and the future modules will build the
> forecasting, explanation, and interrogation layers on top of this verified base.

---

## 9. What is not complete yet

These items belong to later work and should not be described as completed Module
1 functionality:

- importing historical Excel and CSV readings;
- creating the time-series state-layer table or hypertable;
- mapping every raw endpoint to a confirmed meter;
- loading alarm and event history;
- GridReason, GrCF, or CAFA model execution;
- forecasting;
- RAG/interrogation functionality;
- dashboard development;
- continuous scheduling and automation.

Keeping these boundaries explicit makes the project easier to validate
scientifically and prevents unverified assumptions from entering the digital twin.

---

## 10. Module progression record

| Stage | Status | Main result |
|---|---|---|
| Phase 0 | Complete | Environment, Docker database, dependencies, project structure |
| Module 1 | Complete | Trusted static Asset Registry and validation framework |
| Module 2 | Complete | Historical ingestion, reconciliation, mapping, and state-layer migration |
| Module 3 | Pending | Baseline model validation and explainability |
| Future modules | Pending | Forecasting, interrogation, dashboard, automation |

This document should be updated after each module so it remains the project’s
plain-language progress record for mentor discussions.
