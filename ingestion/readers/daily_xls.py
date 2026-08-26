"""
ingestion/readers/daily_xls.py
Parser for DAILY ENERGY REPORT workbooks.

Each workbook (DAILY ENERGY REPORT(264-270).xls) has 12 sheets:
  - Sheet1: annual usage summary — SKIPPED (aggregates monthly values already
    in Sheet2–12; ingesting it would double-count).
  - Sheet2–Sheet12: monthly breakdown by day.

Source name discovery (Blocker B2 fix)
---------------------------------------
The source name is derived from Sheet1 at parse time by scanning for the
first non-empty cell starting with "POWERHOUSE_1.".  If it cannot be found,
the file is recorded as 'unresolved' and no data is written.

Date construction
-----------------
Each Sheet2–12 header row contains:
    "Period : <Month> <Year>\nInterval : Day of Month"
The day is a cell with an integer value (1–31) located in the same column as
the Period header.
Date is constructed as datetime.date(year, month, day_int).
Invalid day numbers (e.g. day 31 in a 30-day month) are rejected.

Yields dicts for daily_energy_reports.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

import xlrd

log = logging.getLogger(__name__)

# Pattern matching "Period : January 2025\nInterval : Day of Month"
_PERIOD_PATTERN = re.compile(
    r"Period\s*:\s*(\w+)\s+(\d{4})\s*\n",
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "january": 1,  "february": 2,  "march": 3,
    "april":   4,  "may":      5,  "june":  6,
    "july":    7,  "august":   8,  "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def _cell_str(sheet, row: int, col: int) -> str:
    cell = sheet.cell(row, col)
    if cell.ctype == xlrd.XL_CELL_EMPTY:
        return ""
    return str(cell.value).strip()


def _detect_source_name(workbook) -> Optional[str]:
    """Read Sheet1 and return the first POWERHOUSE_1.* source found."""
    try:
        sh = workbook.sheet_by_name("Sheet1")
    except xlrd.XLRDError:
        return None
    for ri in range(sh.nrows):
        for ci in range(sh.ncols):
            val = str(sh.cell(ri, ci).value).strip()
            if val.startswith("POWERHOUSE_1."):
                return val
    return None


def _parse_period_header(cell_val: str) -> Optional[tuple[int, int]]:
    """Parse "Period : <Month> <Year>\n..." → (year, month) ints."""
    m = _PERIOD_PATTERN.search(cell_val)
    if not m:
        return None
    month_str = m.group(1).lower()
    year_str  = m.group(2)
    month_num = _MONTH_NAMES.get(month_str)
    if month_num is None:
        return None
    return int(year_str), month_num


def _find_period_row(sheet) -> tuple[Optional[int], Optional[int], Optional[tuple[int, int]]]:
    """Return (row_index, col_index, (year, month)) for the Period header in a sheet."""
    for ri in range(min(sheet.nrows, 12)):
        for ci in range(sheet.ncols):
            val = str(sheet.cell(ri, ci).value)
            result = _parse_period_header(val)
            if result:
                return ri, ci, result
    return None, None, None


def read_daily_xls(
    path: Path,
    source_file_id: int,
    source_map: dict,
) -> tuple[Optional[str], Iterator[dict]]:
    """Parse a DAILY ENERGY REPORT workbook.

    Returns
    -------
    (source_name, iterator_of_records)
    source_name is None if the workbook source could not be determined —
    the caller must mark the file as 'unresolved'.
    """
    wb = xlrd.open_workbook(str(path), on_demand=True)
    source_name = _detect_source_name(wb)
    if source_name is None:
        log.error(
            "Could not detect source name in Sheet1 of %s — marking as unresolved.",
            path.name,
        )
        return None, iter([])

    ms_entry = source_map.get(source_name, {})
    measurement_source_id = ms_entry.get("measurement_source_id")
    entwine_asset_code = ms_entry.get("entwine_asset_code")
    mapping_confidence = ms_entry.get("confidence")

    def _generate():
        for sheet_idx in range(1, wb.nsheets):   # Sheet2 = index 1, ..., Sheet12
            sheet = wb.sheet_by_index(sheet_idx)
            sheet_name = wb.sheet_names()[sheet_idx]

            period_row_idx, day_col_idx, period = _find_period_row(sheet)
            if period is None or day_col_idx is None or period_row_idx is None:
                log.warning(
                    "%s / %s: no Period header found — skipping sheet.",
                    path.name, sheet_name,
                )
                continue

            year, month = period
            max_day = calendar.monthrange(year, month)[1]

            # Data rows begin after the period header row.
            for ri in range(period_row_idx + 1, sheet.nrows):
                # Day column (same column as Period header).
                day_cell = _cell_str(sheet, ri, day_col_idx)
                if not day_cell:
                    continue
                # Skip totals / summary rows.
                if not day_cell.replace(".", "").isdigit():
                    continue
                try:
                    day_int = int(float(day_cell))
                except ValueError:
                    continue
                if not (1 <= day_int <= max_day):
                    log.warning(
                        "%s / %s: invalid day %d for month %d/%d — rejected.",
                        path.name, sheet_name, day_int, month, year,
                    )
                    yield {
                        "__rejected__":    True,
                        "__row_ref__":     f"{sheet_name}:row_{ri}",
                        "__error__":       f"Day {day_int} out of range for {year}-{month:02d}",
                        "__raw_payload__": {
                            f"col_{ci}": _cell_str(sheet, ri, ci)
                            for ci in range(sheet.ncols)
                        },
                    }
                    continue

                try:
                    report_date = date(year, month, day_int)
                except ValueError as exc:
                    log.warning(
                        "%s / %s: cannot construct date (%s) — rejected.",
                        path.name, sheet_name, exc,
                    )
                    continue

                # kWh value column (day_col_idx + 1).
                kwh_raw = ""
                if day_col_idx + 1 < sheet.ncols:
                    kwh_raw = _cell_str(sheet, ri, day_col_idx + 1)
                kwh_val: Optional[float] = None
                if kwh_raw:
                    try:
                        kwh_val = float(kwh_raw)
                    except ValueError:
                        pass

                raw_payload = {
                    f"col_{ci}": _cell_str(sheet, ri, ci)
                    for ci in range(sheet.ncols)
                }

                yield {
                    "measurement_source_id":          measurement_source_id,
                    "source_file_id":                 source_file_id,
                    "report_date":                    report_date,
                    "source_name":                    source_name,
                    "entwine_asset_code":             entwine_asset_code,
                    "mapping_confidence":             mapping_confidence,
                    "real_energy_kwh":                kwh_val,
                    "apparent_energy_into_load_kvah": None,   # not in monthly sheets
                    "sheet_name":                     sheet_name,
                    "raw_payload":                    raw_payload,
                }

    return source_name, _generate()
