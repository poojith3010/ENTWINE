"""
ingestion/run.py
Command-line entry point for ENTWINE Module 2 Historical State Layer.

Supported operations
--------------------
  --migrate          : Apply pending SQL migrations in migrations/
  --register-assets  : Register approved meter/equipment assets in Module 1
                       and populate measurement_sources and mapping_snapshots
  --dry-run          : Profile and parse all files without persisting state data
  --ingest           : Run full ingestion of all 29 historical source files
  --rerun            : Ingest only new or previously un-ingested files (idempotent)
  --validate         : Run Module 2 data quality gates
  --reconcile        : Generate structured Markdown reconciliation report in logs/

Usage examples
--------------
  python -m ingestion.run --migrate
  python -m ingestion.run --register-assets
  python -m ingestion.run --dry-run
  python -m ingestion.run --ingest
  python -m ingestion.run --validate
  python -m ingestion.run --reconcile
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

from ingestion.config import DATA_DIR, get_dsn
from ingestion.discover import discover_sources, SourceFile
from ingestion.mapping import telemetry_source_map
from ingestion.migrate import run_migrations
from ingestion.quality import run_quality_gates
from ingestion.reconcile import generate_report
from ingestion.register_assets import register_assets
from ingestion.writer import (
    create_run,
    finish_run,
    link_run_file,
    update_source_file_status,
    upsert_source_file,
    write_batch,
)
from ingestion.readers.alarm_csv import read_alarm_csv
from ingestion.readers.daily_xls import read_daily_xls
from ingestion.readers.measurement_csv import read_measurement_csv
from ingestion.readers.tabular_xls import read_tabular_xls

log = logging.getLogger("ingestion")


# ─────────────────────────────────────────────────────────────────────────────
# source map loader from DB + mapping CSV
# ─────────────────────────────────────────────────────────────────────────────

def _load_active_source_map(conn) -> dict[str, dict]:
    """Combine asset_mapping.csv with measurement_sources DB table.

    Returns {source_name: {measurement_source_id, entwine_asset_code, confidence, resolution_status}}
    """
    csv_map = telemetry_source_map()
    source_map: dict[str, dict] = {}

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT measurement_source_id, source_name, entwine_asset_code,
                   mapping_confidence, resolution_status
            FROM measurement_sources
            """
        )
        for r in cur.fetchall():
            sname = r["source_name"]
            csv_info = csv_map.get(sname, {})
            source_map[sname] = {
                "measurement_source_id": r["measurement_source_id"],
                "entwine_asset_code":    r["entwine_asset_code"] or csv_info.get("entwine_asset_code"),
                "confidence":            r["mapping_confidence"] or csv_info.get("confidence"),
                "resolution_status":     r["resolution_status"],
            }

    # Include any mapping rows not yet in measurement_sources
    for sname, info in csv_map.items():
        if sname not in source_map:
            source_map[sname] = {
                "measurement_source_id": None,
                "entwine_asset_code":    info.get("entwine_asset_code"),
                "confidence":            info.get("confidence"),
                "resolution_status":     "unresolved",
            }

    return source_map


# ─────────────────────────────────────────────────────────────────────────────
# file processor
# ─────────────────────────────────────────────────────────────────────────────

