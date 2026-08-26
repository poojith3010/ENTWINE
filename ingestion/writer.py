"""
ingestion/writer.py
Batch DB writer for Module 2 curated records.

All writes go into Module 2 tables only:
    interval_telemetry, daily_energy_reports, operational_events,
    source_files, ingestion_run_files, rejected_records.

Records yielded from readers with __rejected__ = True are routed to
rejected_records automatically.

Batch size: BATCH_SIZE rows per transaction (default 1,000 from config).
Progress is logged every LOG_PROGRESS_EVERY rows.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

import psycopg2
import psycopg2.extras

from ingestion.config import BATCH_SIZE, LOG_PROGRESS_EVERY

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# source_files helpers
# ─────────────────────────────────────────────────────────────────────────────

def upsert_source_file(conn, sf_data: dict) -> int:
    """Insert or retrieve source_file_id for a given checksum.

    Returns the source_file_id (existing or newly created).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_files
                (file_path, file_name, source_category, source_name,
                 file_size_bytes, sha256_checksum, processing_status)
            VALUES (%(file_path)s, %(file_name)s, %(source_category)s,
                    %(source_name)s, %(file_size_bytes)s,
                    %(sha256_checksum)s, 'pending')
            ON CONFLICT (sha256_checksum) DO NOTHING
            RETURNING source_file_id
            """,
            sf_data,
        )
        result = cur.fetchone()
        if result:
            return result[0]
        # File already in table — fetch existing id.
        cur.execute(
            "SELECT source_file_id FROM source_files WHERE sha256_checksum = %s",
            (sf_data["sha256_checksum"],),
        )
        return cur.fetchone()[0]


def update_source_file_status(
    conn,
    source_file_id: int,
    status: str,
    rows_read: int = 0,
    rows_curated: int = 0,
    rows_rejected: int = 0,
    rows_skipped: int = 0,
    source_name: str | None = None,
    unresolved_reason: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE source_files SET
                processing_status = %s,
                rows_read         = %s,
                rows_curated      = %s,
                rows_rejected     = %s,
                rows_skipped      = %s,
                source_name       = COALESCE(%s, source_name),
                unresolved_reason = %s
            WHERE source_file_id = %s
            """,
            (status, rows_read, rows_curated, rows_rejected, rows_skipped,
             source_name, unresolved_reason, source_file_id),
        )


