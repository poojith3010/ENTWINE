-- ENTWINE Digital Twin Asset Registry Schema
-- Idempotent schema for Module 1 (Asset Registry)

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS buildings (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    campus_code VARCHAR(64),
    location_description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS meters (
    id BIGSERIAL PRIMARY KEY,
    building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
    meter_code VARCHAR(64) NOT NULL UNIQUE,
    meter_type VARCHAR(64) NOT NULL DEFAULT 'electricity',
    unit VARCHAR(32) NOT NULL DEFAULT 'kWh',
    installation_date DATE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS equipment (
    id BIGSERIAL PRIMARY KEY,
    building_id BIGINT NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
    meter_id BIGINT REFERENCES meters(id) ON DELETE SET NULL,
    equipment_code VARCHAR(64) NOT NULL UNIQUE,
    equipment_name VARCHAR(255) NOT NULL,
    equipment_type VARCHAR(128) NOT NULL,
    rated_capacity_kw NUMERIC(12, 3),
    commissioning_date DATE,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS baseline_parameters (
    id BIGSERIAL PRIMARY KEY,
    meter_id BIGINT NOT NULL REFERENCES meters(id) ON DELETE CASCADE,
    parameter_key VARCHAR(128) NOT NULL,
    parameter_value NUMERIC(14, 6) NOT NULL,
    parameter_unit VARCHAR(32),
    effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to TIMESTAMPTZ,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT baseline_parameters_unique UNIQUE (meter_id, parameter_key, effective_from)
);

CREATE OR REPLACE VIEW twin_instances AS
SELECT
    b.id AS building_id,
    b.name AS building_name,
    m.id AS meter_id,
    m.meter_code,
    m.meter_type,
    e.id AS equipment_id,
    e.equipment_code,
    e.equipment_name,
    e.equipment_type
FROM buildings AS b
LEFT JOIN meters AS m ON m.building_id = b.id
LEFT JOIN equipment AS e ON e.building_id = b.id;

INSERT INTO buildings (name, campus_code, location_description)
VALUES ('KCT Powerhouse Block', 'KCT-PH', 'Primary powerhouse block in KCT campus')
ON CONFLICT (name) DO UPDATE
SET
    campus_code = EXCLUDED.campus_code,
    location_description = EXCLUDED.location_description,
    updated_at = NOW();

INSERT INTO meters (building_id, meter_code, meter_type, unit, installation_date, is_active)
SELECT
    b.id,
    'PH-01',
    'electricity',
    'kWh',
    CURRENT_DATE,
    TRUE
FROM buildings AS b
WHERE b.name = 'KCT Powerhouse Block'
ON CONFLICT (meter_code) DO UPDATE
SET
    building_id = EXCLUDED.building_id,
    meter_type = EXCLUDED.meter_type,
    unit = EXCLUDED.unit,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO equipment (
    building_id,
    meter_id,
    equipment_code,
    equipment_name,
    equipment_type,
    rated_capacity_kw,
    status
)
SELECT
    b.id,
    m.id,
    'PH-MAIN-INCOMER',
    'Main Incomer Panel',
    'switchgear',
    1500.000,
    'active'
FROM buildings AS b
JOIN meters AS m ON m.building_id = b.id
WHERE b.name = 'KCT Powerhouse Block' AND m.meter_code = 'PH-01'
ON CONFLICT (equipment_code) DO UPDATE
SET
    building_id = EXCLUDED.building_id,
    meter_id = EXCLUDED.meter_id,
    equipment_name = EXCLUDED.equipment_name,
    equipment_type = EXCLUDED.equipment_type,
    rated_capacity_kw = EXCLUDED.rated_capacity_kw,
    status = EXCLUDED.status,
    updated_at = NOW();

INSERT INTO baseline_parameters (
    meter_id,
    parameter_key,
    parameter_value,
    parameter_unit,
        effective_from,
    notes
)
SELECT
    m.id,
    'baseline_daily_kwh',
    12500.000000,
    'kWh/day',
        TIMESTAMPTZ '2026-01-01 00:00:00+00',
    'Initial operational baseline for Phase 0 twin bootstrapping'
FROM meters AS m
WHERE m.meter_code = 'PH-01'
    AND NOT EXISTS (
            SELECT 1
            FROM baseline_parameters AS bp
            WHERE bp.meter_id = m.id
                AND bp.parameter_key = 'baseline_daily_kwh'
                AND bp.effective_from = TIMESTAMPTZ '2026-01-01 00:00:00+00'
    );
