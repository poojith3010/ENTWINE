"""
ingestion/discover.py
Source file discovery and SHA-256 checksum manifest.

Scans the real time energy data/ directory tree and classifies every file
into one of the source categories defined in the Module 2 plan.  Returns
SourceFile dataclass instances suitable for insertion into source_files.

Classification rules
--------------------
* Alarms/1st_fllor_*.csv                              → measurement_csv
* Alarms/Alarm_History_*.csv                          → alarm_history
* Alarms/Alarm_Status_*.csv                           → alarm_status
* Alarms/Event_History_*.csv                          → event
* Alarms/Incident_History_*.csv                       → incident
* POWERHOUSE_1/<src>/New Tabular Report*.xls          → interval_telemetry
* daily energy report/DAILY ENERGY REPORT*.xls        → daily_report
* Anything else                                       → unknown (logged, skipped)

Files already recorded in source_files (by SHA-256 checksum) are returned
with already_ingested=True so the pipeline can skip them in rerun mode.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ingestion.config import DATA_DIR

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SourceFile:
    file_path:         str
    file_name:         str
    source_category:   str   # matches processing_status enum values in plan
    source_name:       Optional[str]
    file_size_bytes:   int
    sha256_checksum:   str
    # Set by the pipeline after DB lookup:
    source_file_id:    Optional[int]  = None
    already_ingested:  bool           = False
    processing_status: str            = "pending"


# ─────────────────────────────────────────────────────────────────────────────
# checksum helper
# ─────────────────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# source_name extractor for interval_telemetry XLS
# ─────────────────────────────────────────────────────────────────────────────

def _source_name_from_xls_path(path: Path) -> str:
    """Derive raw source name from the XLS file's parent folder name.

    Example:
        .../POWERHOUSE_1.A_BLOCK/New Tabular Report for report(2).xls
        → "POWERHOUSE_1.A_BLOCK"
    """
    return path.parent.name


# ─────────────────────────────────────────────────────────────────────────────
# classifier
# ─────────────────────────────────────────────────────────────────────────────

def _classify(path: Path) -> tuple[str, Optional[str]]:
    """Return (source_category, source_name) for a given path.

    Returns ("unknown", None) if the file does not match any known pattern.
    """
    rel = path.relative_to(DATA_DIR)
    parts = rel.parts    # e.g. ("Alarms", "Alarm_History_...csv")
    name  = path.name.lower()

    # ── Alarms folder ──
    if parts[0].lower() == "alarms":
        if "1st_fllor" in name or "1st_floor" in name:
            return "measurement_csv", None  # source resolved by reader
        if "alarm_history" in name:
            return "alarm_history", None
        if "alarm_status" in name:
            return "alarm_status", None
        if "event_history" in name:
            return "event", None
        if "incident_history" in name:
            return "incident", None
        log.warning("Unclassified Alarms file: %s", path)
        return "unknown", None

    # ── POWERHOUSE_1 tabular XLS ──
    if parts[0].lower() == "powerhouse_1" and "tabular report" in name:
        src_name = _source_name_from_xls_path(path)
        return "interval_telemetry", src_name

    # ── Daily energy report XLS ──
    if parts[0].lower() == "daily energy report" and name.endswith(".xls"):
        return "daily_report", None   # source resolved by reader from Sheet1

    log.warning("Unclassified file: %s", path)
    return "unknown", None


# ─────────────────────────────────────────────────────────────────────────────
# main discovery function
# ─────────────────────────────────────────────────────────────────────────────

def discover_sources(data_dir: Path | None = None) -> list[SourceFile]:
    """Walk DATA_DIR and return a SourceFile for every recognised file.

    Unknown files are logged and excluded.
    """
    data_dir = data_dir or DATA_DIR
    files: list[SourceFile] = []

    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        # Skip temporary/hidden files.
        if path.name.startswith(("~$", ".")):
            continue
        # Skip non-data extensions.
        if path.suffix.lower() not in (".xls", ".xlsx", ".csv"):
            continue

        category, source_name = _classify(path)
        if category == "unknown":
            continue

        checksum = sha256_file(path)
        rel_path = str(path.relative_to(data_dir.parent))   # relative to project root

        sf = SourceFile(
            file_path       = rel_path,
            file_name       = path.name,
            source_category = category,
            source_name     = source_name,
            file_size_bytes = path.stat().st_size,
            sha256_checksum = checksum,
        )
        files.append(sf)
        log.debug("Discovered [%s] %s", category, path.name)

    log.info("Discovered %d source files under %s", len(files), data_dir)
    return files