def _process_file(
    conn,
    sf: SourceFile,
    source_map: dict[str, dict],
    run_id: int,
    dry_run: bool = False,
    is_rerun: bool = False,
) -> dict:
    """Process a single SourceFile. Returns file level stats dict."""
    file_path = Path(sf.file_path)
    if not file_path.is_absolute():
        file_path = DATA_DIR.parent / file_path

    stats = {
        "rows_read": 0,
        "rows_curated": 0,
        "rows_rejected": 0,
        "rows_skipped": 0,
        "status": "done",
        "action": "ingested",
        "detail": None,
    }

    # Check if already processed in rerun mode
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_file_id, processing_status, rows_read, rows_curated, rows_rejected, rows_skipped "
            "FROM source_files WHERE sha256_checksum = %s",
            (sf.sha256_checksum,),
        )
        existing = cur.fetchone()

    if existing:
        sf_id, proc_status, r_read, r_cur, r_rej, r_skip = existing
        sf.source_file_id = sf_id
        if is_rerun and proc_status in ("done", "no_data"):
            log.info("SKIP (rerun) %s (already %s)", sf.file_name, proc_status)
            if not dry_run:
                link_run_file(conn, run_id, sf_id, "skipped_duplicate", f"status was {proc_status}")
            stats["status"] = "skipped"
            stats["action"] = "skipped_duplicate"
            stats["rows_read"] = r_read or 0
            stats["rows_curated"] = r_cur or 0
            stats["rows_rejected"] = r_rej or 0
            stats["rows_skipped"] = r_skip or 0
            return stats
    else:
        if not dry_run:
            sf.source_file_id = upsert_source_file(conn, {
                "file_path": sf.file_path,
                "file_name": sf.file_name,
                "source_category": sf.source_category,
                "source_name": sf.source_name,
                "file_size_bytes": sf.file_size_bytes,
                "sha256_checksum": sf.sha256_checksum,
            })
        else:
            sf.source_file_id = -1

    source_file_id = sf.source_file_id or -1

    # ── Category 1: interval_telemetry XLS ────────────────────────────────────
    if sf.source_category == "interval_telemetry":
        sname = sf.source_name or ""
        sinfo = source_map.get(sname, {})
        ms_id = sinfo.get("measurement_source_id")
        code = sinfo.get("entwine_asset_code")
        conf = sinfo.get("confidence")
        res_status = sinfo.get("resolution_status", "unresolved")

        # Check for known zero-data file (POWERHOUSE_1_INCOMER)
        if sname == "POWERHOUSE_1.POWERHOUSE_1_INCOMER":
            log.info("NO DATA file: %s (%s)", sf.file_name, sname)
            stats["status"] = "no_data"
            stats["action"] = "no_data"
            stats["detail"] = "Contains preamble only; zero telemetry data rows"
            if not dry_run:
                update_source_file_status(
                    conn, source_file_id, "no_data",
                    rows_read=0, rows_curated=0, rows_rejected=0, rows_skipped=0,
                    source_name=sname,
                )
                link_run_file(conn, run_id, source_file_id, "no_data", stats["detail"])
            return stats

        # Quarantined feeder (FROM_POWERHOUSE_2, TO_POWERHOUSE_2)
        if res_status == "quarantined" or code == "TBD":
            log.info("QUARANTINE ingest: %s (%s)", sf.file_name, sname)
            records = read_tabular_xls(
                file_path, sname, source_file_id, ms_id or 0, code, conf
            )
            # Route all records to rejected_records with 'unresolved_feeder'
            def _quarantine_gen():
                for r in records:
                    if r.get("__rejected__"):
                        yield r
                    else:
                        yield {
                            "__rejected__": True,
                            "__row_ref__": f"Sheet1:{r.get('source_ts_raw')}",
                            "__error_category__": "unresolved_feeder",
                            "__error__": f"Feeder source '{sname}' is quarantined pending topology confirmation",
                            "__raw_payload__": r.get("raw_payload", {}),
                        }
            counts = write_batch(conn, "telemetry", _quarantine_gen(), source_file_id, dry_run)
            stats["rows_read"] = counts["curated"] + counts["rejected"] + counts["skipped"]
            stats["rows_curated"] = counts["curated"]
            stats["rows_rejected"] = counts["rejected"]
            stats["rows_skipped"] = counts["skipped"]
            if not dry_run:
                update_source_file_status(
                    conn, source_file_id, "done",
                    rows_read=stats["rows_read"],
                    rows_curated=stats["rows_curated"],
                    rows_rejected=stats["rows_rejected"],
                    rows_skipped=stats["rows_skipped"],
                    source_name=sname,
                )
                link_run_file(conn, run_id, source_file_id, "ingested", "quarantined feeder records rejected")
            return stats

        # Standard interval telemetry
        if not ms_id:
            log.error("No measurement_source_id for source %s in %s", sname, sf.file_name)
            stats["status"] = "failed"
            stats["action"] = "failed"
            stats["detail"] = f"Missing measurement_sources entry for '{sname}'"
            if not dry_run:
                update_source_file_status(conn, source_file_id, "failed", unresolved_reason=stats["detail"])
                link_run_file(conn, run_id, source_file_id, "failed", stats["detail"])
            return stats

        records = read_tabular_xls(
            file_path, sname, source_file_id, ms_id, code, conf
        )
        counts = write_batch(conn, "telemetry", records, source_file_id, dry_run)
        stats["rows_read"] = counts["curated"] + counts["rejected"] + counts["skipped"]
        stats["rows_curated"] = counts["curated"]
        stats["rows_rejected"] = counts["rejected"]
        stats["rows_skipped"] = counts["skipped"]
        if not dry_run:
            update_source_file_status(
                conn, source_file_id, "done",
                rows_read=stats["rows_read"],
                rows_curated=stats["rows_curated"],
                rows_rejected=stats["rows_rejected"],
                rows_skipped=stats["rows_skipped"],
                source_name=sname,
            )
            link_run_file(conn, run_id, source_file_id, "ingested")
        return stats

    # ── Category 2: daily_report XLS ──────────────────────────────────────────
    elif sf.source_category == "daily_report":
        detected_src, records = read_daily_xls(file_path, source_file_id, source_map)
        if detected_src is None:
            log.warning("UNRESOLVED daily workbook: %s", sf.file_name)
            stats["status"] = "unresolved"
            stats["action"] = "unresolved"
            stats["detail"] = "Source name not detectable in Sheet1"
            if not dry_run:
                update_source_file_status(
                    conn, source_file_id, "unresolved",
                    unresolved_reason=stats["detail"],
                )
                link_run_file(conn, run_id, source_file_id, "unresolved", stats["detail"])
            return stats

        counts = write_batch(conn, "daily", records, source_file_id, dry_run)
        stats["rows_read"] = counts["curated"] + counts["rejected"] + counts["skipped"]
        stats["rows_curated"] = counts["curated"]
        stats["rows_rejected"] = counts["rejected"]
        stats["rows_skipped"] = counts["skipped"]
        if not dry_run:
            update_source_file_status(
                conn, source_file_id, "done",
                rows_read=stats["rows_read"],
                rows_curated=stats["rows_curated"],
                rows_rejected=stats["rows_rejected"],
                rows_skipped=stats["rows_skipped"],
                source_name=detected_src,
            )
            link_run_file(conn, run_id, source_file_id, "ingested")
        return stats

    # ── Category 3: measurement_csv (1st_floor) ───────────────────────────────
    elif sf.source_category == "measurement_csv":
        records = read_measurement_csv(file_path, source_file_id, source_map)
        counts = write_batch(conn, "telemetry", records, source_file_id, dry_run)
        stats["rows_read"] = counts["curated"] + counts["rejected"] + counts["skipped"]
        stats["rows_curated"] = counts["curated"]
        stats["rows_rejected"] = counts["rejected"]
        stats["rows_skipped"] = counts["skipped"]
        if not dry_run:
            update_source_file_status(
                conn, source_file_id, "done",
                rows_read=stats["rows_read"],
                rows_curated=stats["rows_curated"],
                rows_rejected=stats["rows_rejected"],
                rows_skipped=stats["rows_skipped"],
                source_name="POWERHOUSE_1.A_BLOCK",
            )
            link_run_file(conn, run_id, source_file_id, "ingested")
        return stats

    # ── Category 4: alarms / events / incidents CSV ───────────────────────────
    elif sf.source_category in ("alarm_history", "alarm_status", "event", "incident"):
        records = read_alarm_csv(file_path, sf.source_category, source_file_id, source_map)
        counts = write_batch(conn, "event", records, source_file_id, dry_run)
        stats["rows_read"] = counts["curated"] + counts["rejected"] + counts["skipped"]
        stats["rows_curated"] = counts["curated"]
        stats["rows_rejected"] = counts["rejected"]
        stats["rows_skipped"] = counts["skipped"]
        if not dry_run:
            update_source_file_status(
                conn, source_file_id, "done",
                rows_read=stats["rows_read"],
                rows_curated=stats["rows_curated"],
                rows_rejected=stats["rows_rejected"],
                rows_skipped=stats["rows_skipped"],
            )
            link_run_file(conn, run_id, source_file_id, "ingested")
        return stats

    else:
        log.warning("Unknown category '%s' for %s", sf.source_category, sf.file_name)
        stats["status"] = "skipped"
        return stats


