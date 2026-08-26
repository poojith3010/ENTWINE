-- =============================================================================
-- ENTWINE — Module 2 State Layer Schema
-- Migration: 001_module2_state_layer.sql
-- Applied by: ingestion/migrate.py
--
-- Creates the complete Module 2 state-layer tables.
-- Module 1 tables (buildings, meters, equipment, baseline_parameters) are
-- untouched by this migration.  This migration is idempotent: safe to inspect
-- on a live database but designed to run exactly once per environment.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 0. Migration bookkeeping table
--    Created here so the runner can self-track on first execution.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          SERIAL       PRIMARY KEY,
    filename    VARCHAR(200) UNIQUE NOT NULL,
    checksum    VARCHAR(64)  NOT NULL,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 1. mapping_snapshots
--    Immutable record of the asset_mapping.csv state at the time register_assets
--    was run.  Preserves original_confidence verbatim while adding approval
--    provenance fields.  One row per (source_name, snapshot_run_at).
-- ---------------------------------------------------------------------------
CREATE TABLE mapping_snapshots (
    snapshot_id         SERIAL       PRIMARY KEY,
    snapshot_run_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    approval_reference  TEXT,
    approved_at         TIMESTAMPTZ,
    approval_status     VARCHAR(30)  NOT NULL,
        -- 'approved' | 'pending' | 'quarantined'
    source_name         VARCHAR(100) NOT NULL,
    entwine_asset_code  VARCHAR(30),
    asset_type          VARCHAR(40),
    building_code       VARCHAR(20),
    meter_type          VARCHAR(30),
    parent_asset        VARCHAR(30),
    source_of_truth     TEXT,
    original_confidence VARCHAR(40),   -- verbatim from CSV; never altered
    notes               TEXT,
    UNIQUE (source_name, snapshot_run_at)
);

-- ---------------------------------------------------------------------------
-- 2. measurement_sources
--    Maps every raw POWERHOUSE_1.* source name to zero or one registry asset
--    (meter or equipment).  Created by register_assets.py; queried by the
--    ingestion pipeline to link interval_telemetry rows.
--
--    resolution_status:
--      resolved    — meter_id OR equipment_id is populated and FK-valid
--      unresolved  — source present in mapping but asset creation pending
--      quarantined — feeder / TBD; no FK; telemetry rows go to rejected_records
-- ---------------------------------------------------------------------------
CREATE TABLE measurement_sources (
    measurement_source_id  SERIAL       PRIMARY KEY,
    source_name            VARCHAR(100) UNIQUE NOT NULL,
    entwine_asset_code     VARCHAR(30),
    asset_type             VARCHAR(40),
    meter_id               INTEGER      REFERENCES meters(meter_id),
    equipment_id           INTEGER      REFERENCES equipment(equipment_id),
    mapping_confidence     VARCHAR(40),
    approval_status        VARCHAR(30),
    resolution_status      VARCHAR(20)  NOT NULL DEFAULT 'unresolved',
    notes                  TEXT,
    registered_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT chk_one_asset_link CHECK (
        NOT (meter_id IS NOT NULL AND equipment_id IS NOT NULL)
    )
);

CREATE INDEX idx_ms_meter_id
    ON measurement_sources (meter_id)
    WHERE meter_id IS NOT NULL;

CREATE INDEX idx_ms_equipment_id
    ON measurement_sources (equipment_id)
    WHERE equipment_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. ingestion_runs
--    One row per pipeline invocation.  Tracks mode, status, and aggregate
--    counts.  Individual file results live in ingestion_run_files.
-- ---------------------------------------------------------------------------
CREATE TABLE ingestion_runs (
    run_id          SERIAL      PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    mode            VARCHAR(20) NOT NULL,
        -- 'dry_run' | 'ingest' | 'rerun' | 'validate'
    status          VARCHAR(20) NOT NULL DEFAULT 'running',
        -- 'running' | 'done' | 'failed' | 'partial'
    code_version    VARCHAR(60),
    total_files     INTEGER,
    files_processed INTEGER,
    files_skipped   INTEGER,
    files_failed    INTEGER,
    rows_curated    BIGINT,
    rows_rejected   BIGINT,
    rows_skipped    BIGINT,
    error_summary   TEXT
);

