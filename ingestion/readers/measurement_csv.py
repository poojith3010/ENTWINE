"""
ingestion/readers/measurement_csv.py
Parser for the narrow-format measurement CSV (1st_fllor_*.csv).

Schema:  Timestamp, Series, Grouping, Value
  - Timestamp : IST, YYYY-MM-DD HH:MM:SS
  - Series    : "<SOURCE_NAME> <Metric Name> (<unit>)"
                e.g. "POWERHOUSE_1.A_BLOCK Current Avg (A)"
  - Grouping  : redundant time-of-day string — ignored
  - Value     : float metric reading

This file is in long/narrow format: one row per timestamp per metric.
Data is treated as interval_telemetry (same table as XLS-derived data).

Source name is extracted from the Series column.
Canonical metric column is resolved from normalize.METRIC_COLUMN_MAP.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator, Optional

import chardet

from ingestion.normalize import (
    ALL_METRIC_COLS,
    METRIC_COLUMN_MAP,
    coerce_float,
    parse_csv_timestamp,
)

log = logging.getLogger(__name__)

_SERIES_PATTERN = re.compile(
    r"^(POWERHOUSE_\d+\.\S+)\s+(.+?)\s*\(([^)]+)\)\s*$"
)


def _detect_encoding(path: Path) -> str:
    raw = path.read_bytes()
    result = chardet.detect(raw[:10_000])
    enc = result.get("encoding") or "utf-8"
    return "utf-8-sig" if enc.lower() in ("utf-8-sig", "utf-8-bom") else enc


def _parse_series(series: str) -> tuple[Optional[str], Optional[str]]:
    """Return (source_name, canonical_db_column) from a Series cell."""
    m = _SERIES_PATTERN.match(series.strip())
    if not m:
        return None, None
    source_name = m.group(1)
    metric_name = m.group(2).strip().lower()
    canonical   = METRIC_COLUMN_MAP.get(metric_name)
    return source_name, canonical


def read_measurement_csv(
    path: Path,
    source_file_id: int,
    source_map: dict,             # {source_name: {measurement_source_id, ...}}
    entwine_asset_code: Optional[str] = None,
    mapping_confidence: Optional[str] = None,
) -> Iterator[dict]:
    """Yield one interval_telemetry dict per data row.

    Each row produces one record with a single non-null metric column.
    """
    import csv

    enc = _detect_encoding(path)
    with path.open(encoding=enc, newline="") as fh:
        reader = csv.DictReader(fh)
        for line_no, row in enumerate(reader, start=2):
            ts_raw   = (row.get("Timestamp") or "").strip()
            series   = (row.get("Series")    or "").strip()
            value    = (row.get("Value")     or "").strip()

            if not ts_raw or not series:
                continue

            source_name, db_col = _parse_series(series)
            if source_name is None or db_col is None:
                log.warning(
                    "%s line %d: unrecognised Series %r — rejected.",
                    path.name, line_no, series,
                )
                yield {
                    "__rejected__":    True,
                    "__row_ref__":     f"CSV:line_{line_no}",
                    "__error__":       f"Unrecognised Series: {series!r}",
                    "__raw_payload__": dict(row),
                }
                continue

            ts_utc = parse_csv_timestamp(ts_raw)
            if ts_utc is None:
                yield {
                    "__rejected__":    True,
                    "__row_ref__":     f"CSV:line_{line_no}",
                    "__error__":       f"Invalid timestamp: {ts_raw!r}",
                    "__raw_payload__": dict(row),
                }
                continue

            # Look up measurement_source_id from the pre-loaded source map.
            ms_entry = source_map.get(source_name, {})
            ms_id    = ms_entry.get("measurement_source_id")
            code     = ms_entry.get("entwine_asset_code") or entwine_asset_code
            conf     = ms_entry.get("confidence")         or mapping_confidence

            # Build a metrics dict with one populated column.
            metrics: dict[str, Optional[float]] = {col: None for col in ALL_METRIC_COLS}
            metrics[db_col] = coerce_float(value)

            yield {
                "ts":                    ts_utc,
                "measurement_source_id": ms_id,
                "source_file_id":        source_file_id,
                "source_name":           source_name,
                "entwine_asset_code":    code,
                "mapping_confidence":    conf,
                "source_ts_raw":         ts_raw,
                "raw_payload":           {"Timestamp": ts_raw, "Series": series,
                                          "Grouping": row.get("Grouping", ""),
                                          "Value": value},
                **metrics,
            }
