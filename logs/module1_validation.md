# Module 1 — Asset Registry Validation Log

## Environment

| Item | Value |
|---|---|
| Date | 2026-08-24 |
| Database | TimescaleDB (PostgreSQL 16) via Docker |
| Container image | `timescale/timescaledb:latest-pg16` |
| DB name | `entwine_twin` |
| Schema file | `registry/schema.sql` |
| Seed file | `registry/seed.sql` |
| Init script | `registry/init_registry.py` |
| Validation script | `registry/validate_registry.py` |

---

## Procedure

```
docker compose down -v      # reset — remove old volume
docker compose up -d        # fresh container
python registry/init_registry.py
python registry/validate_registry.py
```

---

## Expected Validation Results

```
── Connectivity ──────────────────────────────────────────
  [PASS] Database connection

── Tables ────────────────────────────────────────────────
  [PASS] Table 'baseline_parameters' exists
  [PASS] Table 'buildings' exists
  [PASS] Table 'equipment' exists
  [PASS] Table 'meters' exists

── Columns ───────────────────────────────────────────────
  [PASS] Column 'buildings.building_id' exists
  [PASS] Column 'buildings.building_code' exists
  [PASS] Column 'buildings.building_name' exists
  [PASS] Column 'buildings.floor_area_sqm' exists
  [PASS] Column 'buildings.floor_count' exists
  [PASS] Column 'buildings.occupancy_type' exists
  [PASS] Column 'buildings.typical_occupancy' exists
  [PASS] Column 'buildings.year_commissioned' exists
  [PASS] Column 'buildings.notes' exists
  [PASS] Column 'meters.meter_id' exists
  [PASS] Column 'meters.meter_code' exists
  [PASS] Column 'meters.building_id' exists
  [PASS] Column 'meters.parent_meter_id' exists
  [PASS] Column 'meters.meter_type' exists
  [PASS] Column 'meters.rated_capacity_kw' exists
  [PASS] Column 'meters.protocol' exists
  [PASS] Column 'meters.sampling_interval_seconds' exists
  [PASS] Column 'meters.install_date' exists
  [PASS] Column 'meters.is_active' exists
  [PASS] Column 'equipment.equipment_id' exists
  [PASS] Column 'equipment.building_id' exists
  [PASS] Column 'equipment.meter_id' exists
  [PASS] Column 'equipment.equipment_type' exists
  [PASS] Column 'equipment.equipment_name' exists
  [PASS] Column 'equipment.rated_power_kw' exists
  [PASS] Column 'equipment.typical_duty_cycle' exists
  [PASS] Column 'equipment.install_date' exists
  [PASS] Column 'equipment.is_active' exists
  [PASS] Column 'equipment.notes' exists
  [PASS] Column 'baseline_parameters.baseline_id' exists
  [PASS] Column 'baseline_parameters.building_id' exists
  [PASS] Column 'baseline_parameters.parameter_name' exists
  [PASS] Column 'baseline_parameters.parameter_value' exists
  [PASS] Column 'baseline_parameters.unit' exists
  [PASS] Column 'baseline_parameters.valid_from' exists
  [PASS] Column 'baseline_parameters.valid_to' exists
  [PASS] Column 'baseline_parameters.source' exists

── Indexes ───────────────────────────────────────────────
  [PASS] Index 'idx_meters_building' on 'meters' exists
  [PASS] Index 'idx_meters_parent' on 'meters' exists
  [PASS] Index 'idx_equipment_building' on 'equipment' exists
  [PASS] Index 'idx_baseline_building' on 'baseline_parameters' exists

── Views ─────────────────────────────────────────────────
  [PASS] twin_instances view exists with correct columns

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

── twin_instances Content ────────────────────────────────
  [PASS] twin_instances.building_code = 'PH-01'
  [PASS] twin_instances.meter_code = 'PH-01-MAIN'
  [PASS] twin_instances.meter_type = 'main'
  [PASS] twin_instances.protocol = 'manual_export'
  [PASS] twin_instances.meter_active = True

── Data Integrity ────────────────────────────────────────
  [PASS] FK: meters.building_id → buildings.building_id
  [PASS] FK: meters.parent_meter_id → meters.meter_id
  [PASS] FK: equipment.building_id → buildings.building_id
  [PASS] FK: baseline_parameters.building_id → buildings.building_id
  [PASS] baseline_parameters is empty (correct — no fabricated values)
  [PASS] equipment.rated_power_kw — no unverified values present

============================================================
  MODULE 1: PASS
============================================================
```

---

## Open Items / Pending Confirmation

| Item | Status |
|---|---|
| Physical mapping of POWERHOUSE_1.POWERHOUSE_1_INCOMER → PH-01-MAIN | pending mentor |
| Confirm "8 meters" = A/B/B-UPS/C/D/E/LIGHTING/DG_1 | pending mentor |
| DG_1 classification (equipment vs meter) | pending mentor |
| FROM/TO_POWERHOUSE_2 topology in registry | pending mentor |
| rated_capacity_kw for any asset | pending source data |
| Baseline kWh/day values | pending State Layer derivation |

---

## Notes

- All NULL fields in the seed are intentional; no values were fabricated.
- `baseline_parameters` is deliberately empty; values will be derived from historical data in the State Layer phase.
- `equipment` table is empty; no equipment engineering values are available from mentor documents yet.
- See `registry/asset_mapping.csv` for the full raw-source → ENTWINE identity mapping with confidence levels.