-- ---------------------------------------------------------------------------
-- 4. source_files
--    One row per unique physical file, keyed by SHA-256 checksum.
--    Decoupled from ingestion_runs so a file processed in run-1 is
--    recognised and skipped (not duplicated) in run-2.
--
--    processing_status values:
--      pending     — discovered but not yet processed
--      done        — all rows processed (curated + rejected + skipped)
--      failed      — source identity known but parsing/DB write failed
--      skipped     — already ingested in a previous run (checksum match)
--      no_data     — file parsed but contained zero data rows
--      unresolved  — source identity could not be determined at runtime
-- ---------------------------------------------------------------------------
CREATE TABLE source_files (
    source_file_id    SERIAL       PRIMARY KEY,
    file_path         TEXT         NOT NULL,
    file_name         VARCHAR(255) NOT NULL,
    source_category   VARCHAR(30)  NOT NULL,
        -- 'interval_telemetry' | 'daily_report' | 'alarm_history'
        -- 'alarm_status' | 'event' | 'incident' | 'measurement_csv' | 'no_data'
    source_name       VARCHAR(100),
    file_size_bytes   BIGINT,
    sha256_checksum   VARCHAR(64)  NOT NULL,
    discovered_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    processing_status VARCHAR(20)  NOT NULL DEFAULT 'pending',
    unresolved_reason TEXT,
    rows_read         INTEGER,
    rows_curated      INTEGER,
    rows_rejected     INTEGER,
    rows_skipped      INTEGER,
    UNIQUE (sha256_checksum)
);

-- ---------------------------------------------------------------------------
-- 5. ingestion_run_files
--    Many-to-many link between ingestion_runs and source_files.
--    Allows one source_file to appear across many runs while remaining
--    ingested only once.
-- ---------------------------------------------------------------------------
CREATE TABLE ingestion_run_files (
    run_id          INTEGER     NOT NULL REFERENCES ingestion_runs(run_id),
    source_file_id  INTEGER     NOT NULL REFERENCES source_files(source_file_id),
    action          VARCHAR(20) NOT NULL,
        -- 'ingested' | 'skipped_duplicate' | 'failed' | 'unresolved' | 'no_data'
    detail          TEXT,
    PRIMARY KEY (run_id, source_file_id)
);

