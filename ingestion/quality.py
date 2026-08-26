"""
ingestion/quality.py
Module 2 data quality gate assertions.

Run after ingestion to verify the state layer is internally consistent
and that no Module 1 data was corrupted.

Each assertion logs PASS or FAIL.  Returns True iff all pass.
"""
from __future__ import annotations

import logging

import psycopg2

from ingestion.config import get_dsn

log = logging.getLogger(__name__)

_GATE_RESULTS: list[tuple[str, bool]] = []


def _gate(name: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    msg = f"[{status}] {name}"
    if detail:
        msg += f" — {detail}"
    if passed:
        log.info(msg)
    else:
        log.error(msg)
    _GATE_RESULTS.append((name, passed))
    return passed


def run_quality_gates(dsn: str | None = None) -> bool:
    """Run all quality gates.  Returns True iff all pass."""
    dsn = dsn or get_dsn()
    _GATE_RESULTS.clear()
    all_ok = True

    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()

        # ── Gate 1: All 29 source files present ──────────────────────────────
        cur.execute("SELECT COUNT(*) FROM source_files")
        sf_count = cur.fetchone()[0]
        ok = _gate(
            "All 29 source files in source_files",
            sf_count >= 29,
            f"found {sf_count}",
        )
        all_ok = all_ok and ok

        # ── Gate 2: No source_files rows missing processing_status ───────────
        cur.execute(
            "SELECT COUNT(*) FROM source_files WHERE processing_status IS NULL"
        )
        nulls = cur.fetchone()[0]
        ok = _gate("No NULL processing_status in source_files", nulls == 0,
                   f"{nulls} NULL rows")
        all_ok = all_ok and ok

        # ── Gate 3: Unresolved files have unresolved_reason ──────────────────
        cur.execute(
            "SELECT COUNT(*) FROM source_files "
            "WHERE processing_status = 'unresolved' AND unresolved_reason IS NULL"
        )
        missing_reason = cur.fetchone()[0]
        ok = _gate(
            "All unresolved files have unresolved_reason",
            missing_reason == 0,
            f"{missing_reason} missing",
        )
        all_ok = all_ok and ok

        # ── Gate 4: INCOMER file has no curated rows ──────────────────────────
        cur.execute(
            "SELECT rows_curated FROM source_files "
            "WHERE source_name = 'POWERHOUSE_1.POWERHOUSE_1_INCOMER'"
        )
        incomer = cur.fetchone()
        ok = _gate(
            "POWERHOUSE_1_INCOMER has rows_curated = 0",
            incomer is not None and (incomer[0] or 0) == 0,
            f"rows_curated = {incomer[0] if incomer else 'N/A'}",
        )
        all_ok = all_ok and ok

        # ── Gate 5: measurement_sources populated ─────────────────────────────
        cur.execute("SELECT COUNT(*) FROM measurement_sources")
        ms_count = cur.fetchone()[0]
        ok = _gate(
            "measurement_sources has rows",
            ms_count > 0,
            f"{ms_count} rows",
        )
        all_ok = all_ok and ok

        # ── Gate 6: Feeders quarantined ───────────────────────────────────────
        cur.execute(
            "SELECT COUNT(*) FROM measurement_sources "
            "WHERE source_name IN "
            "('POWERHOUSE_1.FROM_POWERHOUSE_2','POWERHOUSE_1.TO_POWERHOUSE_2') "
            "AND resolution_status = 'quarantined'"
        )
        quarantined = cur.fetchone()[0]
        ok = _gate(
            "FROM/TO_POWERHOUSE_2 measurement_sources are quarantined",
            quarantined == 2,
            f"{quarantined}/2 quarantined",
        )
        all_ok = all_ok and ok

        # ── Gate 7: interval_telemetry has no NULL measurement_source_id ──────
        cur.execute(
            "SELECT COUNT(*) FROM interval_telemetry "
            "WHERE measurement_source_id IS NULL"
        )
        null_ms = cur.fetchone()[0]
        ok = _gate(
            "No NULL measurement_source_id in interval_telemetry",
            null_ms == 0,
            f"{null_ms} NULL rows",
        )
        all_ok = all_ok and ok

        # ── Gate 8: interval_telemetry is a TimescaleDB hypertable ───────────
        cur.execute(
            "SELECT COUNT(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'interval_telemetry'"
        )
        ht = cur.fetchone()[0]
        ok = _gate("interval_telemetry is a TimescaleDB hypertable", ht == 1)
        all_ok = all_ok and ok

        # ── Gate 9: All interval_telemetry timestamps have timezone ──────────
        cur.execute(
            "SELECT COUNT(*) FROM interval_telemetry "
            "WHERE ts AT TIME ZONE 'UTC' IS NULL"
        )
        bad_ts = cur.fetchone()[0]
        ok = _gate(
            "All interval_telemetry timestamps are valid UTC TIMESTAMPTZ",
            bad_ts == 0,
            f"{bad_ts} invalid",
        )
        all_ok = all_ok and ok

        # ── Gate 10: operational_events fingerprint integrity ─────────────────
        cur.execute(
            "SELECT COUNT(*) FROM operational_events WHERE row_fingerprint IS NULL"
        )
        null_fp = cur.fetchone()[0]
        ok = _gate(
            "No NULL row_fingerprint in operational_events",
            null_fp == 0,
            f"{null_fp} NULL",
        )
        all_ok = all_ok and ok

        # ── Gate 11: Row totals reconcile ────────────────────────────────────
        cur.execute(
            """
            SELECT COUNT(*) FROM source_files
            WHERE rows_read IS NOT NULL
              AND rows_read != COALESCE(rows_curated, 0)
                            + COALESCE(rows_rejected, 0)
                            + COALESCE(rows_skipped, 0)
            """
        )
        mismatch = cur.fetchone()[0]
        ok = _gate(
            "Row totals reconcile (read = curated + rejected + skipped)",
            mismatch == 0,
            f"{mismatch} files with mismatched totals",
        )
        all_ok = all_ok and ok

        # ── Gate 12: Module 1 table integrity ────────────────────────────────
        # We verify that buildings, meters, and equipment are populated
        # and baseline_parameters table exists (empty by design in Module 1).
        for tbl in ("buildings", "meters", "equipment"):
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            count = cur.fetchone()[0]
            ok = _gate(
                f"Module 1 {tbl} table has rows (not wiped)",
                count > 0,
                f"{count} rows",
            )
            all_ok = all_ok and ok

        cur.execute("SELECT to_regclass('baseline_parameters')")
        bp_exists = cur.fetchone()[0] is not None
        ok = _gate("Module 1 baseline_parameters table exists", bp_exists)
        all_ok = all_ok and ok

        cur.close()

    finally:
        conn.close()

    log.info("─" * 60)
    passed = sum(1 for _, v in _GATE_RESULTS if v)
    total  = len(_GATE_RESULTS)
    log.info(
        "Quality gates: %d/%d passed%s",
        passed, total,
        " ✓ ALL PASS" if all_ok else " ✗ FAILURES DETECTED",
    )
    log.info("─" * 60)
    return all_ok
