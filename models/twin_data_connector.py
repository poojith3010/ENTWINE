"""Data access layer for retrieving digital twin state telemetry.

Provides the models with a lightweight, decoupled bridge to TimescaleDB,
returning heavily formatted pandas DataFrames ready for AI inference.
"""

import logging
import os
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("models.data_connector")

# Map raw database columns to the exact snake_case format required by CAFA/GrCF
COLUMN_MAPPING = {
    "Real Power (kW)": "real_power_kw",
    "Real Power A (kW)": "real_power_a_kw",
    "Real Power B (kW)": "real_power_b_kw",
    "Real Power C (kW)": "real_power_c_kw",
    "Current Avg (A)": "current_avg_a",
    "Current A (A)": "current_a_a",
    "Current B (A)": "current_b_a",
    "Current C (A)": "current_c_a",
    "Voltage L-N Avg (V)": "voltage_ln_avg_v",
    "Voltage L-L Avg (V)": "voltage_ll_avg_v",
    "Frequency (Hz)": "frequency_hz",
    "Power Factor (%)": "power_factor_pct",
    "Apparent Power (kVA)": "apparent_power_kva"
}

def get_database_engine(custom_url: Optional[str] = None) -> Engine:
    """Create a SQLAlchemy engine, defaulting to .env credentials."""
    if custom_url:
        return create_engine(custom_url)
    
    load_dotenv()
    url = (f"postgresql+psycopg2://{os.getenv('DB_USER', 'postgres')}:"
           f"{os.getenv('DB_PASSWORD', '')}@{os.getenv('DB_HOST', 'localhost')}:"
           f"{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'entwine_db')}")
    return create_engine(url, pool_pre_ping=True)

def get_building_state_df(meter_code: str, engine: Optional[Engine] = None) -> pd.DataFrame:
    """
    Retrieve and pivot historical telemetry for a specific meter.
    
    Args:
        meter_code: The exact string identifier of the meter (e.g., 'PH-A-MAIN').
        engine: Optional injected SQLAlchemy Engine for testing.
        
    Returns:
        A chronologically sorted, UTC timezone-aware pandas DataFrame where 
        the index is 'time' and the columns are the mapped sensor parameters.
    """
    if engine is None:
        engine = get_database_engine()

    query = text("""
        SELECT st.time, st.parameter_name, st.reading_value
        FROM state_telemetry st
        JOIN meters m ON st.meter_id = m.meter_id
        WHERE m.meter_code = :meter_code
        ORDER BY st.time ASC
    """)

    logger.info("Querying digital twin state for meter: %s", meter_code)
    try:
        with engine.connect() as conn:
            df_long = pd.read_sql_query(query, conn, params={"meter_code": meter_code})
    except SQLAlchemyError as exc:
        logger.error("Database connection or execution failed: %s", exc)
        raise ConnectionError(f"Failed to retrieve data for {meter_code}.") from exc

    if df_long.empty:
        logger.warning("No telemetry data found for meter code: %s", meter_code)
        return pd.DataFrame()

    # Pivot the data
    df_wide = df_long.pivot_table(
        index='time', 
        columns='parameter_name', 
        values='reading_value', 
        aggfunc='mean'
    )

    # Map the columns to standard AI inputs
    df_wide.rename(columns=COLUMN_MAPPING, inplace=True)

    # Ensure UTC timezone awareness
    if df_wide.index.tz is None:
        df_wide.index = df_wide.index.tz_localize('UTC')
    else:
        df_wide.index = df_wide.index.tz_convert('UTC')

    df_wide.sort_index(ascending=True, inplace=True)
    
    logger.info("Successfully loaded %d records with %d sensor columns.", len(df_wide), len(df_wide.columns))
    return df_wide