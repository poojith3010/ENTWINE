"""
ingestion/reconcile.py
Reconciliation report generator.

Queries Module 2 tables and writes a structured Markdown report to
logs/module2_reconciliation_<timestamp>.md.

Covers:
  - Source file inventory (all 29 files, status per file)
  - Ingestion run history
  - interval_telemetry counts by source
  - daily_energy_reports counts by source
  - operational_events counts by class
  - rejected_records counts by category
  - measurement_sources resolution status
  - Quality gate summary (reuses quality.py)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

from ingestion.config import LOGS_DIR, get_dsn

log = logging.getLogger(__name__)


def generate_report(dsn: str | None = None, out_dir: Path | None = None) -> Path:
    """Write reconciliation report and return its path."""
    dsn     = dsn or get_dsn()
    out_dir = out_dir or LOGS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    ts      = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outfile = out_dir / f"module2_reconciliation_{ts}.md"

    conn = psycopg2.connect(dsn)
    lines: list[str] = []

    def h1(t):  lines.append(f"# {t}\n")
    def h2(t):  lines.append(f"## {t}\n")
    def h3(t):  lines.append(f"### {t}\n")
    def para(t): lines.append(f"{t}\n")
    def hr():   lines.append("---\n")
    def table_row(*cols): lines.append("| " + " | ".join(str(c) for c in cols) + " |")
    def table_sep(n):     lines.append("| " + " | ".join(["---"] * n) + " |")

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        h1(f"ENTWINE Module 2 — Reconciliation Report")
        para(f"_Generated: {datetime.now(timezone.utc).isoformat()}_")
        hr()

        # ── Ingestion runs ────────────────────────────────────────────────────
        h2("Ingestion Runs")
        cur.execute(
            "SELECT run_id, mode, status, started_at, finished_at, "
            "total_files, files_processed, rows_curated, rows_rejected "
            "FROM ingestion_runs ORDER BY run_id"
        )
        runs = cur.fetchall()
        if runs:
            table_row("Run ID", "Mode", "Status", "Started", "Files", "Curated", "Rejected")
            table_sep(7)
            for r in runs:
                table_row(
                    r["run_id"], r["mode"], r["status"],
                    str(r["started_at"])[:19],
                    r["total_files"] or 0,
                    r["rows_curated"] or 0,
                    r["rows_rejected"] or 0,
                )
        else:
            para("No ingestion runs recorded.")
        lines.append("")

        # ── Source files ──────────────────────────────────────────────────────
        h2("Source Files")
        cur.execute(
            "SELECT source_category, processing_status, COUNT(*) as n, "
            "SUM(rows_read) as tot_read, SUM(rows_curated) as tot_curated, "
            "SUM(rows_rejected) as tot_rejected "
            "FROM source_files GROUP BY source_category, processing_status "
            "ORDER BY source_category, processing_status"
        )
        rows = cur.fetchall()
        table_row("Category", "Status", "Files", "Rows Read", "Curated", "Rejected")
        table_sep(6)
        for r in rows:
            table_row(
                r["source_category"], r["processing_status"], r["n"],
                r["tot_read"] or 0, r["tot_curated"] or 0, r["tot_rejected"] or 0,
            )
        lines.append("")

        h3("Unresolved Files")
        cur.execute(
            "SELECT file_name, unresolved_reason FROM source_files "
            "WHERE processing_status = 'unresolved'"
        )
        unresolved = cur.fetchall()
        if unresolved:
            table_row("File", "Reason")
            table_sep(2)
            for r in unresolved:
                table_row(r["file_name"], r["unresolved_reason"] or "—")
        else:
            para("None.")
        lines.append("")

        # ── interval_telemetry ────────────────────────────────────────────────
        h2("interval_telemetry — Rows by Source")
        cur.execute(
            "SELECT source_name, COUNT(*) as n, "
            "MIN(ts) as earliest, MAX(ts) as latest "
            "FROM interval_telemetry "
            "GROUP BY source_name ORDER BY source_name"
        )
        rows = cur.fetchall()
        if rows:
            table_row("Source Name", "Rows", "Earliest (UTC)", "Latest (UTC)")
            table_sep(4)
            for r in rows:
                table_row(
                    r["source_name"], r["n"],
                    str(r["earliest"])[:19], str(r["latest"])[:19],
                )
        else:
            para("No telemetry records.")
        lines.append("")

        # ── daily_energy_reports ──────────────────────────────────────────────
        h2("daily_energy_reports — Rows by Source")
        cur.execute(
            "SELECT source_name, COUNT(*) as n, "
            "MIN(report_date) as earliest, MAX(report_date) as latest "
            "FROM daily_energy_reports "
            "GROUP BY source_name ORDER BY source_name"
        )
        rows = cur.fetchall()
        if rows:
            table_row("Source Name", "Rows", "Earliest", "Latest")
            table_sep(4)
            for r in rows:
                table_row(r["source_name"], r["n"], r["earliest"], r["latest"])
        else:
            para("No daily energy records.")
        lines.append("")

        # ── operational_events ────────────────────────────────────────────────
        h2("operational_events — Rows by Class")
        cur.execute(
            "SELECT event_class, COUNT(*) as n "
            "FROM operational_events GROUP BY event_class ORDER BY event_class"
        )
        rows = cur.fetchall()
        if rows:
            table_row("Event Class", "Rows")
            table_sep(2)
            for r in rows:
                table_row(r["event_class"], r["n"])
        else:
            para("No operational events.")
        lines.append("")

        # ── rejected_records ──────────────────────────────────────────────────
        h2("rejected_records — Rows by Category")
        cur.execute(
            "SELECT error_category, COUNT(*) as n "
            "FROM rejected_records GROUP BY error_category ORDER BY error_category"
        )
        rows = cur.fetchall()
        if rows:
            table_row("Error Category", "Rows")
            table_sep(2)
            for r in rows:
                table_row(r["error_category"], r["n"])
        else:
            para("No rejected records.")
        lines.append("")

        # ── measurement_sources ───────────────────────────────────────────────
        h2("measurement_sources — Resolution Status")
        cur.execute(
            "SELECT source_name, asset_type, resolution_status, "
            "meter_id, equipment_id "
            "FROM measurement_sources ORDER BY source_name"
        )
        rows = cur.fetchall()
        if rows:
            table_row("Source Name", "Asset Type", "Status", "meter_id", "equip_id")
            table_sep(5)
            for r in rows:
                table_row(
                    r["source_name"], r["asset_type"], r["resolution_status"],
                    r["meter_id"] or "—", r["equipment_id"] or "—",
                )
        else:
            para("No measurement_sources records.")
        lines.append("")

        cur.close()

    finally:
        conn.close()

    # Write report.
    outfile.write_text("\n".join(lines), encoding="utf-8")
    log.info("Reconciliation report written to %s", outfile)
    return outfile
