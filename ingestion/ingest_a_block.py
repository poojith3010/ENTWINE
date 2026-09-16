"""Data ingestion pipeline for A Block historical energy time-series.

Parses the XLS report, locates the true data headers, creates the state_telemetry
table, and inserts readings into TimescaleDB without imputing missing values.
"""

import logging
import os
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("ingestion.a_block")

FILE_PATH = Path("real time energy data/POWERHOUSE_1/POWERHOUSE_1.A_BLOCK/New Tabular Report for report(2).xls")


def get_database_engine() -> Engine:
    """Load credentials and create a SQLAlchemy Engine."""
    load_dotenv()
    url = (f"postgresql+psycopg2://{os.getenv('DB_USER', 'postgres')}:"
           f"{os.getenv('DB_PASSWORD', '')}@{os.getenv('DB_HOST', 'localhost')}:"
           f"{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'entwine_db')}")
    return create_engine(url)


def setup_state_table(engine: Engine) -> None:
    """Ensure state_telemetry exists and is enabled for TimescaleDB."""
    schema_sql = """
    CREATE TABLE IF NOT EXISTS state_telemetry (
        time TIMESTAMPTZ NOT NULL,
        meter_id BIGINT NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
        parameter_name VARCHAR(128) NOT NULL,
        reading_value NUMERIC(14, 6),
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


def get_meter_id(engine: Engine, meter_code: str) -> int:
    """Retrieve the primary key meter_id for a given meter code."""
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT meter_id FROM meters WHERE meter_code = :code"),
            {"code": meter_code}
        )
        row = result.fetchone()
        if not row:
            raise ValueError(f"Meter '{meter_code}' not found in registry. Run seed_a_block.py first.")
        return int(row[0])


def ingest_excel_data(engine: Engine, file_path: Path, meter_id: int) -> None:
    """Parse Excel, preserve null values without imputation, and insert."""
    if not file_path.exists():
        logger.error("File not found: %s", file_path)
        return

    logger.info("Reading raw file: %s", file_path)
    df_raw = pd.read_excel(file_path)

    # 1. Locate the true header row
    header_idx = -1
    for i, row in df_raw.iterrows():
        row_str = " ".join([str(x).lower() for x in row.values])
        if "timestamp" in row_str:
            header_idx = int(i)
            break

    if header_idx == -1:
        logger.error("Could not find the 'Timestamp' header row in the file.")
        return

    logger.info("Header found at row %d. Loading data...", header_idx)
    # FIX: Use header=header_idx + 1 instead of skiprows
    df = pd.read_excel(file_path, header=header_idx + 1)
    
    # Drop entirely empty columns (formatting noise)
    df.dropna(axis=1, how="all", inplace=True)

    # 2. Clean column headers
    clean_cols = []
    for col in df.columns:
        c = str(col).replace("\n", " ").replace("POWERHOUSE_1.A_BLOCK", "").strip()
        c = " ".join(c.split())
        clean_cols.append(c)
    df.columns = clean_cols

    # Ensure Timestamp column is named 'time' for TimescaleDB
    time_col = [c for c in df.columns if "timestamp" in c.lower()][0]
    df.rename(columns={time_col: "time"}, inplace=True)
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
    df.dropna(subset=["time"], inplace=True)

    # 3. Melt to (time, parameter_name, reading_value)
    df_melted = df.melt(id_vars=["time"], var_name="parameter_name", value_name="reading_value")

    # Coerce values to numeric; preserve NaNs/None (no imputation)
    df_melted["reading_value"] = pd.to_numeric(df_melted["reading_value"], errors="coerce")
    df_melted["meter_id"] = meter_id
    df_melted = df_melted[["time", "meter_id", "parameter_name", "reading_value"]]

    # 4. Remove duplicate readings if any exist within the file
    df_melted.drop_duplicates(subset=["time", "meter_id", "parameter_name"], inplace=True)

    logger.info("Bulk inserting %d records into state_telemetry...", len(df_melted))
    try:
        df_melted.to_sql(
            "state_telemetry", 
            engine, 
            if_exists="append", 
            index=False, 
            method="multi", 
            chunksize=5000
        )
        logger.info("Ingestion completed successfully for A Block.")
    except SQLAlchemyError as exc:
        logger.error("Bulk insert failed: %s", exc)
        raise


if __name__ == "__main__":
    db_engine = get_database_engine()
    setup_state_table(db_engine)
    target_meter_id = get_meter_id(db_engine, "PH-A-MAIN")
    ingest_excel_data(db_engine, FILE_PATH, target_meter_id)