"""Initialize the ENTWINE asset registry schema in PostgreSQL/TimescaleDB.

This module provides an idempotent, production-ready migration utility for
executing the SQL schema required by Module 1 (Asset Registry).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
SCHEMA_FILE: Final[Path] = Path(__file__).resolve().parent / "asset_registry_schema.sql"
LOG_FILE: Final[Path] = PROJECT_ROOT / "logs" / "init_registry.log"


class RegistryInitializationError(RuntimeError):
    """Raised when registry initialization cannot be completed safely."""


@dataclass(frozen=True)
class DatabaseSettings:
    """Strongly-typed database configuration loaded from environment variables."""

    host: str
    port: int
    name: str
    user: str
    password: str
    sslmode: str
    pool_size: int
    max_overflow: int
    pool_timeout: int
    pool_recycle: int

    @property
    def sqlalchemy_url(self) -> str:
        """Return a SQLAlchemy-compatible PostgreSQL connection URL."""
        return (
            "postgresql+psycopg2://"
            f"{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
            f"?sslmode={self.sslmode}"
        )


def configure_logging() -> None:
    """Configure structured application logging for console and file outputs."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def get_required_env(name: str) -> str:
    """Return a required environment variable or raise a descriptive error."""
    value: str | None = os.getenv(name)
    if value is None or value.strip() == "":
        raise RegistryInitializationError(
            f"Missing required environment variable: {name}"
        )
    return value.strip()


def load_settings() -> DatabaseSettings:
    """Load and validate database settings from the .env file and environment."""
    load_dotenv(PROJECT_ROOT / ".env")

    try:
        return DatabaseSettings(
            host=get_required_env("DB_HOST"),
            port=int(os.getenv("DB_PORT", "5432")),
            name=get_required_env("DB_NAME"),
            user=get_required_env("DB_USER"),
            password=get_required_env("DB_PASSWORD"),
            sslmode=os.getenv("DB_SSLMODE", "prefer"),
            pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
            max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
            pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
            pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
        )
    except ValueError as exc:
        raise RegistryInitializationError(
            "Environment variables contain invalid integer values "
            "for one or more pool or port settings."
        ) from exc


def create_db_engine(settings: DatabaseSettings) -> Engine:
    """Create a pooled SQLAlchemy engine with production-safe defaults."""
    return create_engine(
        settings.sqlalchemy_url,
        poolclass=QueuePool,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout,
        pool_recycle=settings.pool_recycle,
        pool_pre_ping=True,
        future=True,
    )


def execute_schema(engine: Engine, schema_path: Path) -> None:
    """Execute the schema SQL file in a single transactional unit of work."""
    if not schema_path.exists():
        raise RegistryInitializationError(
            f"Schema file not found: {schema_path}"
        )

    schema_sql: str = schema_path.read_text(encoding="utf-8")
    if schema_sql.strip() == "":
        raise RegistryInitializationError(
            "Schema file is empty; aborting initialization."
        )

    try:
        with engine.begin() as connection:
            raw_connection = connection.connection
            with raw_connection.cursor() as cursor:
                cursor.execute(schema_sql)
    except Exception as exc:
        raise RegistryInitializationError(
            "Database initialization failed while executing schema SQL."
        ) from exc


def main() -> int:
    """Run the registry initialization workflow and return a process exit code."""
    configure_logging()
    logger = logging.getLogger("registry.init_registry")

    engine: Engine | None = None
    try:
        settings: DatabaseSettings = load_settings()
        engine = create_db_engine(settings)

        logger.info("Starting registry schema initialization.")
        execute_schema(engine, SCHEMA_FILE)
        logger.info("Registry schema initialized successfully.")
        return 0
    except RegistryInitializationError as exc:
        logger.exception("Registry initialization error: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unexpected fatal error: %s", exc)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
