"""
ingestion/config.py
Database connection settings loaded from the project .env file.
All Module 2 pipeline modules import DatabaseSettings from here.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve .env relative to the project root (one level above this package).
_PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=False)

# ── connection string ─────────────────────────────────────────────────────────

def get_dsn() -> str:
    """Return a psycopg2-compatible DSN assembled from .env variables.

    Expected .env keys (matches Module 1 and Docker Compose):
        DB_HOST (or POSTGRES_HOST), DB_PORT (or POSTGRES_PORT),
        DB_NAME (or POSTGRES_DB), DB_USER (or POSTGRES_USER),
        DB_PASSWORD (or POSTGRES_PASSWORD)
    """
    host   = os.environ.get("DB_HOST") or os.environ.get("POSTGRES_HOST") or "localhost"
    port   = os.environ.get("DB_PORT") or os.environ.get("POSTGRES_PORT") or "5432"
    db     = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB") or "entwine_twin"
    user   = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER") or "entwine_admin"
    pw     = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD") or "change_me_now"
    return f"host={host} port={port} dbname={db} user={user} password={pw}"


# ── project paths ─────────────────────────────────────────────────────────────

PROJECT_ROOT          = _PROJECT_ROOT
DATA_DIR              = PROJECT_ROOT / "real time energy data"
REGISTRY_DIR          = PROJECT_ROOT / "registry"
MIGRATIONS_DIR        = PROJECT_ROOT / "migrations"
LOGS_DIR              = PROJECT_ROOT / "logs"
ASSET_MAPPING_CSV     = REGISTRY_DIR / "asset_mapping.csv"

# ── ingestion tuning ──────────────────────────────────────────────────────────

BATCH_SIZE         = 1_000   # rows per DB transaction
LOG_PROGRESS_EVERY = 10_000  # rows between progress log lines

# ── approval provenance (written into mapping_snapshots) ─────────────────────

APPROVAL_REFERENCE = (
    "User approval 2026-08-26 via ENTWINE Module 2 planning session "
    "(strong_inference sources approved for ingestion)"
)
