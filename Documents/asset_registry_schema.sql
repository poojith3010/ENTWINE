-- ============================================================
-- KCT Campus Energy Digital Twin — Asset Registry Schema
-- Layer 1 of the twin: static, structural data per building/meter.
-- Populate this before any state-layer ingestion begins.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Buildings — one row per physical building on campus
-- ------------------------------------------------------------
CREATE TABLE buildings (
    building_id      SERIAL PRIMARY KEY,
    building_code     VARCHAR(20) UNIQUE NOT NULL,   -- e.g. 'PH-01', short internal code
    building_name     VARCHAR(120) NOT NULL,          -- e.g. 'Main Powerhouse Block'
    floor_area_sqm    NUMERIC(10,2),                  -- used to normalize consumption per sqm
    floor_count       SMALLINT,
    occupancy_type    VARCHAR(50),                    -- e.g. 'academic', 'hostel', 'lab', 'admin'
    typical_occupancy INTEGER,                        -- headcount, used as a context feature
    year_commissioned SMALLINT,
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 2. Meters — physical metering points; a building can have
--    more than one (main feed + sub-panels), and a meter can
--    optionally report into a parent meter for hierarchy.
-- ------------------------------------------------------------
CREATE TABLE meters (
    meter_id          SERIAL PRIMARY KEY,
    meter_code        VARCHAR(30) UNIQUE NOT NULL,    -- e.g. 'PH-01-MAIN', 'PH-01-HVAC-1'
    building_id       INTEGER NOT NULL REFERENCES buildings(building_id),
    parent_meter_id   INTEGER REFERENCES meters(meter_id),  -- NULL if this is the main feed
    meter_type        VARCHAR(30) NOT NULL,            -- 'main', 'sub_panel', 'equipment'
    rated_capacity_kw NUMERIC(10,2),
    protocol          VARCHAR(30),                     -- 'modbus', 'mqtt', 'opcua', 'manual_export' — how it's read
    sampling_interval_seconds INTEGER,                 -- expected reporting cadence
    install_date      DATE,
    is_active         BOOLEAN NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_meters_building ON meters(building_id);
CREATE INDEX idx_meters_parent   ON meters(parent_meter_id);

-- ------------------------------------------------------------
-- 3. Equipment inventory — major loads within a building,
--    used both as twin metadata and as context features for
--    the model layer (e.g. explaining a residual via HVAC load).
-- ------------------------------------------------------------
CREATE TABLE equipment (
    equipment_id      SERIAL PRIMARY KEY,
    building_id       INTEGER NOT NULL REFERENCES buildings(building_id),
    meter_id          INTEGER REFERENCES meters(meter_id),  -- which meter this equipment sits behind, if known
    equipment_type     VARCHAR(50) NOT NULL,            -- 'hvac', 'lighting', 'lab_equipment', 'lift', 'server_rack'
    equipment_name     VARCHAR(120),
    rated_power_kw     NUMERIC(10,2),
    typical_duty_cycle NUMERIC(4,2),                     -- 0.0–1.0, used for expected-load baselining
    install_date       DATE,
    is_active          BOOLEAN NOT NULL DEFAULT true,
    notes              TEXT
);

CREATE INDEX idx_equipment_building ON equipment(building_id);

-- ------------------------------------------------------------
-- 4. Baseline parameters — the reference values your model
--    layer (GridReason's contextual prediction) uses to compute
--    "expected" consumption per building. Time-varying but at a
--    slow cadence (weekly/seasonal), so it lives in the registry,
--    not the high-frequency state layer.
-- ------------------------------------------------------------
CREATE TABLE baseline_parameters (
    baseline_id       SERIAL PRIMARY KEY,
    building_id       INTEGER NOT NULL REFERENCES buildings(building_id),
    parameter_name    VARCHAR(60) NOT NULL,             -- e.g. 'weekday_baseline_kwh', 'weekend_baseline_kwh'
    parameter_value   NUMERIC(12,4) NOT NULL,
    unit              VARCHAR(20) NOT NULL DEFAULT 'kWh',
    valid_from        DATE NOT NULL,
    valid_to          DATE,                             -- NULL = still in effect
    source            VARCHAR(60),                       -- 'historical_average', 'manual_estimate', 'model_derived'
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_baseline_building ON baseline_parameters(building_id, parameter_name, valid_from);

-- ------------------------------------------------------------
-- 5. Convenience view — the "twin instance" summary the model
--    layer and interrogation layer will query most often.
-- ------------------------------------------------------------
CREATE VIEW twin_instances AS
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
JOIN meters m ON m.building_id = b.building_id
WHERE m.meter_type = 'main';

-- ------------------------------------------------------------
-- 6. Seed example — KCT Powerhouse Block, matching the
--    proof-of-concept demo (meter PH-01)
-- ------------------------------------------------------------
INSERT INTO buildings (building_code, building_name, occupancy_type, floor_area_sqm, floor_count)
VALUES ('PH-01', 'Main Powerhouse Block', 'utility', 2400.0, 2);

INSERT INTO meters (meter_code, building_id, meter_type, protocol, sampling_interval_seconds, is_active)
VALUES ('PH-01-MAIN', (SELECT building_id FROM buildings WHERE building_code = 'PH-01'), 'main', 'manual_export', 900, true);
