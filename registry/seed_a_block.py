"""Idempotent seed script to register A Block in the ENTWINE Asset Registry.

Usage
-----
    python registry/seed_a_block.py           # upsert (safe to re-run)
    python registry/seed_a_block.py --reset   # drop & re-insert Block A rows
"""

import argparse
import logging
import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

# Resolve .env from project root (one level above this script's directory)
_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_ENV_PATH: Final[Path] = _PROJECT_ROOT / ".env"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("registry.seed_a_block")


def get_database_engine() -> Engine:
    """Load credentials from project-root .env and return a SQLAlchemy Engine."""
    load_dotenv(dotenv_path=_ENV_PATH, override=False)
    db_user     = os.getenv("DB_USER",     "entwine_admin")
    db_password = os.getenv("DB_PASSWORD", "change_me_now")
    db_host     = os.getenv("DB_HOST",     "localhost")
    db_port     = os.getenv("DB_PORT",     "5432")
    db_name     = os.getenv("DB_NAME",     "entwine_twin")
    url = (
        f"postgresql+psycopg2://{db_user}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}"
    )
    return create_engine(url, pool_pre_ping=True)


def reset_a_block(engine: Engine) -> None:
    """Remove all Block A-specific rows to allow a clean re-seed."""
    reset_sql = """
    -- Delete meters belonging to PH-A-BLK (FK cascade prevents orphans)
    DELETE FROM meters
    WHERE meter_code = 'PH-A-MAIN';

    -- Delete the A Block building record
    DELETE FROM buildings
    WHERE building_code = 'PH-A-BLK';
    """
    with engine.begin() as conn:
        conn.execute(text(reset_sql))
    logger.info("Block A registry rows removed (reset complete).")


def seed_a_block(engine: Engine) -> None:
    """Insert or update A Block metadata using the repository schema."""
    seed_sql: Final[str] = """
    -- 1. Insert A Block Building (idempotent upsert)
    INSERT INTO buildings (
        building_code,
        building_name,
        occupancy_type,
        floor_area_sqm,
        floor_count
    )
    VALUES (
        'PH-A-BLK',
        'Powerhouse A Block',
        'utility',
        1800.0,
        2
    )
    ON CONFLICT (building_code) DO UPDATE
    SET building_name  = EXCLUDED.building_name,
        occupancy_type = EXCLUDED.occupancy_type,
        floor_area_sqm = EXCLUDED.floor_area_sqm,
        floor_count    = EXCLUDED.floor_count,
        updated_at     = NOW();

    -- 2. Insert A Block Main Meter (idempotent upsert)
    INSERT INTO meters (
        building_id,
        meter_code,
        meter_type,
        protocol,
        sampling_interval_seconds,
        is_active
    )
    SELECT
        b.building_id,
        'PH-A-MAIN',
        'main',
        'manual_export',
        900,
        TRUE
    FROM buildings b
    WHERE b.building_code = 'PH-A-BLK'
    ON CONFLICT (meter_code) DO UPDATE
    SET is_active  = EXCLUDED.is_active,
        updated_at = NOW();
    """

    try:
        with engine.begin() as conn:
            conn.execute(text(seed_sql))
        logger.info(
            "Successfully registered 'Powerhouse A Block' (PH-A-BLK) "
            "and meter 'PH-A-MAIN'."
        )
    except SQLAlchemyError as exc:
        logger.error("Database error during A Block registration: %s", exc)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed Block A into the ENTWINE asset registry."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Remove existing Block A rows before re-seeding (full clean reinit).",
    )
    args = parser.parse_args()

    db_engine = get_database_engine()
    if args.reset:
        reset_a_block(db_engine)
    seed_a_block(db_engine)