"""
ingestion/normalize.py
Timestamp and metric normalisation for all Module 2 source types.

Timestamp formats encountered in source files
---------------------------------------------
XLS tabular reports (Server Local = IST):
    "1/1/2025 12:15:00 AM"   → datetime.strptime(..., '%m/%d/%Y %I:%M:%S %p')
    → localize as IST → convert to UTC

CSV files (client-local IST):
    "2026-01-30 17:45:00"           (no milliseconds)
    "2026-01-30 17:45:00.000"       (with milliseconds — rare)
    → datetime.fromisoformat(...)   → localize as IST → convert to UTC

All output timestamps are UTC-aware datetime objects suitable for
TIMESTAMPTZ columns.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import pytz

log = logging.getLogger(__name__)

_IST = pytz.timezone("Asia/Kolkata")
_UTC = pytz.utc

# Pre-compiled patterns.
_XLS_TS_PATTERN = re.compile(
    r"^\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*(AM|PM)$",
    re.IGNORECASE,
)
_ISO_TS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(\.\d+)?$"
)
_UUID_ROW_PATTERN = re.compile(r"^ID:\s+[0-9a-f\-]{36}$", re.IGNORECASE)

# Metric column name → canonical DB column name.
# Header format from XLS: "<SOURCE>\n<Metric Name>\n(<unit>)"
# After stripping source prefix and normalising.
METRIC_COLUMN_MAP = {
    "apparent energy into the load":  "apparent_energy_into_load_kvah",
    "apparent energy":                "apparent_energy_kvah",
    "apparent power":                 "apparent_power_kva",
    "current a":                      "current_a_a",
    "current avg":                    "current_avg_a",
    "current b":                      "current_b_a",
    "current c":                      "current_c_a",
    "frequency":                      "frequency_hz",
    "power factor":                   "power_factor_pct",
    "reactive energy into the load":  "reactive_energy_kvarh",
    "real energy into the load":      "real_energy_kwh",
    "real power a":                   "real_power_a_kw",
    "real power b":                   "real_power_b_kw",
    "real power c":                   "real_power_c_kw",
    "real power":                     "real_power_kw",
    "voltage l-l avg":                "voltage_ll_avg_v",
    "voltage l-n avg":                "voltage_ln_avg_v",
}

ALL_METRIC_COLS = set(METRIC_COLUMN_MAP.values())

# Known sentinel / placeholder values that should be stored as NULL.
_NULL_SENTINELS = {"-0.001", "nan", "none", "", "n/a"}


# ─────────────────────────────────────────────────────────────────────────────
# timestamp helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_xls_timestamp(raw: str) -> Optional[datetime]:
    """Parse XLS M/D/YYYY H:MM:SS AM/PM string → UTC-aware datetime.

    Returns None if the string cannot be parsed or does not match the
    expected format.
    """
    raw = raw.strip()
    if not _XLS_TS_PATTERN.match(raw):
        return None
    try:
        naive = datetime.strptime(raw, "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        try:
            # Fallback for zero-padded variants.
            naive = datetime.strptime(raw, "%-m/%-d/%Y %I:%M:%S %p")
        except ValueError:
            log.debug("Could not parse XLS timestamp: %r", raw)
            return None
    ist_dt = _IST.localize(naive)
    return ist_dt.astimezone(_UTC)


def parse_csv_timestamp(raw: str) -> Optional[datetime]:
    """Parse ISO-like YYYY-MM-DD HH:MM:SS[.mmm] string → UTC-aware datetime.

    Returns None if the string cannot be parsed.
    """
    raw = raw.strip()
    if not _ISO_TS_PATTERN.match(raw):
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        log.debug("Could not parse CSV timestamp: %r", raw)
        return None
    ist_dt = _IST.localize(naive)
    return ist_dt.astimezone(_UTC)


def is_uuid_row(cell_value: str) -> bool:
    """Return True if this looks like the trailing ID row in a tabular XLS."""
    return bool(_UUID_ROW_PATTERN.match(str(cell_value).strip()))


# ─────────────────────────────────────────────────────────────────────────────
# metric helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_metric_header(raw_header: str, source_name: str) -> Optional[str]:
    """Map a raw XLS column header to a canonical DB column name.

    Raw header format: "<SOURCE_NAME>\\n<Metric Name>\\n(<unit>)"
    The source_name prefix is stripped before lookup.

    Returns the canonical column name, or None if the header is not recognised.
    """
    # Strip source prefix and newlines.
    header = raw_header.replace(source_name, "").replace("\n", " ").strip()
    # Remove trailing unit in parentheses.
    header = re.sub(r"\s*\([^)]+\)\s*$", "", header).strip().lower()
    return METRIC_COLUMN_MAP.get(header)


def coerce_float(value: str | float | None) -> Optional[float]:
    """Coerce a cell value to float; return None for sentinel/missing values."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in _NULL_SENTINELS:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None
