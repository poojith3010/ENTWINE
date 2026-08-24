-- ============================================================
-- ENTWINE — Asset Registry Seed Data (Module 1)
-- Run AFTER schema.sql on a clean or idempotent database.
-- Contains only values sourced from the mentor documents or
-- confirmed from the real dataset.  No fabricated values.
-- ============================================================

-- ============================================================
-- PH-01 — Main Powerhouse Block
-- Source: mentor seed in asset_registry_schema.sql
-- ============================================================
INSERT INTO buildings (
    building_code,
    building_name,
    occupancy_type,
    floor_area_sqm,
    floor_count
    -- typical_occupancy  : unknown — left NULL
    -- year_commissioned  : unknown — left NULL
    -- notes              : left NULL until additional info confirmed
)
VALUES (
    'PH-01',
    'Main Powerhouse Block',
    'utility',
    2400.0,
    2
)
ON CONFLICT (building_code) DO UPDATE
    SET
        building_name  = EXCLUDED.building_name,
        occupancy_type = EXCLUDED.occupancy_type,
        floor_area_sqm = EXCLUDED.floor_area_sqm,
        floor_count    = EXCLUDED.floor_count,
        updated_at     = now();

-- ============================================================
-- PH-01-MAIN — Main metering point for PH-01
-- Source: mentor seed in asset_registry_schema.sql
-- Note: physical mapping to POWERHOUSE_1.POWERHOUSE_1_INCOMER
--       is a strong candidate but pending mentor confirmation.
--       See registry/asset_mapping.csv.
-- ============================================================
INSERT INTO meters (
    meter_code,
    building_id,
    meter_type,
    protocol,
    sampling_interval_seconds,
    is_active
    -- parent_meter_id    : NULL — this is the main (top-level) meter
    -- rated_capacity_kw  : unknown — left NULL
    -- install_date       : unknown — left NULL
)
SELECT
    'PH-01-MAIN',
    b.building_id,
    'main',
    'manual_export',
    900,
    TRUE
FROM buildings b
WHERE b.building_code = 'PH-01'
ON CONFLICT (meter_code) DO UPDATE
    SET
        building_id               = EXCLUDED.building_id,
        meter_type                = EXCLUDED.meter_type,
        protocol                  = EXCLUDED.protocol,
        sampling_interval_seconds = EXCLUDED.sampling_interval_seconds,
        is_active                 = EXCLUDED.is_active,
        updated_at                = now();

-- ============================================================
-- equipment — intentionally empty for now.
-- No equipment rated_power_kw or typical_duty_cycle values are
-- available from the mentor documents or the dataset at this
-- stage.  Records will be added once values are confirmed.
-- Known candidates (see asset_mapping.csv):
--   POWERHOUSE_1.DG_1     → generator/equipment (pending)
--   POWERHOUSE_1.MAIN_VCB → switchgear (pending)
-- ============================================================

-- ============================================================
-- baseline_parameters — intentionally empty for now.
-- No baseline kWh values have been derived from historical data
-- or supplied by the mentor.  The table will be populated in
-- the State Layer phase once real data is available.
-- ============================================================
