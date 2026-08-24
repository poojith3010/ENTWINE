-- ============================================================
-- ENTWINE — Asset Registry Schema (Module 1)
-- Source of truth: mentor's asset_registry_schema.sql
-- Layer 1 of the twin: static, structural data per building/meter.
-- Populate this before any state-layer ingestion begins.
-- ============================================================

-- Required extension for later TimescaleDB state-layer work.
-- Loading it here so the same PostgreSQL instance is ready.
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ============================================================
-- 1. buildings — one row per physical building on campus
-- ============================================================
CREATE TABLE IF NOT EXISTS buildings (
    building_id       SERIAL PRIMARY KEY,
    building_code     VARCHAR(20)  UNIQUE NOT NULL,   -- e.g. 'PH-01'
    building_name     VARCHAR(120) NOT NULL,           -- e.g. 'Main Powerhouse Block'
    floor_area_sqm    NUMERIC(10,2),                   -- normalise consumption per sqm
    floor_count       SMALLINT,
    occupancy_type    VARCHAR(50),                     -- 'academic', 'hostel', 'utility', …
    typical_occupancy INTEGER,                         -- headcount; context feature for models
    year_commissioned SMALLINT,
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 2. meters — physical metering points
--    A building can have more than one (main feed + sub-panels).
--    parent_meter_id enables a hierarchical meter tree.
-- ============================================================
CREATE TABLE IF NOT EXISTS meters (
    meter_id                  SERIAL  PRIMARY KEY,
    meter_code                VARCHAR(30) UNIQUE NOT NULL,   -- e.g. 'PH-01-MAIN'
    building_id               INTEGER NOT NULL
                                  REFERENCES buildings(building_id),
    parent_meter_id           INTEGER
                                  REFERENCES meters(meter_id),  -- NULL → main feed
    meter_type                VARCHAR(30) NOT NULL,             -- 'main', 'sub_panel', 'equipment'
    rated_capacity_kw         NUMERIC(10,2),
    protocol                  VARCHAR(30),                      -- 'modbus', 'manual_export', …
    sampling_interval_seconds INTEGER,                          -- expected cadence in seconds
    install_date              DATE,
    is_active                 BOOLEAN NOT NULL DEFAULT true,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_meters_building
    ON meters(building_id);

CREATE INDEX IF NOT EXISTS idx_meters_parent
    ON meters(parent_meter_id);

-- ============================================================
-- 3. equipment — major loads / generation assets within a
--    building; used as twin metadata and as model-layer context.
-- ============================================================
CREATE TABLE IF NOT EXISTS equipment (
    equipment_id       SERIAL PRIMARY KEY,
    building_id        INTEGER NOT NULL
                           REFERENCES buildings(building_id),
    meter_id           INTEGER
                           REFERENCES meters(meter_id),   -- NULL if not known
    equipment_type     VARCHAR(50)  NOT NULL,              -- 'hvac', 'lighting', 'generator', …
    equipment_name     VARCHAR(120),
    rated_power_kw     NUMERIC(10,2),                      -- NULL until confirmed from source
    typical_duty_cycle NUMERIC(4,2),                       -- 0.0–1.0; NULL until confirmed
    install_date       DATE,
    is_active          BOOLEAN NOT NULL DEFAULT true,
    notes              TEXT
);

CREATE INDEX IF NOT EXISTS idx_equipment_building
    ON equipment(building_id);

-- ============================================================
-- 4. baseline_parameters — slowly-changing reference values
--    used by the model layer to compute expected consumption.
--    Lives in the registry (not the state layer) because its
--    cadence is weekly/seasonal, not per-15-min.
-- ============================================================
CREATE TABLE IF NOT EXISTS baseline_parameters (
    baseline_id     SERIAL PRIMARY KEY,
    building_id     INTEGER NOT NULL
                        REFERENCES buildings(building_id),
    parameter_name  VARCHAR(60)   NOT NULL,   -- e.g. 'weekday_baseline_kwh'
    parameter_value NUMERIC(12,4) NOT NULL,
    unit            VARCHAR(20)   NOT NULL DEFAULT 'kWh',
    valid_from      DATE          NOT NULL,
    valid_to        DATE,                      -- NULL = still in effect
    source          VARCHAR(60),               -- 'historical_average', 'manual_estimate', …
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_baseline_building
    ON baseline_parameters(building_id, parameter_name, valid_from);

-- ============================================================
-- 5. twin_instances — convenience view used by the model layer
--    and interrogation layer.
--    Returns one row per (building, main-meter) pair only.
-- ============================================================
CREATE OR REPLACE VIEW twin_instances AS
SELECT
    b.building_id,
    b.building_code,
    b.building_name,
    b.occupancy_type,
    b.floor_area_sqm,
    m.meter_id,
    m.meter_code,
    m.meter_type,
    m.protocol,
    m.is_active AS meter_active
FROM buildings b
JOIN meters m
    ON m.building_id = b.building_id
WHERE m.meter_type = 'main';