# ─────────────────────────────────────────────────────────────────────────────
# main pipeline orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    mode: str = "ingest",
    dsn: Optional[str] = None,
) -> bool:
    """Execute the ingestion pipeline in dry_run, ingest, or rerun mode."""
    dsn = dsn or get_dsn()
    is_dry = mode == "dry_run"
    is_rerun = mode == "rerun"

    log.info("=" * 70)
    log.info("ENTWINE Module 2 Ingestion Pipeline — Mode: %s", mode.upper())
    log.info("=" * 70)

    conn = psycopg2.connect(dsn)
    conn.autocommit = True

    try:
        source_map = _load_active_source_map(conn)
        discovered = discover_sources()

        run_id = 0
        if not is_dry:
            run_id = create_run(conn, mode, code_version="1.0.0")
            log.info("Created ingestion run: ID %d", run_id)
        else:
            log.info("[dry-run] Discovery completed — %d source files found", len(discovered))

        totals = {
            "total_files":     len(discovered),
            "files_processed": 0,
            "files_skipped":   0,
            "files_failed":    0,
            "rows_curated":    0,
            "rows_rejected":   0,
            "rows_skipped":    0,
        }

        for idx, sf in enumerate(discovered, start=1):
            log.info("[%2d/%2d] Processing %s (%s)...", idx, len(discovered), sf.file_name, sf.source_category)
            fstats = _process_file(conn, sf, source_map, run_id, dry_run=is_dry, is_rerun=is_rerun)

            if fstats["status"] == "skipped":
                totals["files_skipped"] += 1
            elif fstats["status"] == "failed":
                totals["files_failed"] += 1
            else:
                totals["files_processed"] += 1

            totals["rows_curated"]  += fstats["rows_curated"]
            totals["rows_rejected"] += fstats["rows_rejected"]
            totals["rows_skipped"]  += fstats["rows_skipped"]

            log.info(
                "       -> status=%s, curated=%d, rejected=%d, skipped=%d",
                fstats["status"], fstats["rows_curated"], fstats["rows_rejected"], fstats["rows_skipped"]
            )

        overall_status = "done" if totals["files_failed"] == 0 else "partial"
        if not is_dry:
            finish_run(conn, run_id, overall_status, totals)
            log.info("Completed ingestion run: ID %d (status: %s)", run_id, overall_status)

        log.info("─" * 70)
        log.info("Summary totals:")
        for k, v in totals.items():
            log.info("  %-20s : %d", k, v)
        log.info("─" * 70)

        # Generate reconciliation report
        if not is_dry:
            report_path = generate_report(dsn)
            log.info("Reconciliation report generated at: %s", report_path)

        return totals["files_failed"] == 0

    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI parser
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ENTWINE Module 2: Historical State Layer Ingestion & Management CLI"
    )
    parser.add_argument(
        "--migrate", action="store_true",
        help="Apply database schema migrations for Module 2",
    )
    parser.add_argument(
        "--register-assets", action="store_true",
        help="Register approved meter and equipment assets and create mapping snapshots",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate ingestion without writing curated state data to database",
    )
    parser.add_argument(
        "--ingest", action="store_true",
        help="Run full ingestion of all discovered source files",
    )
    parser.add_argument(
        "--rerun", action="store_true",
        help="Idempotently ingest only new or un-ingested files",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Run all Module 2 data quality gates",
    )
    parser.add_argument(
        "--reconcile", action="store_true",
        help="Generate a structured Markdown reconciliation report in logs/",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not any(vars(args).values()):
        parser.print_help()
        sys.exit(0)

    if args.migrate:
        log.info("Running schema migrations...")
        run_migrations()

    if args.register_assets:
        log.info("Registering mapped assets from asset_mapping.csv...")
        register_assets()

    if args.dry_run:
        success = run_pipeline(mode="dry_run")
        if not success:
            sys.exit(1)

    if args.ingest:
        success = run_pipeline(mode="ingest")
        if not success:
            sys.exit(1)

    if args.rerun:
        success = run_pipeline(mode="rerun")
        if not success:
            sys.exit(1)

    if args.validate:
        log.info("Running quality gates...")
        passed = run_quality_gates()
        if not passed:
            sys.exit(1)

    if args.reconcile:
        log.info("Generating reconciliation report...")
        report_file = generate_report()
        print(f"\nReconciliation report written to: {report_file}")


if __name__ == "__main__":
    main()
