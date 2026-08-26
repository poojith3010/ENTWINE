# ENTWINE Module 2 — Reconciliation Report

_Generated: 2026-08-26T15:56:43.510263+00:00_

---

## Ingestion Runs

| Run ID | Mode | Status | Started | Files | Curated | Rejected |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | ingest | done | 2026-08-26 15:52:10 | 29 | 284462 | 67057 |
| 2 | rerun | done | 2026-08-26 15:56:43 | 29 | 284462 | 67057 |

## Source Files

| Category | Status | Files | Rows Read | Curated | Rejected |
| --- | --- | --- | --- | --- | --- |
| alarm_history | done | 2 | 149 | 149 | 0 |
| alarm_status | done | 3 | 115 | 115 | 0 |
| daily_report | done | 7 | 2130 | 2130 | 0 |
| event | done | 1 | 48 | 48 | 0 |
| incident | done | 3 | 33 | 33 | 0 |
| interval_telemetry | done | 11 | 348952 | 281895 | 67057 |
| interval_telemetry | no_data | 1 | 0 | 0 | 0 |
| measurement_csv | done | 1 | 92 | 92 | 0 |

### Unresolved Files

None.


## interval_telemetry — Rows by Source

| Source Name | Rows | Earliest (UTC) | Latest (UTC) |
| --- | --- | --- | --- |
| POWERHOUSE_1.A_BLOCK | 33620 | 2024-12-31 18:45:00 | 2026-01-31 11:00:00 |
| POWERHOUSE_1.B_BLOCK | 33528 | 2024-12-31 18:45:00 | 2025-12-31 18:30:00 |
| POWERHOUSE_1.B_BLOCK_UPS | 33530 | 2024-12-31 18:45:00 | 2025-12-31 18:30:00 |
| POWERHOUSE_1.C_BLOCK | 33529 | 2024-12-31 18:45:00 | 2025-12-31 18:30:00 |
| POWERHOUSE_1.DG_1 | 14610 | 2024-12-31 18:45:00 | 2025-12-24 09:45:00 |
| POWERHOUSE_1.D_BLOCK | 33529 | 2024-12-31 18:45:00 | 2025-12-31 18:30:00 |
| POWERHOUSE_1.E_BLOCK | 33529 | 2024-12-31 18:45:00 | 2025-12-31 18:30:00 |
| POWERHOUSE_1.LIGHTING | 33528 | 2024-12-31 18:45:00 | 2025-12-31 18:30:00 |
| POWERHOUSE_1.MAIN_VCB | 32584 | 2024-12-31 18:45:00 | 2025-12-31 18:30:00 |

## daily_energy_reports — Rows by Source

| Source Name | Rows | Earliest | Latest |
| --- | --- | --- | --- |
| POWERHOUSE_1.A_BLOCK | 326 | 2025-02-03 | 2025-12-31 |
| POWERHOUSE_1.B_BLOCK | 326 | 2025-02-03 | 2025-12-31 |
| POWERHOUSE_1.C_BLOCK | 326 | 2025-02-03 | 2025-12-31 |
| POWERHOUSE_1.DG_1 | 183 | 2025-02-03 | 2025-12-24 |
| POWERHOUSE_1.D_BLOCK | 326 | 2025-02-03 | 2025-12-31 |
| POWERHOUSE_1.E_BLOCK | 326 | 2025-02-03 | 2025-12-31 |
| POWERHOUSE_1.MAIN_VCB | 317 | 2025-02-03 | 2025-12-31 |

## operational_events — Rows by Class

| Event Class | Rows |
| --- | --- |
| alarm_history | 149 |
| alarm_status | 115 |
| event | 48 |
| incident | 33 |

## rejected_records — Rows by Category

| Error Category | Rows |
| --- | --- |
| unresolved_feeder | 67057 |

## measurement_sources — Resolution Status

| Source Name | Asset Type | Status | meter_id | equip_id |
| --- | --- | --- | --- | --- |
| POWERHOUSE_1.A_BLOCK | meter_candidate | resolved | 2 | — |
| POWERHOUSE_1.B_BLOCK | meter_candidate | resolved | 3 | — |
| POWERHOUSE_1.B_BLOCK_UPS | meter_candidate | resolved | 4 | — |
| POWERHOUSE_1.C_BLOCK | meter_candidate | resolved | 5 | — |
| POWERHOUSE_1.DG_1 | equipment_candidate | resolved | — | 2 |
| POWERHOUSE_1.D_BLOCK | meter_candidate | resolved | 6 | — |
| POWERHOUSE_1.E_BLOCK | meter_candidate | resolved | 7 | — |
| POWERHOUSE_1.FROM_POWERHOUSE_2 | feeder | quarantined | — | — |
| POWERHOUSE_1.LIGHTING | meter_candidate | resolved | 8 | — |
| POWERHOUSE_1.MAIN_VCB | switchgear | resolved | — | 1 |
| POWERHOUSE_1.POWERHOUSE_1_INCOMER | meter_candidate | resolved | 1 | — |
| POWERHOUSE_1.TO_POWERHOUSE_2 | feeder | quarantined | — | — |
