"""
ingestion/readers/alarm_csv.py
Parser for all CSV files in the Alarms/ directory.

Handles five schemas discriminated by source_category:
  - alarm_history   : Alarm_History_*.csv
  - alarm_status    : Alarm_Status_*.csv
  - event           : Event_History_*.csv
  - incident        : Incident_History_*.csv

Each reader yields dicts for operational_events.
The event fingerprint is computed here to guarantee uniqueness.
"""
from __future__ import annotations

import csv
import hashlib
import logging
from pathlib import Path
from typing import Iterator, Optional

import chardet

from ingestion.normalize import parse_csv_timestamp

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _detect_encoding(path: Path) -> str:
    raw = path.read_bytes()
    result = chardet.detect(raw[:10_000])
    enc = result.get("encoding") or "utf-8"
    # Normalise BOM variant.
    return "utf-8-sig" if enc.lower() in ("utf-8-sig", "utf-8-bom") else enc


def _read_csv(path: Path) -> list[dict]:
    enc = _detect_encoding(path)
    with path.open(encoding=enc, newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    return rows


def _make_fingerprint(*parts: Optional[str]) -> str:
    """SHA-256 of pipe-joined parts.  Strengthened per Correction m3."""
    payload = "|".join(str(p or "") for p in parts)
    return hashlib.sha256(payload.encode()).hexdigest()


def _ts(val: Optional[str]) -> Optional[object]:
    """Parse an IST timestamp string; return UTC datetime or None."""
    if not val or val.strip().lower() in ("invalid", "never", "n/a", ""):
        return None
    return parse_csv_timestamp(val.strip())


# ─────────────────────────────────────────────────────────────────────────────
# schema-specific readers
# ─────────────────────────────────────────────────────────────────────────────

def _read_alarm_history(
    path: Path,
    source_file_id: int,
    source_map: dict,
) -> Iterator[dict]:
    """
    Columns: ID, Priority, Device Name, Alarm Name, Alarm Details,
             Start Time (IST col), Start Time (Device Local),
             End Time (IST col), End Time (Device Local)
    """
    rows = _read_csv(path)
    for line_no, row in enumerate(rows, start=2):
        device    = (row.get("Device Name") or "").strip()
        start_raw = _find_ist_col(row, "Start Time")
        end_raw   = _find_ist_col(row, "End Time")
        src_ev_id = (row.get("ID") or "").strip()
        alarm     = (row.get("Alarm Name") or "").strip()
        priority  = _safe_int(row.get("Priority"))

        fingerprint = _make_fingerprint(
            str(source_file_id), "alarm_history",
            src_ev_id, start_raw,
            str(priority), device, alarm,
        )

        ms_id, code, conf = _resolve_source(device, source_map)

        yield {
            "source_file_id":        source_file_id,
            "event_class":           "alarm_history",
            "source_event_id":       src_ev_id,
            "priority":              priority,
            "device_name":           device,
            "measurement_source_id": ms_id,
            "entwine_asset_code":    code,
            "mapping_confidence":    conf,
            "alarm_name":            alarm,
            "alarm_details":         (row.get("Alarm Details") or "").strip(),
            "start_time_ist":        _ts(start_raw),
            "end_time_ist":          _ts(end_raw),
            "start_time_raw":        start_raw,
            "end_time_raw":          end_raw,
            "raw_payload":           dict(row),
            "row_fingerprint":       fingerprint,
            "__line__":              line_no,
        }


def _read_alarm_status(
    path: Path,
    source_file_id: int,
    source_map: dict,
) -> Iterator[dict]:
    """
    Columns: ID, Priority, Active, Type, Name, Source, Unacknowledged,
             Last/First Occurrence (IST + Device), Last Acknowledged (IST + Device),
             Occurrences
    """
    rows = _read_csv(path)
    for line_no, row in enumerate(rows, start=2):
        source    = (row.get("Source") or "").strip()
        src_ev_id = (row.get("ID") or "").strip()
        name      = (row.get("Name") or "").strip()
        priority  = _safe_int(row.get("Priority"))
        last_occ  = _find_ist_col(row, "Last Occurrence")
        first_occ = _find_ist_col(row, "First Occurrence")

        fingerprint = _make_fingerprint(
            str(source_file_id), "alarm_status",
            src_ev_id, first_occ,
            str(priority), source, name,
        )

        ms_id, code, conf = _resolve_source(source, source_map)

        yield {
            "source_file_id":        source_file_id,
            "event_class":           "alarm_status",
            "source_event_id":       src_ev_id,
            "priority":              priority,
            "device_name":           source,
            "measurement_source_id": ms_id,
            "entwine_asset_code":    code,
            "mapping_confidence":    conf,
            "alarm_name":            name,
            "event_type":            (row.get("Type") or "").strip(),
            "active":                _safe_bool(row.get("Active")),
            "unacknowledged":        _safe_bool(row.get("Unacknowledged")),
            "occurrences":           _safe_int(row.get("Occurrences")),
            "start_time_ist":        _ts(first_occ),
            "end_time_ist":          _ts(last_occ),
            "start_time_raw":        first_occ,
            "end_time_raw":          last_occ,
            "raw_payload":           dict(row),
            "row_fingerprint":       fingerprint,
            "__line__":              line_no,
        }


def _read_event(
    path: Path,
    source_file_id: int,
    source_map: dict,
) -> Iterator[dict]:
    """
    Columns: ID, Priority, Device Name, Event, Condition, Measurement, Value,
             Type, Start Time (IST), Start Time (Device), End Time (IST), End Time (Device)
    """
    rows = _read_csv(path)
    for line_no, row in enumerate(rows, start=2):
        device    = (row.get("Device Name") or "").strip()
        start_raw = _find_ist_col(row, "Start Time")
        end_raw   = _find_ist_col(row, "End Time")
        src_ev_id = (row.get("ID") or "").strip()
        event_name = (row.get("Event") or "").strip()
        priority  = _safe_int(row.get("Priority"))

        fingerprint = _make_fingerprint(
            str(source_file_id), "event",
            src_ev_id, start_raw,
            str(priority), device, event_name,
        )

        ms_id, code, conf = _resolve_source(device, source_map)

        yield {
            "source_file_id":        source_file_id,
            "event_class":           "event",
            "source_event_id":       src_ev_id,
            "priority":              priority,
            "device_name":           device,
            "measurement_source_id": ms_id,
            "entwine_asset_code":    code,
            "mapping_confidence":    conf,
            "alarm_name":            event_name,
            "condition_text":        (row.get("Condition") or "").strip(),
            "measurement":           (row.get("Measurement") or "").strip(),
            "measurement_value":     (row.get("Value") or "").strip(),
            "event_sub_type":        (row.get("Type") or "").strip(),
            "start_time_ist":        _ts(start_raw),
            "end_time_ist":          _ts(end_raw),
            "start_time_raw":        start_raw,
            "end_time_raw":          end_raw,
            "raw_payload":           dict(row),
            "row_fingerprint":       fingerprint,
            "__line__":              line_no,
        }


def _read_incident(
    path: Path,
    source_file_id: int,
    source_map: dict,
) -> Iterator[dict]:
    """
    Columns: ID, Priority, Device Name, Alarm Name,
             Start Time (IST), Start Time (Device), End Time (IST), End Time (Device)
    Note: Device Name may be a comma-separated list of multiple sources.
    """
    rows = _read_csv(path)
    for line_no, row in enumerate(rows, start=2):
        device_raw = (row.get("Device Name") or "").strip()
        start_raw  = _find_ist_col(row, "Start Time")
        end_raw    = _find_ist_col(row, "End Time")
        src_ev_id  = (row.get("ID") or "").strip()
        alarm      = (row.get("Alarm Name") or "").strip()
        priority   = _safe_int(row.get("Priority"))

        # Handle comma-separated multi-device names.
        devices = [d.strip() for d in device_raw.split(",") if d.strip()]
        # Use primary device for fingerprint; store full string in device_name.
        primary_device = devices[0] if devices else ""

        fingerprint = _make_fingerprint(
            str(source_file_id), "incident",
            src_ev_id, start_raw,
            str(priority), device_raw, alarm,
        )

        ms_id, code, conf = _resolve_source(primary_device, source_map)

        yield {
            "source_file_id":        source_file_id,
            "event_class":           "incident",
            "source_event_id":       src_ev_id,
            "priority":              priority,
            "device_name":           device_raw,       # preserved as-is (may be multi)
            "measurement_source_id": ms_id,
            "entwine_asset_code":    code,
            "mapping_confidence":    conf,
            "alarm_name":            alarm,
            "start_time_ist":        _ts(start_raw),
            "end_time_ist":          _ts(end_raw),
            "start_time_raw":        start_raw,
            "end_time_raw":          end_raw,
            "raw_payload":           dict(row),
            "row_fingerprint":       fingerprint,
            "__line__":              line_no,
        }


# ─────────────────────────────────────────────────────────────────────────────
# dispatch
# ─────────────────────────────────────────────────────────────────────────────

def read_alarm_csv(
    path: Path,
    source_category: str,
    source_file_id: int,
    source_map: dict,        # {source_name: row} from mapping.py
) -> Iterator[dict]:
    """Dispatch to the correct reader based on source_category.

    Yields operational_events dicts (or __rejected__ dicts).
    """
    if source_category == "alarm_history":
        yield from _read_alarm_history(path, source_file_id, source_map)
    elif source_category == "alarm_status":
        yield from _read_alarm_status(path, source_file_id, source_map)
    elif source_category == "event":
        yield from _read_event(path, source_file_id, source_map)
    elif source_category == "incident":
        yield from _read_incident(path, source_file_id, source_map)
    else:
        log.error("read_alarm_csv called with unexpected category: %s", source_category)


# ─────────────────────────────────────────────────────────────────────────────
# private utilities
# ─────────────────────────────────────────────────────────────────────────────

def _find_ist_col(row: dict, prefix: str) -> Optional[str]:
    """Return the value of the first column whose name starts with prefix
    and contains 'India' or 'IST', or falls back to the first matching column.
    """
    candidates = [k for k in row if k.startswith(prefix)]
    ist_candidates = [k for k in candidates if "india" in k.lower() or "ist" in k.lower()]
    key = ist_candidates[0] if ist_candidates else (candidates[0] if candidates else None)
    return row[key].strip() if key else None


def _safe_int(val: Optional[str]) -> Optional[int]:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return None


def _safe_bool(val: Optional[str]) -> Optional[bool]:
    if val is None:
        return None
    v = str(val).strip().lower()
    if v in ("true", "yes", "1"):
        return True
    if v in ("false", "no", "0"):
        return False
    return None


def _resolve_source(
    device_name: str,
    source_map: dict,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Look up measurement_source_id, entwine_asset_code, and confidence
    for a device name from measurement_sources (keyed by source_name).
    Returns (None, None, None) if not found.
    """
    row = source_map.get(device_name)
    if row is None:
        return None, None, None
    return (
        row.get("measurement_source_id"),
        row.get("entwine_asset_code"),
        row.get("confidence"),
    )
