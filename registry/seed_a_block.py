"""Idempotent seed script to register A Block in the ENTWINE Asset Registry."""

import logging
import os
from typing import Final

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("registry.seed_a_block")


def get_database_engine() -> Engine:
    """Load credentials from .env and return a SQLAlchemy Engine."""
    load_dotenv()
    db_user: str = os.getenv("DB_USER", "postgres")
    db_password: str = os.getenv("DB_PASSWORD", "")
    db_host: str = os.getenv("DB_HOST", "localhost")
    db_port: str = os.getenv("DB_PORT", "5432")
    db_name: str = os.getenv("DB_NAME", "entwine_db")
    
    url = f"postgresql+psycopg2://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    return create_engine(url, pool_pre_ping=True)


def seed_a_block(engine: Engine) -> None:
    """Insert or update A Block metadata using the repository schema."""
    seed_sql: Final[str] = """
    -- 1. Insert A Block Building
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
    SET building_name = EXCLUDED.building_name,
        updated_at = NOW();

    -- 2. Insert A Block Main Meter
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
    SET is_active = EXCLUDED.is_active,
        updated_at = NOW();
    """
    
    try:
        with engine.begin() as conn:
            conn.execute(text(seed_sql))
        logger.info("Successfully registered 'Powerhouse A Block' (PH-A-BLK) and meter 'PH-A-MAIN'.")
    except SQLAlchemyError as exc:
        logger.error("Database error during A Block registration: %s", exc)
        raise


if __name__ == "__main__":
    engine = get_database_engine()
    seed_a_block(engine)