"""FastAPI REST backend for the ENTWINE digital twin.

The API exposes recent telemetry, persisted CAFA/GrCF anomaly events, and a
minimal service health endpoint. Database access is asynchronous and all
credentials are loaded from the project-root ``.env`` file.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Final
from urllib.parse import quote_plus

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

PROJECT_ROOT: Final[str] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


def _database_url() -> str:
    """Build an asyncpg SQLAlchemy URL from validated environment settings."""
    required = ("DB_USER", "DB_PASSWORD", "DB_NAME")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing database environment variables: {missing}")

    user = quote_plus(os.environ["DB_USER"])
    password = quote_plus(os.environ["DB_PASSWORD"])
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    database = quote_plus(os.environ["DB_NAME"])
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


DATABASE_URL: Final[str] = _database_url()
ENGINE: Final[AsyncEngine] = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SESSION_FACTORY: Final[async_sessionmaker[AsyncSession]] = async_sessionmaker(
    ENGINE,
    expire_on_commit=False,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Ensure the anomaly_events table exists (with all columns) on startup.

    Creates the table idempotently if it does not yet exist, then ensures
    the natural_language_explanation column is present. This makes the API
    safe to start before models/orchestrator.py has been executed.
    """
    async with ENGINE.begin() as connection:
        # Step 1: Create the table if it does not exist yet
        await connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS anomaly_events (
                    time                         TIMESTAMPTZ NOT NULL,
                    meter_id                     BIGINT      NOT NULL
                                                 REFERENCES meters(meter_id),
                    detector_type                VARCHAR(64) NOT NULL,
                    counterfactual_data          JSONB       NOT NULL,
                    natural_language_explanation TEXT
                )
                """
            )
        )
        # Step 2: Add the explanation column if a pre-existing table lacks it
        await connection.execute(
            text(
                """
                ALTER TABLE anomaly_events
                ADD COLUMN IF NOT EXISTS natural_language_explanation TEXT
                """
            )
        )
    yield
    await ENGINE.dispose()


class TelemetryRecord(BaseModel):
    """Serialized long-format telemetry record."""

    model_config = ConfigDict(from_attributes=True)

    time: datetime
    parameter_name: str
    reading_value: float | None


class AnomalyRecord(BaseModel):
    """Serialized persisted CAFA/GrCF anomaly event."""

    model_config = ConfigDict(from_attributes=True)

    time: datetime
    detector_type: str
    counterfactual_data: list[list[float]]
    natural_language_explanation: str | None


class HealthResponse(BaseModel):
    """Service health response."""

    status: str = Field(pattern="^online$")


app = FastAPI(
    title="ENTWINE Digital Twin API",
    description="REST interface for telemetry and intelligence-layer outputs.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield one SQLAlchemy async session and close it after the request."""
    async with SESSION_FACTORY() as session:
        yield session


@app.get(
    "/api/v1/telemetry/{meter_code}",
    response_model=list[TelemetryRecord],
    status_code=status.HTTP_200_OK,
)
async def get_telemetry(
    meter_code: str = Path(..., min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
) -> list[TelemetryRecord]:
    """Return the 100 most recent long-format telemetry records for a meter."""
    query = text(
        """
        SELECT st.time, st.parameter_name, st.reading_value
        FROM state_telemetry AS st
        JOIN meters AS m ON m.meter_id = st.meter_id
        WHERE m.meter_code = :meter_code
        ORDER BY st.time DESC
        LIMIT 100
        """
    )
    try:
        result = await db.execute(query, {"meter_code": meter_code})
        rows = result.mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry database is unavailable.",
        ) from exc
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No telemetry found for meter '{meter_code}'.",
        )
    return [TelemetryRecord.model_validate(row) for row in rows]