def link_run_file(conn, run_id: int, source_file_id: int,
                  action: str, detail: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_run_files (run_id, source_file_id, action, detail)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, source_file_id) DO UPDATE
                SET action = EXCLUDED.action,
                    detail = EXCLUDED.detail
            """,
            (run_id, source_file_id, action, detail),
        )


# ─────────────────────────────────────────────────────────────────────────────
# ingestion_runs helpers
# ─────────────────────────────────────────────────────────────────────────────

def create_run(conn, mode: str, code_version: str = "") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingestion_runs (mode, code_version) "
            "VALUES (%s, %s) RETURNING run_id",
            (mode, code_version),
        )
        return cur.fetchone()[0]


def finish_run(conn, run_id: int, status: str, totals: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ingestion_runs SET
                finished_at     = now(),
                status          = %s,
                total_files     = %s,
                files_processed = %s,
                files_skipped   = %s,
                files_failed    = %s,
                rows_curated    = %s,
                rows_rejected   = %s,
                rows_skipped    = %s
            WHERE run_id = %s
            """,
            (
                status,
                totals.get("total_files", 0),
                totals.get("files_processed", 0),
                totals.get("files_skipped", 0),
                totals.get("files_failed", 0),
                totals.get("rows_curated", 0),
                totals.get("rows_rejected", 0),
                totals.get("rows_skipped", 0),
                run_id,
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# rejected_records helper
# ─────────────────────────────────────────────────────────────────────────────

def write_rejected(conn, source_file_id: int, row_ref: str,
                   category: str, message: str, payload: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rejected_records
                (source_file_id, row_reference, error_category,
                 error_message, raw_payload)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (source_file_id, row_ref, category, message,
             psycopg2.extras.Json(payload)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# batch writers
# ─────────────────────────────────────────────────────────────────────────────

_TELEMETRY_INSERT = """
INSERT INTO interval_telemetry (
    ts, measurement_source_id, source_file_id, source_name,
    entwine_asset_code, mapping_confidence,
    real_power_kw, real_power_a_kw, real_power_b_kw, real_power_c_kw,
    reactive_energy_kvarh, real_energy_kwh,
    apparent_energy_kvah, apparent_energy_into_load_kvah,
    apparent_power_kva,
    current_avg_a, current_a_a, current_b_a, current_c_a,
    voltage_ll_avg_v, voltage_ln_avg_v,
    frequency_hz, power_factor_pct,
    source_ts_raw, raw_payload
) VALUES (
    %(ts)s, %(measurement_source_id)s, %(source_file_id)s, %(source_name)s,
    %(entwine_asset_code)s, %(mapping_confidence)s,
    %(real_power_kw)s, %(real_power_a_kw)s, %(real_power_b_kw)s, %(real_power_c_kw)s,
    %(reactive_energy_kvarh)s, %(real_energy_kwh)s,
    %(apparent_energy_kvah)s, %(apparent_energy_into_load_kvah)s,
    %(apparent_power_kva)s,
    %(current_avg_a)s, %(current_a_a)s, %(current_b_a)s, %(current_c_a)s,
    %(voltage_ll_avg_v)s, %(voltage_ln_avg_v)s,
    %(frequency_hz)s, %(power_factor_pct)s,
    %(source_ts_raw)s, %(raw_payload)s
)
ON CONFLICT (source_file_id, source_name, ts) DO NOTHING
"""

_DAILY_INSERT = """
INSERT INTO daily_energy_reports (
    measurement_source_id, source_file_id, report_date, source_name,
    entwine_asset_code, mapping_confidence,
    real_energy_kwh, apparent_energy_into_load_kvah,
    sheet_name, raw_payload
) VALUES (
    %(measurement_source_id)s, %(source_file_id)s, %(report_date)s, %(source_name)s,
    %(entwine_asset_code)s, %(mapping_confidence)s,
    %(real_energy_kwh)s, %(apparent_energy_into_load_kvah)s,
    %(sheet_name)s, %(raw_payload)s
)
ON CONFLICT (source_file_id, source_name, report_date) DO NOTHING
"""

_EVENT_INSERT = """
INSERT INTO operational_events (
    source_file_id, event_class, source_event_id, priority, device_name,
    measurement_source_id, entwine_asset_code, mapping_confidence,
    alarm_name, alarm_details, event_type, condition_text,
    measurement, measurement_value, event_sub_type,
    active, unacknowledged, occurrences,
    start_time_ist, end_time_ist, start_time_raw, end_time_raw,
    raw_payload, row_fingerprint
) VALUES (
    %(source_file_id)s, %(event_class)s, %(source_event_id)s, %(priority)s, %(device_name)s,
    %(measurement_source_id)s, %(entwine_asset_code)s, %(mapping_confidence)s,
    %(alarm_name)s, %(alarm_details)s, %(event_type)s, %(condition_text)s,
    %(measurement)s, %(measurement_value)s, %(event_sub_type)s,
    %(active)s, %(unacknowledged)s, %(occurrences)s,
    %(start_time_ist)s, %(end_time_ist)s, %(start_time_raw)s, %(end_time_raw)s,
    %(raw_payload)s, %(row_fingerprint)s
)
ON CONFLICT (row_fingerprint) DO NOTHING
"""


def _safe_json(val):
    if val is None:
        return None
    return psycopg2.extras.Json(val)


def _prep_telemetry(r: dict) -> dict:
    r = dict(r)
    r["raw_payload"] = _safe_json(r.get("raw_payload"))
    return r


def _prep_event(r: dict) -> dict:
    r = dict(r)
    r.setdefault("alarm_details", None)
    r.setdefault("event_type", None)
    r.setdefault("condition_text", None)
    r.setdefault("measurement", None)
    r.setdefault("measurement_value", None)
    r.setdefault("event_sub_type", None)
    r.setdefault("active", None)
    r.setdefault("unacknowledged", None)
    r.setdefault("occurrences", None)
    r["raw_payload"] = _safe_json(r.get("raw_payload"))
    return r


def write_batch(
    conn,
    table: str,            # 'telemetry' | 'daily' | 'event'
    records: Iterable[dict],
    source_file_id: int,
    dry_run: bool = False,
) -> dict:
    """Write records in batches of BATCH_SIZE.

    Returns: {"curated": int, "rejected": int, "skipped": int}
    """
    counts = {"curated": 0, "rejected": 0, "skipped": 0}
    batch: list[dict] = []
    total = 0

    def _flush(b: list[dict]):
        if dry_run or not b:
            return
        if table == "telemetry":
            sql  = _TELEMETRY_INSERT
            prep = _prep_telemetry
        elif table == "daily":
            sql  = _DAILY_INSERT
            prep = lambda r: {**r, "raw_payload": _safe_json(r.get("raw_payload"))}
        else:
            sql  = _EVENT_INSERT
            prep = _prep_event

        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur, sql, [prep(r) for r in b], page_size=BATCH_SIZE
            )

    for record in records:
        if record.get("__rejected__"):
            counts["rejected"] += 1
            if not dry_run:
                write_rejected(
                    conn,
                    source_file_id,
                    record.get("__row_ref__", ""),
                    record.get("__error_category__", "parse_error"),
                    record.get("__error__", ""),
                    record.get("__raw_payload__", {}),
                )
            continue

        batch.append(record)
        total += 1

        if len(batch) >= BATCH_SIZE:
            _flush(batch)
            counts["curated"] += len(batch)
            batch = []

            if total % LOG_PROGRESS_EVERY == 0:
                log.info("  … %d rows processed", total)

    if batch:
        _flush(batch)
        counts["curated"] += len(batch)

    return counts
