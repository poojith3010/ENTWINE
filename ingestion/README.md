# ENTWINE — Module 2: Historical State Layer Ingestion

This package provides an auditable, idempotent ingestion pipeline for historical CSV and XLS files in `real time energy data/` into TimescaleDB and PostgreSQL.

---

## 1. Architectural Highlights

- **Idempotent Ingestion**: Every file is tracked by SHA-256 checksum in `source_files`. Re-running the pipeline (`--rerun`) automatically skips already-ingested files.
- **Strict Module 1 Separation**: Ingestion writes solely to Module 2 tables (`interval_telemetry`, `daily_energy_reports`, `operational_events`, `rejected_records`, `source_files`, `ingestion_run_files`).
- **Mapping-Driven Assets**: Static meter and equipment definitions are managed via `ingestion/register_assets.py` reading `registry/asset_mapping.csv`. All mappings are snapshotted in `mapping_snapshots` with approval metadata.
- **Dynamic Measurement Routing**: `measurement_sources` acts as the indirection layer linking raw data sources (including switchgear and generators) to Module 1 assets, keeping telemetry hypertable definitions clean and resilient.
- **Feeder Quarantine**: Unresolved or TBD feeders (e.g. `FROM_POWERHOUSE_2`, `TO_POWERHOUSE_2`) are systematically parsed and stored in `rejected_records` with error category `unresolved_feeder`.
- **Zero-Data Handling**: Empty files (such as `POWERHOUSE_1_INCOMER`) are cataloged with status `no_data` rather than failing or silently dropping.

---

## 2. CLI Usage

Run commands using the project virtual environment:

```powershell
# 1. Apply Module 2 database migrations
python -m ingestion.run --migrate

# 2. Register approved assets from registry/asset_mapping.csv
python -m ingestion.run --register-assets

# 3. Dry-run simulation (verifies parsing without state writes)
python -m ingestion.run --dry-run

# 4. Full historical ingestion
python -m ingestion.run --ingest

# 5. Idempotent rerun (processes only new/un-ingested files)
python -m ingestion.run --rerun

# 6. Validate Module 2 quality gates
python -m ingestion.run --validate

# 7. Generate reconciliation report
python -m ingestion.run --reconcile
```

---

## 3. Package Structure

```
ingestion/
├── config.py             # Database DSN and path configuration
├── discover.py           # Source file discovery and SHA-256 calculation
├── mapping.py            # asset_mapping.csv parser and validator
├── migrate.py            # Migration runner with checksum tamper protection
├── normalize.py          # Timestamp (IST -> UTC) and metric conversion
├── quality.py            # 12 automated quality gate checks
├── readers/              # Source-specific parsers
│   ├── alarm_csv.py      # Alarms, events, and incidents CSV parser
│   ├── daily_xls.py      # Daily multi-sheet energy report XLS parser
│   ├── measurement_csv.py# Long/narrow format telemetry CSV parser
│   └── tabular_xls.py    # 15-minute interval telemetry XLS parser
├── reconcile.py          # Markdown reconciliation report generator
├── register_assets.py    # Controlled asset registrar for Module 1
├── run.py                # Unified CLI entry point
└── writer.py             # Batch database writer with transaction management
```

---

## 4. Tables in Module 2

- `schema_migrations`: Migration audit log with checksum verification.
- `mapping_snapshots`: Historical snapshot of asset mapping with approval provenance.
- `measurement_sources`: Mapping of raw sources to `meters` or `equipment`.
- `ingestion_runs`: Pipeline execution runs and statistics.
- `source_files`: Physical files cataloged with SHA-256 and status.
- `ingestion_run_files`: Many-to-many run-to-file processing log.
- `rejected_records`: Audit table for unmapped, invalid, or quarantined rows.
- `interval_telemetry`: TimescaleDB hypertable for 15-minute sensor readings.
- `daily_energy_reports`: Daily aggregate energy consumption records.
- `operational_events`: Unified alarms, events, and incidents table.