@app.get(
    "/api/v1/anomalies/{meter_code}",
    response_model=list[AnomalyRecord],
    status_code=status.HTTP_200_OK,
)
async def get_anomalies(
    meter_code: str = Path(..., min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
) -> list[AnomalyRecord]:
    """Return anomaly events for a meter, newest first."""
    query = text(
        """
        SELECT ae.time, ae.detector_type, ae.counterfactual_data,
               ae.natural_language_explanation
        FROM anomaly_events AS ae
        JOIN meters AS m ON m.meter_id = ae.meter_id
        WHERE m.meter_code = :meter_code
        ORDER BY ae.time DESC
        """
    )
    try:
        result = await db.execute(query, {"meter_code": meter_code})
        rows = result.mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Anomaly database is unavailable.",
        ) from exc
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No anomalies found for meter '{meter_code}'.",
        )
    return [AnomalyRecord.model_validate(row) for row in rows]


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Return a lightweight API availability response."""
    return HealthResponse(status="online")


class TelemetryWindowPoint(BaseModel):
    """One pivoted telemetry reading returned by the window endpoint."""
    model_config = ConfigDict(from_attributes=True)
    time: datetime
    real_power_kw: float | None = None
    power_factor_pct: float | None = None
    current_avg_a: float | None = None
    voltage_ln_avg_v: float | None = None
    frequency_hz: float | None = None
    apparent_power_kva: float | None = None


class MonthlyTelemetryPoint(BaseModel):
    """One live monthly energy summary for dashboard trend charts."""

    model_config = ConfigDict(from_attributes=True)

    month: datetime
    average_real_power_kw: float | None = None
    average_power_factor_pct: float | None = None


@app.get(
    "/api/v1/telemetry/{meter_code}/window",
    response_model=list[TelemetryWindowPoint],
    status_code=status.HTTP_200_OK,
)
async def get_telemetry_window(
    meter_code: str = Path(..., min_length=1, max_length=64),
    hours: int = 48,
    db: AsyncSession = Depends(get_db),
) -> list[TelemetryWindowPoint]:
    """Return pivoted telemetry for the key dashboard parameters.

    Fetches up to `hours` hours of data for the main charted parameters,
    pivoted into wide format (one row per timestamp). Used exclusively by
    the dashboard chart — not intended as a general-purpose endpoint.
    """
    query = text(
        """
        SELECT
            st.time,
            MAX(CASE WHEN st.parameter_name = 'Real Power (kW)'       THEN st.reading_value END) AS real_power_kw,
            MAX(CASE WHEN st.parameter_name = 'Power Factor (%)'       THEN st.reading_value END) AS power_factor_pct,
            MAX(CASE WHEN st.parameter_name = 'Current Avg (A)'        THEN st.reading_value END) AS current_avg_a,
            MAX(CASE WHEN st.parameter_name = 'Voltage L-N Avg (V)'    THEN st.reading_value END) AS voltage_ln_avg_v,
            MAX(CASE WHEN st.parameter_name = 'Frequency (Hz)'         THEN st.reading_value END) AS frequency_hz,
            MAX(CASE WHEN st.parameter_name = 'Apparent Power (kVA)'   THEN st.reading_value END) AS apparent_power_kva
        FROM state_telemetry AS st
        JOIN meters AS m ON m.meter_id = st.meter_id
        WHERE m.meter_code = :meter_code
          AND st.time >= NOW() - MAKE_INTERVAL(hours => :hours)
          AND st.parameter_name IN (
              'Real Power (kW)', 'Power Factor (%)',
              'Current Avg (A)', 'Voltage L-N Avg (V)',
              'Frequency (Hz)', 'Apparent Power (kVA)'
          )
        GROUP BY st.time
        ORDER BY st.time ASC
        LIMIT 500
        """
    )
    try:
        result = await db.execute(query, {"meter_code": meter_code, "hours": hours})
        rows = result.mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry database is unavailable.",
        ) from exc
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No windowed telemetry found for meter '{meter_code}'.",
        )
    return [TelemetryWindowPoint.model_validate(row) for row in rows]


@app.get(
    "/api/v1/telemetry/{meter_code}/monthly",
    response_model=list[MonthlyTelemetryPoint],
    status_code=status.HTTP_200_OK,
)
async def get_monthly_telemetry(
    meter_code: str = Path(..., min_length=1, max_length=64),
    months: int = 24,
    db: AsyncSession = Depends(get_db),
) -> list[MonthlyTelemetryPoint]:
    """Return monthly averages computed from live state telemetry."""
    if months < 1 or months > 120:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="months must be between 1 and 120.",
        )
    query = text(
        """
        SELECT
            date_trunc('month', st.time AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS month,
            AVG(CASE WHEN st.parameter_name = 'Real Power (kW)' THEN st.reading_value END)
                AS average_real_power_kw,
            AVG(CASE WHEN st.parameter_name = 'Power Factor (%)' THEN st.reading_value END)
                AS average_power_factor_pct
        FROM state_telemetry AS st
        JOIN meters AS m ON m.meter_id = st.meter_id
        WHERE m.meter_code = :meter_code
          AND st.time >= NOW() - MAKE_INTERVAL(months => :months)
          AND st.parameter_name IN ('Real Power (kW)', 'Power Factor (%)')
        GROUP BY date_trunc('month', st.time AT TIME ZONE 'UTC')
        ORDER BY month ASC
        """
    )
    try:
        result = await db.execute(query, {"meter_code": meter_code, "months": months})
        rows = result.mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry database is unavailable.",
        ) from exc
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No monthly telemetry found for meter '{meter_code}'.",
        )
    return [MonthlyTelemetryPoint.model_validate(row) for row in rows]


@app.get(
    "/api/v1/telemetry/{meter_code}/anomaly-window",
    response_model=list[TelemetryWindowPoint],
    status_code=status.HTTP_200_OK,
)
async def get_anomaly_window(
    meter_code: str = Path(..., min_length=1, max_length=64),
    hours_before: int = 24,
    hours_after: int = 24,
    db: AsyncSession = Depends(get_db),
) -> list[TelemetryWindowPoint]:
    """Return pivoted telemetry centred around the latest anomaly event.

    Fetches `hours_before` hours before and `hours_after` hours after the
    most recent anomaly timestamp. Used by the dashboard anomaly chart.
    """
    anomaly_query = text(
        """
        SELECT ae.time
        FROM anomaly_events ae
        JOIN meters m ON ae.meter_id = m.meter_id
        WHERE m.meter_code = :meter_code
        ORDER BY ae.time DESC
        LIMIT 1
        """
    )
    try:
        anomaly_result = await db.execute(anomaly_query, {"meter_code": meter_code})
        anomaly_row = anomaly_result.fetchone()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Database unavailable.") from exc

    if not anomaly_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No anomaly found for meter '{meter_code}'.")

    anomaly_time = anomaly_row[0]

    window_query = text(
        """
        SELECT
            st.time,
            MAX(CASE WHEN st.parameter_name = 'Real Power (kW)'       THEN st.reading_value END) AS real_power_kw,
            MAX(CASE WHEN st.parameter_name = 'Power Factor (%)'       THEN st.reading_value END) AS power_factor_pct,
            MAX(CASE WHEN st.parameter_name = 'Current Avg (A)'        THEN st.reading_value END) AS current_avg_a,
            MAX(CASE WHEN st.parameter_name = 'Voltage L-N Avg (V)'    THEN st.reading_value END) AS voltage_ln_avg_v,
            MAX(CASE WHEN st.parameter_name = 'Frequency (Hz)'         THEN st.reading_value END) AS frequency_hz,
            MAX(CASE WHEN st.parameter_name = 'Apparent Power (kVA)'   THEN st.reading_value END) AS apparent_power_kva
        FROM state_telemetry AS st
        JOIN meters AS m ON m.meter_id = st.meter_id
        WHERE m.meter_code = :meter_code
          AND st.time BETWEEN :start_time AND :end_time
          AND st.parameter_name IN (
              'Real Power (kW)', 'Power Factor (%)',
              'Current Avg (A)', 'Voltage L-N Avg (V)',
              'Frequency (Hz)', 'Apparent Power (kVA)'
          )
        GROUP BY st.time
        ORDER BY st.time ASC
        LIMIT 500
        """
    )
    from datetime import timedelta
    start_time = anomaly_time - timedelta(hours=hours_before)
    end_time = anomaly_time + timedelta(hours=hours_after)

    try:
        result = await db.execute(window_query, {
            "meter_code": meter_code,
            "start_time": start_time,
            "end_time": end_time,
        })
        rows = result.mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Database unavailable.") from exc

    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No telemetry in anomaly window.")

    return [TelemetryWindowPoint.model_validate(row) for row in rows]
