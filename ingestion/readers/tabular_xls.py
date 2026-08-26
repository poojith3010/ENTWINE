"""
ingestion/readers/tabular_xls.py
Parser for POWERHOUSE_1 interval telemetry XLS files.

Each file (New Tabular Report for report(2).xls) contains:
  - A variable-length preamble (report title, blank rows, date range,
    gap/quality warnings).  The header row is found dynamically by
    scanning for a cell containing "Timestamp".
  - One header row with "Timestamp" in col-0 and 17 metric columns.
  - Data rows: M/D/YYYY H:MM:SS AM/PM timestamps with float metric values.
  - A trailing ID row: "ID: <uuid>" — must be detected and skipped.

Returns an iterator of dicts suitable for interval_telemetry ingestion.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Optional

import xlrd

from ingestion.normalize import (
    METRIC_COLUMN_MAP,
    coerce_float,
    is_uuid_row,
    parse_metric_header,
    parse_xls_timestamp,
)

log = logging.getLogger(__name__)

# Canonical metric DB columns (all 17 — presence verified at parse time).
ALL_METRIC_COLS = set(METRIC_COLUMN_MAP.values())


def _cell_str(sheet, row: int, col: int, wb) -> str:
    """Return string representation of a cell, handling date cells."""
    cell = sheet.cell(row, col)
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return str(xlrd.xldate_as_datetime(cell.value, wb.datemode))
        except Exception:
            return str(cell.value)
    if cell.ctype == xlrd.XL_CELL_EMPTY:
        return ""
    return str(cell.value).strip()


def _find_header_row(sheet, wb) -> Optional[int]:
    """Scan sheet rows for the data header (first cell = 'Timestamp').

    Returns the 0-indexed row number, or None if not found.
    """
    for ri in range(sheet.nrows):
        val = _cell_str(sheet, ri, 0, wb).lower()
        if val == "timestamp":
            return ri
    return None


def _parse_metric_columns(
    sheet, header_row: int, source_name: str, wb
) -> dict[int, str]:
    """Return {col_index: canonical_db_column} for metric columns.

    Logs a warning for any unrecognised column (excluding col-0 = Timestamp).
    """
    col_map: dict[int, str] = {}
    for ci in range(1, sheet.ncols):
        raw = _cell_str(sheet, header_row, ci, wb)
        if not raw:
            continue
        canonical = parse_metric_header(raw, source_name)
        if canonical:
            col_map[ci] = canonical
        else:
            log.warning(
                "Unrecognised metric column in %s: %r", source_name, raw
            )
    return col_map


def read_tabular_xls(
    path: Path,
    source_name: str,
    source_file_id: int,
    measurement_source_id: int,
    entwine_asset_code: Optional[str] = None,
    mapping_confidence: Optional[str] = None,
) -> Iterator[dict]:
    """Yield one dict per data row from a POWERHOUSE_1 tabular XLS.

    Parameters
    ----------
    path                  : Path to the .xls file.
    source_name           : Raw source name, e.g. "POWERHOUSE_1.A_BLOCK".
    source_file_id        : FK from source_files.
    measurement_source_id : FK from measurement_sources.
    entwine_asset_code    : From mapping CSV.
    mapping_confidence    : From mapping CSV.

    Yields
    ------
    dict with keys matching interval_telemetry columns plus metadata.
    """
    wb = xlrd.open_workbook(str(path), on_demand=True)
    sheet = wb.sheet_by_index(0)

    header_row_idx = _find_header_row(sheet, wb)
    if header_row_idx is None:
        log.error(
            "No Timestamp header found in %s — file will be marked as failed.",
            path.name,
        )
        return

    col_map = _parse_metric_columns(sheet, header_row_idx, source_name, wb)
    if not col_map:
        log.error("No recognisable metric columns in %s.", path.name)
        return

    rows_yielded = 0
    rows_skipped = 0

    for ri in range(header_row_idx + 1, sheet.nrows):
        ts_raw = _cell_str(sheet, ri, 0, wb)

        # Skip blank rows.
        if not ts_raw:
            rows_skipped += 1
            continue

        # Detect and skip the trailing ID row.
        if is_uuid_row(ts_raw):
            log.debug("Skipping ID row at row %d in %s", ri, path.name)
            rows_skipped += 1
            continue

        ts_utc = parse_xls_timestamp(ts_raw)
        if ts_utc is None:
            log.warning(
                "Invalid timestamp at row %d in %s: %r", ri, path.name, ts_raw
            )
            yield {
                "__rejected__":   True,
                "__row_ref__":    f"Sheet1:row_{ri}",
                "__error__":      f"Invalid timestamp: {ts_raw!r}",
                "__raw_payload__": {f"col_{ci}": _cell_str(sheet, ri, ci, wb)
                                    for ci in range(sheet.ncols)},
            }
            continue

        # Build canonical metric dict.
        metrics: dict[str, Optional[float]] = {col: None for col in ALL_METRIC_COLS}
        raw_row: dict = {"Timestamp": ts_raw}

        for ci, db_col in col_map.items():
            raw_val = _cell_str(sheet, ri, ci, wb)
            raw_row[db_col] = raw_val
            metrics[db_col] = coerce_float(raw_val)

        record = {
            "ts":                    ts_utc,
            "measurement_source_id": measurement_source_id,
            "source_file_id":        source_file_id,
            "source_name":           source_name,
            "entwine_asset_code":    entwine_asset_code,
            "mapping_confidence":    mapping_confidence,
            "source_ts_raw":         ts_raw,
            "raw_payload":           raw_row,
            **metrics,
        }
        rows_yielded += 1
        yield record

    log.info(
        "%s: yielded %d rows, skipped %d",
        path.name, rows_yielded, rows_skipped,
    )