-- ---------------------------------------------------------------------------
-- 6. rejected_records
--    Every source row that is invalid, unmappable, or a quarantined feeder
--    row lands here with its full raw payload.  Nothing is silently dropped.
-- ---------------------------------------------------------------------------
CREATE TABLE rejected_records (
    rejection_id    SERIAL       PRIMARY KEY,
    source_file_id  INTEGER      NOT NULL REFERENCES source_files(source_file_id),
    row_reference   TEXT,
        -- e.g. "Sheet1:row_102", "CSV:line_44"
    error_category  VARCHAR(50),
        -- 'unmapped_source' | 'invalid_timestamp' | 'missing_value'
        -- 'duplicate' | 'unresolved_feeder' | 'parse_error' | 'no_data_file'
    error_message   TEXT,
    raw_payload     JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_rr_source_file ON rejected_records (source_file_id);
CREATE INDEX idx_rr_category    ON rejected_records (error_category);

-- ---------------------------------------------------------------------------
-- 7. interval_telemetry  (TimescaleDB hypertable)
--    15-minute interval readings from all POWERHOUSE_1 tabular XLS files
--    and the misplaced 1st_floor measurement CSV.
--
--    measurement_source_id links every row to measurement_sources, which
--    in turn links to meters or equipment as appropriate.  This indirection
--    keeps the telemetry table stable as the registry evolves.
-- ---------------------------------------------------------------------------
CREATE TABLE interval_telemetry (
    ts                             TIMESTAMPTZ  NOT NULL,
    measurement_source_id          INTEGER      NOT NULL
                                       REFERENCES measurement_sources(measurement_source_id),
    source_file_id                 INTEGER      NOT NULL
                                       REFERENCES source_files(source_file_id),
    source_name                    VARCHAR(100) NOT NULL,
    entwine_asset_code             VARCHAR(30),
    mapping_confidence             VARCHAR(30),
    -- Canonical metrics (NULL when not present in source)
    real_power_kw                  NUMERIC(12,4),
    real_power_a_kw                NUMERIC(12,4),
    real_power_b_kw                NUMERIC(12,4),
    real_power_c_kw                NUMERIC(12,4),
    reactive_energy_kvarh          NUMERIC(16,4),
    real_energy_kwh                NUMERIC(16,4),
    apparent_energy_kvah           NUMERIC(16,4),
    apparent_energy_into_load_kvah NUMERIC(16,4),
    apparent_power_kva             NUMERIC(12,4),
    current_avg_a                  NUMERIC(12,4),
    current_a_a                    NUMERIC(12,4),
    current_b_a                    NUMERIC(12,4),
    current_c_a                    NUMERIC(12,4),
    voltage_ll_avg_v               NUMERIC(12,4),
    voltage_ln_avg_v               NUMERIC(12,4),
    frequency_hz                   NUMERIC(10,4),
    power_factor_pct               NUMERIC(10,4),
    -- Provenance
    source_ts_raw                  TEXT         NOT NULL,
    raw_payload                    JSONB,
    -- Idempotency: (source_file_id, source_name, ts) is unique per curated row.
    UNIQUE (source_file_id, source_name, ts)
);

-- Convert to TimescaleDB hypertable partitioned by time.
SELECT create_hypertable(
    'interval_telemetry', 'ts',
    if_not_exists       => TRUE,
    chunk_time_interval => INTERVAL '1 month'
);

-- Primary query patterns for model and dashboard layers.
CREATE INDEX idx_it_msource_ts
    ON interval_telemetry (measurement_source_id, ts DESC);

CREATE INDEX idx_it_source_ts
    ON interval_telemetry (source_name, ts DESC);

-- ---------------------------------------------------------------------------
-- 8. daily_energy_reports
--    Daily kWh (and optional kVAh) aggregates from the 7 DAILY ENERGY REPORT
--    workbooks.  Stored at day granularity; separate from interval_telemetry.
-- ---------------------------------------------------------------------------
CREATE TABLE daily_energy_reports (
    report_id                      SERIAL      PRIMARY KEY,
    measurement_source_id          INTEGER     REFERENCES measurement_sources(measurement_source_id),
    source_file_id                 INTEGER     NOT NULL
                                       REFERENCES source_files(source_file_id),
    report_date                    DATE        NOT NULL,
    source_name                    VARCHAR(100) NOT NULL,
    entwine_asset_code             VARCHAR(30),
    mapping_confidence             VARCHAR(30),
    real_energy_kwh                NUMERIC(16,4),
    apparent_energy_into_load_kvah NUMERIC(16,4),
    sheet_name                     VARCHAR(30),
    raw_payload                    JSONB,
    UNIQUE (source_file_id, source_name, report_date)
);

CREATE INDEX idx_der_msource_date
    ON daily_energy_reports (measurement_source_id, report_date DESC);

-- ---------------------------------------------------------------------------
-- 9. operational_events
--    Unified table for alarm history, alarm status snapshots, events, and
--    incidents.  Discriminated by event_class.
--
--    row_fingerprint is always NOT NULL and computed before insert:
--      sha256( source_file_id | event_class | source_event_id
--              | start_time_raw | priority | device_name | alarm_name )
--    This avoids the NULL-equality gap in UNIQUE constraints and ensures
--    two structurally distinct source rows never share the same fingerprint.
-- ---------------------------------------------------------------------------
CREATE TABLE operational_events (
    event_id              SERIAL       PRIMARY KEY,
    source_file_id        INTEGER      NOT NULL
                              REFERENCES source_files(source_file_id),
    event_class           VARCHAR(20)  NOT NULL,
        -- 'alarm_history' | 'alarm_status' | 'event' | 'incident'
    source_event_id       TEXT,
    priority              INTEGER,
    device_name           TEXT,
    measurement_source_id INTEGER      REFERENCES measurement_sources(measurement_source_id),
    entwine_asset_code    VARCHAR(30),
    mapping_confidence    VARCHAR(30),
    alarm_name            TEXT,
    alarm_details         TEXT,
    event_type            TEXT,
    condition_text        TEXT,
    measurement           TEXT,
    measurement_value     TEXT,
    event_sub_type        TEXT,
    active                BOOLEAN,
    unacknowledged        BOOLEAN,
    occurrences           INTEGER,
    start_time_ist        TIMESTAMPTZ,
    end_time_ist          TIMESTAMPTZ,
    start_time_raw        TEXT,
    end_time_raw          TEXT,
    raw_payload           JSONB,
    row_fingerprint       VARCHAR(64)  NOT NULL,
    UNIQUE (row_fingerprint)
);

CREATE INDEX idx_oe_source_file  ON operational_events (source_file_id);
CREATE INDEX idx_oe_device_class ON operational_events (device_name, event_class);
CREATE INDEX idx_oe_msource      ON operational_events (measurement_source_id)
    WHERE measurement_source_id IS NOT NULL;
CREATE INDEX idx_oe_start_time   ON operational_events (start_time_ist DESC)
    WHERE start_time_ist IS NOT NULL;
