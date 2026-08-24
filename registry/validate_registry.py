"""ENTWINE Module 1 — Asset Registry Validation Script

Connects to PostgreSQL, runs a series of checks, and prints a
PASS / FAIL summary.  Run this after init_registry.py to confirm
that the Asset Registry is correctly initialised.

Usage
-----
    python registry/validate_registry.py

Exit codes
----------
    0 — all checks passed
    1 — one or more checks failed
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}[PASS]{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def build_engine() -> Engine:
    url = (
        "postgresql+psycopg2://"
        f"{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
        f"@{os.environ.get('DB_HOST', 'localhost')}:{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ['DB_NAME']}"
        f"?sslmode={os.environ.get('DB_SSLMODE', 'prefer')}"
    )
    return create_engine(url, future=True)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_connection(engine: Engine) -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        ok("Database connection")
        return True
    except Exception as exc:
        fail(f"Database connection — {exc}")
        return False


def check_tables(engine: Engine) -> bool:
    required = {"buildings", "meters", "equipment", "baseline_parameters"}
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    passed = True
    for tbl in sorted(required):
        if tbl in existing:
            ok(f"Table '{tbl}' exists")
        else:
            fail(f"Table '{tbl}' MISSING")
            passed = False
    return passed


def check_columns(engine: Engine) -> bool:
    """Spot-check critical mentor-required columns."""
    checks = {
        "buildings": [
            "building_id", "building_code", "building_name",
            "floor_area_sqm", "floor_count", "occupancy_type",
            "typical_occupancy", "year_commissioned", "notes",
        ],
        "meters": [
            "meter_id", "meter_code", "building_id", "parent_meter_id",
            "meter_type", "rated_capacity_kw", "protocol",
            "sampling_interval_seconds", "install_date", "is_active",
        ],
        "equipment": [
            "equipment_id", "building_id", "meter_id",
            "equipment_type", "equipment_name", "rated_power_kw",
            "typical_duty_cycle", "install_date", "is_active", "notes",
        ],
        "baseline_parameters": [
            "baseline_id", "building_id", "parameter_name",
            "parameter_value", "unit", "valid_from", "valid_to", "source",
        ],
    }
    insp = inspect(engine)
    passed = True
    for table, cols in checks.items():
        try:
            existing_cols = {c["name"] for c in insp.get_columns(table)}
        except Exception:
            fail(f"Could not inspect columns for '{table}'")
            passed = False
            continue
        for col in cols:
            if col in existing_cols:
                ok(f"  Column '{table}.{col}' exists")
            else:
                fail(f"  Column '{table}.{col}' MISSING")
                passed = False
    return passed


def check_indexes(engine: Engine) -> bool:
    required_indexes = {
        "meters":               ["idx_meters_building", "idx_meters_parent"],
        "equipment":            ["idx_equipment_building"],
        "baseline_parameters":  ["idx_baseline_building"],
    }
    insp = inspect(engine)
    passed = True
    for table, idx_names in required_indexes.items():
        try:
            existing = {i["name"] for i in insp.get_indexes(table)}
        except Exception:
            fail(f"Could not inspect indexes for '{table}'")
            passed = False
            continue
        for idx in idx_names:
            if idx in existing:
                ok(f"Index '{idx}' on '{table}' exists")
            else:
                fail(f"Index '{idx}' on '{table}' MISSING")
                passed = False
    return passed


def check_view(engine: Engine) -> bool:
    try:
        with engine.connect() as c:
            result = c.execute(text("SELECT * FROM twin_instances LIMIT 0"))
            cols = set(result.keys())
        required_cols = {
            "building_id", "building_code", "building_name",
            "occupancy_type", "floor_area_sqm",
            "meter_id", "meter_code", "meter_type",
            "protocol", "meter_active",
        }
        missing = required_cols - cols
        if missing:
            fail(f"twin_instances view missing columns: {missing}")
            return False
        ok("twin_instances view exists with correct columns")
        return True
    except Exception as exc:
        fail(f"twin_instances view — {exc}")
        return False


def check_seed_building(engine: Engine) -> bool:
    passed = True
    with engine.connect() as c:
        row = c.execute(
            text("SELECT building_code, building_name, occupancy_type, floor_area_sqm, floor_count "
                 "FROM buildings WHERE building_code = 'PH-01'")
        ).mappings().first()

    if row is None:
        fail("PH-01 building record NOT FOUND")
        return False

    checks = {
        "building_code":  ("PH-01",                  row["building_code"]),
        "building_name":  ("Main Powerhouse Block",   row["building_name"]),
        "occupancy_type": ("utility",                 row["occupancy_type"]),
        "floor_area_sqm": (2400.0,  float(row["floor_area_sqm"])),
        "floor_count":    (2,       int(row["floor_count"])),
    }
    for field, (expected, actual) in checks.items():
        if actual == expected:
            ok(f"buildings.{field} = {expected!r}")
        else:
            fail(f"buildings.{field}: expected {expected!r}, got {actual!r}")
            passed = False
    return passed


def check_seed_meter(engine: Engine) -> bool:
    passed = True
    with engine.connect() as c:
        row = c.execute(
            text("SELECT meter_code, meter_type, protocol, sampling_interval_seconds, is_active "
                 "FROM meters WHERE meter_code = 'PH-01-MAIN'")
        ).mappings().first()

    if row is None:
        fail("PH-01-MAIN meter record NOT FOUND")
        return False

    checks = {
        "meter_code":                ("PH-01-MAIN",     row["meter_code"]),
        "meter_type":                ("main",            row["meter_type"]),
        "protocol":                  ("manual_export",   row["protocol"]),
        "sampling_interval_seconds": (900,               row["sampling_interval_seconds"]),
        "is_active":                 (True,              row["is_active"]),
    }
    for field, (expected, actual) in checks.items():
        if actual == expected:
            ok(f"meters.{field} = {expected!r}")
        else:
            fail(f"meters.{field}: expected {expected!r}, got {actual!r}")
            passed = False
    return passed


def check_twin_instances_content(engine: Engine) -> bool:
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT building_code, meter_code, meter_type, protocol, meter_active "
                 "FROM twin_instances WHERE building_code = 'PH-01'")
        ).mappings().all()

    if not rows:
        fail("twin_instances returned 0 rows for PH-01")
        return False

    row = rows[0]
    expected = {
        "building_code": "PH-01",
        "meter_code":    "PH-01-MAIN",
        "meter_type":    "main",
        "protocol":      "manual_export",
        "meter_active":  True,
    }
    passed = True
    for field, exp_val in expected.items():
        actual = row[field]
        if actual == exp_val:
            ok(f"twin_instances.{field} = {exp_val!r}")
        else:
            fail(f"twin_instances.{field}: expected {exp_val!r}, got {actual!r}")
            passed = False
    return passed


def check_no_fabricated_baseline(engine: Engine) -> bool:
    with engine.connect() as c:
        count = c.execute(text("SELECT COUNT(*) FROM baseline_parameters")).scalar()
    if count == 0:
        ok("baseline_parameters is empty (correct — no fabricated values)")
        return True
    warn(f"baseline_parameters contains {count} row(s). Verify source before review.")
    return True  # warn only; don't fail if the team has added real values


def check_no_fabricated_equipment_kw(engine: Engine) -> bool:
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT equipment_name, rated_power_kw FROM equipment "
                 "WHERE rated_power_kw IS NOT NULL")
        ).mappings().all()
    if not rows:
        ok("equipment.rated_power_kw — no unverified values present")
        return True
    warn(f"{len(rows)} equipment row(s) have rated_power_kw set — verify source:")
    for r in rows:
        warn(f"  {r['equipment_name']}: {r['rated_power_kw']} kW")
    return True  # warn only; team may have confirmed values later


def check_foreign_keys(engine: Engine) -> bool:
    """Validate FK consistency by running referential checks."""
    passed = True
    queries = [
        (
            "meters.building_id → buildings.building_id",
            "SELECT COUNT(*) FROM meters m "
            "LEFT JOIN buildings b ON b.building_id = m.building_id "
            "WHERE b.building_id IS NULL",
        ),
        (
            "meters.parent_meter_id → meters.meter_id",
            "SELECT COUNT(*) FROM meters m "
            "LEFT JOIN meters p ON p.meter_id = m.parent_meter_id "
            "WHERE m.parent_meter_id IS NOT NULL AND p.meter_id IS NULL",
        ),
        (
            "equipment.building_id → buildings.building_id",
            "SELECT COUNT(*) FROM equipment e "
            "LEFT JOIN buildings b ON b.building_id = e.building_id "
            "WHERE b.building_id IS NULL",
        ),
        (
            "baseline_parameters.building_id → buildings.building_id",
            "SELECT COUNT(*) FROM baseline_parameters bp "
            "LEFT JOIN buildings b ON b.building_id = bp.building_id "
            "WHERE b.building_id IS NULL",
        ),
    ]
    with engine.connect() as c:
        for label, q in queries:
            orphans = c.execute(text(q)).scalar()
            if orphans == 0:
                ok(f"FK: {label}")
            else:
                fail(f"FK violation: {label} — {orphans} orphan row(s)")
                passed = False
    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print()
    print("=" * 60)
    print("  ENTWINE — Module 1 Asset Registry Validation")
    print("=" * 60)
    print()

    try:
        engine = build_engine()
    except KeyError as exc:
        fail(f"Missing environment variable: {exc}")
        return 1

    results: list[bool] = []

    print("── Connectivity ──────────────────────────────────────────")
    results.append(check_connection(engine))

    if not results[-1]:
        print()
        print(f"{RED}Cannot reach the database — aborting remaining checks.{RESET}")
        return 1

    print()
    print("── Tables ────────────────────────────────────────────────")
    results.append(check_tables(engine))

    print()
    print("── Columns ───────────────────────────────────────────────")
    results.append(check_columns(engine))

    print()
    print("── Indexes ───────────────────────────────────────────────")
    results.append(check_indexes(engine))

    print()
    print("── Views ─────────────────────────────────────────────────")
    results.append(check_view(engine))

    print()
    print("── PH-01 Building Seed ───────────────────────────────────")
    results.append(check_seed_building(engine))

    print()
    print("── PH-01-MAIN Meter Seed ─────────────────────────────────")
    results.append(check_seed_meter(engine))

    print()
    print("── twin_instances Content ────────────────────────────────")
    results.append(check_twin_instances_content(engine))

    print()
    print("── Data Integrity ────────────────────────────────────────")
    results.append(check_foreign_keys(engine))
    check_no_fabricated_baseline(engine)
    check_no_fabricated_equipment_kw(engine)

    engine.dispose()

    print()
    print("=" * 60)
    if all(results):
        print(f"  {GREEN}MODULE 1: PASS{RESET}")
    else:
        failed = results.count(False)
        print(f"  {RED}MODULE 1: FAIL — {failed} check(s) did not pass{RESET}")
    print("=" * 60)
    print()

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
