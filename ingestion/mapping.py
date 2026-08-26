"""
ingestion/mapping.py
Loads and validates registry/asset_mapping.csv.

Provides two views of the mapping rows:
  * approved_rows()    — sources with entwine_asset_code != 'TBD'
  * quarantined_rows() — sources with entwine_asset_code == 'TBD'
  * all_rows()         — everything

Each row is returned as a plain dict keyed by CSV column names.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterator

from ingestion.config import ASSET_MAPPING_CSV

log = logging.getLogger(__name__)

# Required columns that must exist in the CSV.
_REQUIRED_COLUMNS = {
    "source_name",
    "entwine_asset_code",
    "asset_type",
    "building_code",
    "confidence",
}


def _load(path: Path | None = None) -> list[dict]:
    """Read the asset mapping CSV and return all rows as dicts.

    Raises
    ------
    FileNotFoundError
        If the CSV does not exist at the expected path.
    ValueError
        If required columns are missing.
    """
    path = path or ASSET_MAPPING_CSV
    if not path.exists():
        raise FileNotFoundError(
            f"Asset mapping CSV not found: {path}\n"
            "Expected at registry/asset_mapping.csv"
        )

    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = set(reader.fieldnames or [])
        missing = _REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(
                f"asset_mapping.csv is missing required columns: {sorted(missing)}\n"
                f"Found: {sorted(columns)}"
            )
        rows = list(reader)

    log.debug("Loaded %d rows from %s", len(rows), path)
    return rows


def all_rows(path: Path | None = None) -> list[dict]:
    """Return every row from the mapping CSV."""
    return _load(path)


def approved_rows(path: Path | None = None) -> list[dict]:
    """Return rows where entwine_asset_code is not 'TBD' or empty."""
    return [
        r for r in _load(path)
        if r.get("entwine_asset_code", "").strip().upper() not in ("TBD", "")
    ]


def quarantined_rows(path: Path | None = None) -> list[dict]:
    """Return rows where entwine_asset_code is 'TBD' or empty."""
    return [
        r for r in _load(path)
        if r.get("entwine_asset_code", "").strip().upper() in ("TBD", "")
    ]


def telemetry_source_map(path: Path | None = None) -> dict[str, dict]:
    """Return {source_name: row} for every mapping row.
    Used by the ingestion pipeline to look up asset info by raw source name.
    """
    return {r["source_name"]: r for r in _load(path)}
