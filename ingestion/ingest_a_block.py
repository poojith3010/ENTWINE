"""Data ingestion pipeline for A Block historical energy time-series.

Parses the XLS report, locates the true data headers, creates the
state_telemetry table if needed, and inserts readings into TimescaleDB
using ON CONFLICT DO NOTHING for full idempotency.

Usage
-----
    python -m ingestion.ingest_a_block            # safe to re-run
    python -m ingestion.ingest_a_block --reset    # truncate state_telemetry first
"""

import argparse
import logging
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

# ── Paths ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

A_BLOCK_XLS = (
    _PROJECT_ROOT
    / "real time energy data"
    / "POWERHOUSE_1"
    / "POWERHOUSE_1.A_BLOCK"
    / "New Tabular Report for report(2).xls"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("ingestion.a_block")


# ── Database helpers ─────────────────────────────────────────────────────────

def get_database_engine() -> Engine:
    """Load credentials from project-root .env and create a SQLAlchemy Engine."""
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


def setup_state_table(engine: Engine) -> None:
    """Ensure state_telemetry exists and is enabled for TimescaleDB."""
    schema_sql = """
    CREATE TABLE IF NOT EXISTS state_telemetry (
        time             TIMESTAMPTZ NOT NULL,
        meter_id         BIGINT      NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
        parameter_name   VARCHAR(128) NOT NULL,
        reading_value    NUMERIC(14, 6),
        PRIMARY KEY (time, meter_id, parameter_name)
    );
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
            PERFORM create_hypertable('state_telemetry', 'time', if_not_exists => TRUE);
        END IF;
    EXCEPTION
        WHEN OTHERS THEN NULL;
    END $$;
    """
    with engine.begin() as conn:
        conn.execute(text(schema_sql))
    logger.info("Ensured 'state_telemetry' table is ready.")


def reset_block_a_telemetry(engine: Engine, meter_id: int) -> None:
    """Remove all telemetry rows for PH-A-MAIN to allow a clean re-ingest."""
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM state_telemetry WHERE meter_id = :mid"),
            {"mid": meter_id},
        )
    logger.info("Deleted existing state_telemetry rows for meter_id=%d.", meter_id)


def get_meter_id(engine: Engine, meter_code: str) -> int:
    """Retrieve the primary key meter_id for a given meter code."""
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT meter_id FROM meters WHERE meter_code = :code"),
            {"code": meter_code}
        )
        row = result.fetchone()
        if not row:
            raise ValueError(
                f"Meter '{meter_code}' not found in registry. "
                "Run 'python registry/seed_a_block.py' first."
            )
        return int(row[0])


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_excel(file_path: Path) -> pd.DataFrame:
    """Parse the A Block XLS report into a melted long-format DataFrame.

    Returns columns: time (UTC TIMESTAMPTZ), parameter_name (str),
    reading_value (float or NaN).
    """
    if not file_path.exists():
        raise FileNotFoundError(f"A Block XLS not found: {file_path}")

    logger.info("Reading raw file: %s", file_path)
    df_raw = pd.read_excel(file_path, header=None)

    # 1. Locate the true header row (contains "Timestamp")
    header_idx = -1
    for i, row in df_raw.iterrows():
        if "timestamp" in " ".join(str(v).lower() for v in row.values):
            header_idx = int(i)
            break

    if header_idx == -1:
        raise ValueError("Could not find 'Timestamp' header row in the XLS file.")

    logger.info("Header found at row %d. Loading data...", header_idx)
    df = pd.read_excel(file_path, header=header_idx)

    # 2. Drop entirely empty columns (formatting noise)
    df.dropna(axis=1, how="all", inplace=True)

    # 3. Clean column names
    clean_cols = []
    for col in df.columns:
        c = (
            str(col)
            .replace("\n", " ")
            .replace("POWERHOUSE_1.A_BLOCK", "")
            .strip()
        )
        c = " ".join(c.split())
        clean_cols.append(c)
    df.columns = clean_cols

    # 4. Parse and localize Timestamp column to UTC
    time_col = next(c for c in df.columns if "timestamp" in c.lower())
    df.rename(columns={time_col: "time"}, inplace=True)
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
    df.dropna(subset=["time"], inplace=True)

    # 5. Melt to long format: (time, parameter_name, reading_value)
    df_melted = df.melt(
        id_vars=["time"],
        var_name="parameter_name",
        value_name="reading_value",
    )

    # 6. Coerce to numeric — preserve NaN (no imputation)
    df_melted["reading_value"] = pd.to_numeric(
        df_melted["reading_value"], errors="coerce"
    )

    # 7. Drop duplicate (time, parameter_name) pairs within the file
    df_melted.drop_duplicates(
        subset=["time", "parameter_name"], inplace=True
    )
    df_melted.dropna(subset=["time", "parameter_name"], inplace=True)

    logger.info("Parsed %d long-format rows from XLS.", len(df_melted))
    return df_melted


# ── Ingestion ─────────────────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO state_telemetry (time, meter_id, parameter_name, reading_value)
VALUES (:time, :meter_id, :parameter_name, :reading_value)
ON CONFLICT (time, meter_id, parameter_name) DO NOTHING;
"""


def ingest(engine: Engine, file_path: Path, meter_id: int) -> int:
    """Parse the XLS and bulk-upsert records into state_telemetry.

    Returns the total number of rows submitted (conflicts are silently skipped).
    """
    df = parse_excel(file_path)
    df["meter_id"] = meter_id
    df = df[["time", "meter_id", "parameter_name", "reading_value"]]

    total = len(df)
    batch_size = 2_000
    inserted = 0

    with engine.begin() as conn:
        for start in range(0, total, batch_size):
            batch = df.iloc[start : start + batch_size]
            rows = batch.to_dict("records")
            conn.execute(text(_UPSERT_SQL), rows)
            inserted += len(rows)
            logger.info(
                "Ingested rows %d–%d / %d",
                start + 1, start + len(rows), total,
            )

    logger.info(
        "Ingestion complete — %d rows submitted to state_telemetry "
        "(conflicts silently skipped, data is idempotent).",
        total,
    )
    return total


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest A Block historical XLS data into state_telemetry."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing Block A state_telemetry rows before ingesting.",
    )
    args = parser.parse_args()

    db_engine = get_database_engine()
    setup_state_table(db_engine)
    target_meter_id = get_meter_id(db_engine, "PH-A-MAIN")

    if args.reset:
        reset_block_a_telemetry(db_engine, target_meter_id)

    ingest(db_engine, A_BLOCK_XLS, target_meter_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())