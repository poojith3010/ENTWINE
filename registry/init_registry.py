"""Initialize the ENTWINE asset registry — schema then seed.

This script is idempotent:
  - schema.sql uses CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
  - seed.sql uses ON CONFLICT … DO UPDATE

Running this on an already-initialized database is safe.

Usage
-----
    python registry/init_registry.py

Environment variables (via .env or shell)
-----------------------------------------
    DB_HOST      (required)
    DB_NAME      (required)
    DB_USER      (required)
    DB_PASSWORD  (required)
    DB_PORT      (default: 5432)
    DB_SSLMODE   (default: prefer)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
REGISTRY_DIR: Final[Path] = Path(__file__).resolve().parent

SCHEMA_FILE: Final[Path] = REGISTRY_DIR / "schema.sql"
SEED_FILE:   Final[Path] = REGISTRY_DIR / "seed.sql"
LOG_FILE:    Final[Path] = PROJECT_ROOT / "logs" / "init_registry.log"


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class RegistryInitializationError(RuntimeError):
    """Raised when registry initialization cannot be completed safely."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    """Configure structured application logging to console and file."""
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


def execute_sql_file(engine: Engine, sql_path: Path, label: str) -> None:
    """Execute a SQL file inside a single transaction.

    Uses the raw psycopg2 cursor so that multi-statement SQL files
    (including DDL + DML in the same file) are handled correctly by
    the driver without needing statement splitting in Python.
    """
    logger = logging.getLogger("registry.init_registry")

    if not sql_path.exists():
        raise RegistryInitializationError(
            f"SQL file not found: {sql_path}"
        )

    sql_text: str = sql_path.read_text(encoding="utf-8").strip()
    if not sql_text:
        raise RegistryInitializationError(
            f"SQL file is empty: {sql_path}"
        )

    logger.info("Executing %s (%s) …", label, sql_path.name)
    try:
        with engine.begin() as conn:
            # Use the underlying psycopg2 cursor to execute the whole
            # file as one block — this preserves BEGIN/COMMIT semantics
            # and avoids splitting on semicolons in comments or strings.
            raw_conn = conn.connection
            with raw_conn.cursor() as cur:
                cur.execute(sql_text)
        logger.info("%s executed successfully.", label)
    except Exception as exc:
        raise RegistryInitializationError(
            f"Database error while executing {label}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the registry initialisation workflow and return a process exit code."""
    configure_logging()
    logger = logging.getLogger("registry.init_registry")

    engine: Engine | None = None
    try:
        settings: DatabaseSettings = load_settings()
        engine = create_db_engine(settings)

        logger.info("=== ENTWINE Asset Registry Initialization ===")
        execute_sql_file(engine, SCHEMA_FILE, "schema.sql")
        execute_sql_file(engine, SEED_FILE,   "seed.sql")
        logger.info("=== Registry initialization complete. ===")
        return 0

    except RegistryInitializationError as exc:
        logging.getLogger("registry.init_registry").error(
            "Initialization error: %s", exc
        )
        return 1
    except Exception as exc:  # pragma: no cover — defensive guard
        logging.getLogger("registry.init_registry").exception(
            "Unexpected fatal error: %s", exc
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
