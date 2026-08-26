"""
ingestion/register_assets.py
Mapping-driven registry registrar for Module 2.

This command is the ONLY Module 2 component that writes to Module 1 tables
(meters, equipment).  It must be invoked separately from the telemetry
ingestion pipeline.

Behaviour per mapping row
--------------------------
meter_candidate (with non-TBD code):
    1. Resolve parent_asset code → meter_id FK (fail if parent not found).
    2. INSERT into meters if absent (ON CONFLICT DO NOTHING).
    3. If the row already exists, verify key columns; report any conflict as a
       WARNING — never silently update Module 1 data.
    4. INSERT into measurement_sources pointing to meter_id.

equipment_candidate / switchgear (with non-TBD code):
    1. Resolve building_code → building_id.
    2. INSERT into equipment if absent.
    3. Verify if already present; report conflicts.
    4. INSERT into measurement_sources pointing to equipment_id.

feeder / TBD:
    1. INSERT into measurement_sources with both FKs NULL,
       resolution_status = 'quarantined'.
    2. No meter or equipment record created.

building (already in Module 1):
    1. Skip — buildings are seeded separately.

After processing all rows, writes one mapping_snapshot row per mapping entry
to preserve approval provenance.

Usage
-----
    python -m ingestion.run --register-assets
    # or directly:
    python -m ingestion.register_assets
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

import psycopg2
import psycopg2.extras

from ingestion.config import APPROVAL_REFERENCE, get_dsn
from ingestion import mapping as mapping_mod

log = logging.getLogger(__name__)

# Asset types handled by this registrar.
_METER_TYPES    = {"meter_candidate"}
_EQUIP_TYPES    = {"equipment_candidate", "switchgear"}
_SKIP_TYPES     = {"building"}
_QUARANTINE     = {"feeder"}          # also catches TBD codes regardless of type

# Approval reference timestamp (the planning session date).
_APPROVED_AT = datetime.datetime(2026, 8, 26, 14, 47, 0,
                                 tzinfo=datetime.timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_building_id(cur, building_code: str) -> int:
    cur.execute(
        "SELECT building_id FROM buildings WHERE building_code = %s",
        (building_code,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(
            f"building_code '{building_code}' not found in buildings table. "
            "Run Module 1 initialisation before register_assets."
        )
    return row[0]


def _resolve_parent_meter_id(cur, parent_code: str) -> int | None:
    """Return meter_id for parent_code, or None if parent_code is empty."""
    if not parent_code or not parent_code.strip():
        return None
    cur.execute(
        "SELECT meter_id FROM meters WHERE meter_code = %s",
        (parent_code,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(
            f"parent_asset code '{parent_code}' not found in meters table. "
            "The parent meter must exist before its children are registered."
        )
    return row[0]


def _verify_meter(cur, meter_code: str, expected: dict) -> list[str]:
    """Compare existing meter row against expected values.
    Returns a list of conflict description strings (empty = no conflicts).
    """
    cur.execute(
        "SELECT meter_type, protocol, sampling_interval_seconds "
        "FROM meters WHERE meter_code = %s",
        (meter_code,),
    )
    row = cur.fetchone()
    if row is None:
        return []
    conflicts = []
    actual = {"meter_type": row[0], "protocol": row[1],
              "sampling_interval_seconds": row[2]}
    for key, exp_val in expected.items():
        if key in actual and actual[key] != exp_val and exp_val is not None:
            conflicts.append(
                f"  {key}: expected={exp_val!r}, found={actual[key]!r}"
            )
    return conflicts


def _verify_equipment(cur, equipment_name: str, expected: dict) -> list[str]:
    """Compare existing equipment row against expected values."""
    cur.execute(
        "SELECT equipment_type, is_active FROM equipment "
        "WHERE equipment_name = %s",
        (equipment_name,),
    )
    row = cur.fetchone()
    if row is None:
        return []
    conflicts = []
    actual = {"equipment_type": row[0], "is_active": row[1]}
    for key, exp_val in expected.items():
        if key in actual and actual[key] != exp_val and exp_val is not None:
            conflicts.append(
                f"  {key}: expected={exp_val!r}, found={actual[key]!r}"
            )
    return conflicts


def _upsert_measurement_source(cur, row: dict,
                                meter_id: int | None,
                                equipment_id: int | None,
                                resolution_status: str) -> int:
    """Insert measurement_sources row; return measurement_source_id."""
    cur.execute(
        """
        INSERT INTO measurement_sources
            (source_name, entwine_asset_code, asset_type, meter_id,
             equipment_id, mapping_confidence, approval_status,
             resolution_status, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_name) DO UPDATE
            SET resolution_status = EXCLUDED.resolution_status,
                meter_id          = COALESCE(measurement_sources.meter_id,
                                             EXCLUDED.meter_id),
                equipment_id      = COALESCE(measurement_sources.equipment_id,
                                             EXCLUDED.equipment_id),
                approval_status   = EXCLUDED.approval_status
        RETURNING measurement_source_id
        """,
        (
            row["source_name"],
            row["entwine_asset_code"],
            row["asset_type"],
            meter_id,
            equipment_id,
            row.get("confidence", ""),
            "approved" if resolution_status != "quarantined" else "quarantined",
            resolution_status,
            row.get("notes", ""),
        ),
    )
    return cur.fetchone()[0]


def _write_snapshot(cur, row: dict, approval_status: str) -> None:
    """Write one mapping_snapshots row for audit trail."""
    cur.execute(
        """
        INSERT INTO mapping_snapshots
            (snapshot_run_at, approval_reference, approved_at, approval_status,
             source_name, entwine_asset_code, asset_type, building_code,
             meter_type, parent_asset, source_of_truth,
             original_confidence, notes)
        VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_name, snapshot_run_at) DO NOTHING
        """,
        (
            APPROVAL_REFERENCE,
            _APPROVED_AT,
            approval_status,
            row["source_name"],
            row.get("entwine_asset_code", ""),
            row.get("asset_type", ""),
            row.get("building_code", ""),
            row.get("meter_type", ""),
            row.get("parent_asset", ""),
            row.get("source_of_truth", ""),
            row.get("confidence", ""),          # verbatim — never altered
            row.get("notes", ""),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# main registration logic
# ─────────────────────────────────────────────────────────────────────────────

def register_assets(dsn: str | None = None, dry_run: bool = False) -> dict:
    """Read asset_mapping.csv and register approved assets.

    Returns a summary dict with keys:
        meters_created, meters_verified, equipment_created, equipment_verified,
        measurement_sources_created, quarantined, conflicts, skipped, errors
    """
    dsn = dsn or get_dsn()
    rows = mapping_mod.all_rows()

    stats: dict[str, int] = {
        "meters_created":              0,
        "meters_verified":             0,
        "equipment_created":           0,
        "equipment_verified":          0,
        "measurement_sources_created": 0,
        "quarantined":                 0,
        "conflicts":                   0,
        "skipped":                     0,
        "errors":                      0,
    }
    conflict_details: list[str] = []

    conn = psycopg2.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                for row in rows:
                    asset_type = row.get("asset_type", "").strip().lower()
                    code       = row.get("entwine_asset_code", "").strip()
                    src_name   = row["source_name"]
                    is_tbd     = code.upper() in ("TBD", "")

                    # ── snapshot always written ─────────────────────────────
                    if is_tbd or asset_type in _QUARANTINE:
                        snap_status = "quarantined"
                    elif asset_type in _SKIP_TYPES:
                        snap_status = "approved"
                    else:
                        snap_status = "approved"

                    if not dry_run:
                        _write_snapshot(cur, row, snap_status)

                    # ── buildings: skip ─────────────────────────────────────
                    if asset_type in _SKIP_TYPES:
                        log.info("SKIP  %s (building — managed by Module 1)", src_name)
                        stats["skipped"] += 1
                        continue

                    # ── feeders / TBD: quarantine only ─────────────────────
                    if is_tbd or asset_type in _QUARANTINE:
                        log.info(
                            "QUARANTINE  %s (code=%s, type=%s)",
                            src_name, code, asset_type,
                        )
                        if not dry_run:
                            _upsert_measurement_source(
                                cur, row, None, None, "quarantined"
                            )
                        stats["quarantined"] += 1
                        continue

                    # ── meter_candidate ────────────────────────────────────
                    if asset_type in _METER_TYPES:
                        building_id = _resolve_building_id(
                            cur, row.get("building_code", "")
                        )
                        parent_id = _resolve_parent_meter_id(
                            cur, row.get("parent_asset", "")
                        )
                        expected = {
                            "meter_type":               row.get("meter_type") or "sub_panel",
                            "protocol":                 "manual_export",
                            "sampling_interval_seconds": 900,
                        }

                        # Check if meter already exists.
                        cur.execute(
                            "SELECT meter_id FROM meters WHERE meter_code = %s",
                            (code,),
                        )
                        existing = cur.fetchone()

                        if existing is None:
                            log.info("CREATE meter  %s → %s", src_name, code)
                            if not dry_run:
                                cur.execute(
                                    """
                                    INSERT INTO meters
                                        (meter_code, building_id, parent_meter_id,
                                         meter_type, protocol,
                                         sampling_interval_seconds, is_active)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                                    ON CONFLICT (meter_code) DO NOTHING
                                    RETURNING meter_id
                                    """,
                                    (
                                        code, building_id, parent_id,
                                        expected["meter_type"],
                                        expected["protocol"],
                                        expected["sampling_interval_seconds"],
                                        True,
                                    ),
                                )
                                result = cur.fetchone()
                                meter_id = result[0] if result else None
                                if meter_id is None:
                                    # Race condition: inserted by another process.
                                    cur.execute(
                                        "SELECT meter_id FROM meters WHERE meter_code = %s",
                                        (code,),
                                    )
                                    meter_id = cur.fetchone()[0]
                            else:
                                meter_id = None  # dry-run placeholder
                            stats["meters_created"] += 1
                        else:
                            meter_id = existing[0]
                            conflicts = _verify_meter(cur, code, expected)
                            if conflicts:
                                msg = (
                                    f"CONFLICT meter {code} ({src_name}):\n"
                                    + "\n".join(conflicts)
                                )
                                log.warning(msg)
                                conflict_details.append(msg)
                                stats["conflicts"] += 1
                            else:
                                log.info("VERIFIED meter  %s → %s (OK)", src_name, code)
                            stats["meters_verified"] += 1

                        if not dry_run:
                            ms_id = _upsert_measurement_source(
                                cur, row, meter_id, None, "resolved"
                            )
                            stats["measurement_sources_created"] += 1
                        continue

                    # ── equipment_candidate / switchgear ───────────────────
                    if asset_type in _EQUIP_TYPES:
                        building_id = _resolve_building_id(
                            cur, row.get("building_code", "")
                        )
                        equip_type = (
                            "switchgear"
                            if asset_type == "switchgear"
                            else "generator"   # DG_1
                        )
                        equip_name = code      # use entwine_asset_code as name

                        cur.execute(
                            "SELECT equipment_id FROM equipment "
                            "WHERE equipment_name = %s",
                            (equip_name,),
                        )
                        existing = cur.fetchone()

                        if existing is None:
                            log.info(
                                "CREATE equipment  %s → %s (%s)",
                                src_name, code, equip_type,
                            )
                            if not dry_run:
                                cur.execute(
                                    """
                                    INSERT INTO equipment
                                        (building_id, equipment_type, equipment_name,
                                         is_active, notes)
                                    VALUES (%s, %s, %s, %s, %s)
                                    RETURNING equipment_id
                                    """,
                                    (
                                        building_id,
                                        equip_type,
                                        equip_name,
                                        True,
                                        (
                                            f"Added by Module 2 register_assets. "
                                            f"source_name={src_name}. "
                                            f"Original confidence: {row.get('confidence')}. "
                                            + row.get("notes", "")
                                        ),
                                    ),
                                )
                                equipment_id = cur.fetchone()[0]
                            else:
                                equipment_id = None
                            stats["equipment_created"] += 1
                        else:
                            equipment_id = existing[0]
                            conflicts = _verify_equipment(
                                cur, equip_name,
                                {"equipment_type": equip_type, "is_active": True},
                            )
                            if conflicts:
                                msg = (
                                    f"CONFLICT equipment {code} ({src_name}):\n"
                                    + "\n".join(conflicts)
                                )
                                log.warning(msg)
                                conflict_details.append(msg)
                                stats["conflicts"] += 1
                            else:
                                log.info(
                                    "VERIFIED equipment  %s → %s (OK)",
                                    src_name, code,
                                )
                            stats["equipment_verified"] += 1

                        if not dry_run:
                            _upsert_measurement_source(
                                cur, row, None, equipment_id, "resolved"
                            )
                            stats["measurement_sources_created"] += 1
                        continue

                    # ── unknown type ───────────────────────────────────────
                    log.warning(
                        "SKIP  %s — unknown asset_type '%s'",
                        src_name, asset_type,
                    )
                    stats["skipped"] += 1

    finally:
        conn.close()

    # Summary.
    log.info("─" * 60)
    log.info("register_assets summary:")
    for k, v in stats.items():
        if v:
            log.info("  %-32s %d", k, v)
    if conflict_details:
        log.warning("Conflicts detected — review before proceeding:")
        for d in conflict_details:
            log.warning(d)
    log.info("─" * 60)

    return stats


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    dry = "--dry-run" in sys.argv
    result = register_assets(dry_run=dry)
    if result["errors"] > 0:
        sys.exit(1)
