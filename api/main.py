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
    """Apply the additive API schema migration and dispose the engine on shutdown."""
    async with ENGINE.begin() as connection:
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
