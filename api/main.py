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

from validation.live_meter_service import (
    LiveMeterSimulator,
    SimulatedReading,
    SimulatedReadingStore,
    SimulationConfig,
)
from validation.sim_pipeline import (
    run_full_sim_pipeline,
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
SIMULATED_STORE: Final[SimulatedReadingStore] = SimulatedReadingStore()
LIVE_SIMULATOR: LiveMeterSimulator | None = None


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


class SimulatorStartRequest(BaseModel):
    """Validated configuration for starting a simulated meter session."""

    simulation_id: str = Field(default="demo-ph-a", min_length=1, max_length=100)
    duration_minutes: int = Field(default=15, ge=1, le=1440)
    interval_seconds: float = Field(default=60.0, gt=0, le=3600)
    abnormal_start_minute: int = Field(default=5, ge=0)
    abnormal_duration_minutes: int = Field(default=4, ge=1)
    abnormal_multiplier: float = Field(default=1.6, gt=1.0, le=10.0)
    random_seed: int = Field(default=20261030)


class SimulatorStatusResponse(BaseModel):
    """Current simulated-meter lifecycle and processing counters."""

    status: str
    meter_code: str
    simulation_id: str
    generated: int
    persisted: int
    failed: int
    injected_anomalies: int
    latest_reading: SimulatedReading | None = None
    error: str | None = None


class SimulatedTelemetryRecord(BaseModel):
    """One persisted synthetic meter observation."""

    time: datetime
    meter_code: str
    simulation_id: str
    real_power_kw: float
    current_avg_a: float
    power_factor_pct: float
    known_anomaly: bool
    simulation_status: str


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


def _simulator_status() -> SimulatorStatusResponse:
    """Convert the active simulator state into an API response."""
    simulator = LIVE_SIMULATOR
    if simulator is None:
        return SimulatorStatusResponse(
            status="stopped",
            meter_code="PH-A-MAIN",
            simulation_id="",
            generated=0,
            persisted=0,
            failed=0,
            injected_anomalies=0,
        )
    stats = simulator.stats
    return SimulatorStatusResponse(
        status=stats.status,
        meter_code=simulator.config.meter_code,
        simulation_id=simulator.config.simulation_id,
        generated=stats.generated,
        persisted=stats.persisted,
        failed=stats.failed,
        injected_anomalies=stats.injected_anomalies,
        latest_reading=stats.last_reading,
        error=stats.error,
    )


@app.post("/api/v1/simulator/start", response_model=SimulatorStatusResponse)
async def start_simulator(request: SimulatorStartRequest) -> SimulatorStatusResponse:
    """Start one controlled simulated PH-A-MAIN meter session."""
    global LIVE_SIMULATOR
    if LIVE_SIMULATOR is not None and LIVE_SIMULATOR.stats.status == "running":
        raise HTTPException(status_code=409, detail="Simulator is already running.")
    config = SimulationConfig(
        meter_code="PH-A-MAIN",
        simulation_id=request.simulation_id,
        duration_minutes=request.duration_minutes,
        interval_seconds=request.interval_seconds,
        abnormal_start_minute=request.abnormal_start_minute,
        abnormal_duration_minutes=request.abnormal_duration_minutes,
        abnormal_multiplier=request.abnormal_multiplier,
        random_seed=request.random_seed,
    )
    try:
        SIMULATED_STORE.ensure_table()
        LIVE_SIMULATOR = LiveMeterSimulator(config, SIMULATED_STORE.persist)
        LIVE_SIMULATOR.start()
    except (ValueError, SQLAlchemyError, OSError) as exc:
        LIVE_SIMULATOR = None
        raise HTTPException(status_code=503, detail=f"Simulator could not start: {exc}") from exc
    return _simulator_status()


@app.post("/api/v1/simulator/stop", response_model=SimulatorStatusResponse)
async def stop_simulator() -> SimulatorStatusResponse:
    """Stop the active simulated meter session without deleting its records."""
    if LIVE_SIMULATOR is not None:
        LIVE_SIMULATOR.stop()
    return _simulator_status()


@app.get("/api/v1/simulator/status", response_model=SimulatorStatusResponse)
async def simulator_status() -> SimulatorStatusResponse:
    """Return current simulator status, counters, and latest reading."""
    return _simulator_status()


@app.get(
    "/api/v1/simulator/readings",
    response_model=list[SimulatedTelemetryRecord],
)
async def simulated_readings(
    simulation_id: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
) -> list[SimulatedTelemetryRecord]:
    """Return persisted synthetic readings, newest first."""
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000.")
    query = text("""
        SELECT st.time, m.meter_code, st.simulation_id,
               st.real_power_kw, st.current_avg_a, st.power_factor_pct,
               st.known_anomaly, st.simulation_status
        FROM simulated_telemetry st
        JOIN meters m ON m.meter_id = st.meter_id
        WHERE (:simulation_id IS NULL OR st.simulation_id = :simulation_id)
        ORDER BY st.time DESC
        LIMIT :limit
    """)
    try:
        result = await db.execute(query, {"simulation_id": simulation_id, "limit": limit})
        rows = result.mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Simulator database is unavailable.") from exc
    return [SimulatedTelemetryRecord.model_validate(row) for row in rows]


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


# == Simulation Pipeline Endpoints =============================================

import asyncio
import math


def _nan_to_none(v):
    """Convert NaN/inf to None for JSON serialisation."""
    if v is None:
        return None
    try:
        if math.isnan(v) or math.isinf(v):
            return None
    except TypeError:
        return None
    return v


class FeatureChangeRecord(BaseModel):
    """One feature change from observed to counterfactual."""
    feature: str
    delta: float


class GrCFSummary(BaseModel):
    """GrCF result summary for the dashboard."""
    status: str
    anomaly_idx: int = -1
    anomaly_time: datetime | None = None
    counterfactual_shape: list[int] = []
    feature_changes: list[FeatureChangeRecord] = []
    error: str | None = None
    elapsed_s: float = 0.0


class CafaSummary(BaseModel):
    """CAFA run summary for the dashboard."""
    status: str
    records_processed: int = 0
    anomalies_flagged: int = 0
    mae_kw: float = 0.0
    error: str | None = None
    elapsed_s: float = 0.0


class AuditSummary(BaseModel):
    """CAFA audit summary for the dashboard."""
    status: str
    n_evaluated: int = 0
    n_true_positive: int = 0
    n_false_positive: int = 0
    n_false_negative: int = 0
    n_true_negative: int = 0
    recall: float | None = None
    precision: float | None = None
    f1: float | None = None
    n_known_anomaly: int = 0
    n_cafa_flagged: int = 0
    warnings: list[str] = []
    note: str = ""


class GridReasonSummary(BaseModel):
    """GridReason alert summary for the dashboard."""
    status: str
    alert: str | None = None


class SimPipelineResponse(BaseModel):
    """Full simulation pipeline result for dashboard consumption."""
    pipeline_status: str
    adapter_status: str
    adapter_n_1min_rows: int = 0
    adapter_n_15min_rows: int = 0
    adapter_warnings: list[str] = []
    cafa: CafaSummary
    grcf: GrCFSummary
    gridreason: GridReasonSummary
    audit: AuditSummary
    summary: str = ""
    pipeline_elapsed_s: float = 0.0


@app.post(
    "/api/v1/simulator/pipeline",
    response_model=SimPipelineResponse,
    status_code=200,
)
async def run_simulation_pipeline(
    simulation_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> SimPipelineResponse:
    """Run the full simulation to CAFA to GrCF to GridReason to Audit pipeline."""
    from validation.sim_pipeline import (
        CafaRunResult,
        GrCFRunResult,
        CafaAuditResult,
        run_full_sim_pipeline,
    )
    import pandas as pd

    df_1min = None
    if simulation_id:
        try:
            q = text(
                "SELECT st.time, st.real_power_kw, st.current_avg_a, "
                "st.voltage_ln_avg_v, st.voltage_ll_avg_v, st.frequency_hz, "
                "st.power_factor_pct, st.apparent_power_kva, st.known_anomaly "
                "FROM simulated_telemetry st "
                "JOIN meters m ON m.meter_id = st.meter_id "
                "WHERE st.simulation_id = :sid ORDER BY st.time ASC"
            )
            res = await db.execute(q, {"sid": simulation_id})
            rows = res.mappings().all()
            if rows:
                recs = [dict(r) for r in rows]
                df_1min = pd.DataFrame(recs)
                df_1min["time"] = pd.to_datetime(df_1min["time"], utc=True)
                df_1min = df_1min.set_index("time")
        except Exception:
            df_1min = None

    loop = asyncio.get_event_loop()
    pipeline_result = await loop.run_in_executor(None, run_full_sim_pipeline, df_1min)

    adapter = pipeline_result.adapter
    cafa = pipeline_result.cafa or CafaRunResult(status="skipped")
    grcf = pipeline_result.grcf or GrCFRunResult(status="skipped")
    gr = pipeline_result.gridreason
    audit = pipeline_result.audit or CafaAuditResult(status="skipped")

    return SimPipelineResponse(
        pipeline_status="ok" if cafa.status == "ok" else cafa.status,
        adapter_status=adapter.status if adapter else "skipped",
        adapter_n_1min_rows=adapter.n_1min_rows if adapter else 0,
        adapter_n_15min_rows=adapter.n_15min_rows if adapter else 0,
        adapter_warnings=adapter.warnings if adapter else [],
        cafa=CafaSummary(
            status=cafa.status,
            records_processed=cafa.records_processed,
            anomalies_flagged=cafa.anomalies_flagged,
            mae_kw=_nan_to_none(cafa.mae_kw) or 0.0,
            error=cafa.error,
            elapsed_s=cafa.elapsed_s,
        ),
        grcf=GrCFSummary(
            status=grcf.status,
            anomaly_idx=grcf.anomaly_idx,
            anomaly_time=grcf.anomaly_time.to_pydatetime() if grcf.anomaly_time else None,
            counterfactual_shape=list(grcf.counterfactual.shape) if grcf.counterfactual is not None else [],
            feature_changes=[
                FeatureChangeRecord(feature=k, delta=v)
                for k, v in (grcf.feature_changes or {}).items()
            ],
            error=grcf.error,
            elapsed_s=grcf.elapsed_s,
        ),
        gridreason=GridReasonSummary(
            status=gr.status if gr else "skipped",
            alert=gr.alert if gr else None,
        ),
        audit=AuditSummary(
            status=audit.status,
            n_evaluated=audit.n_evaluated,
            n_true_positive=audit.n_true_positive,
            n_false_positive=audit.n_false_positive,
            n_false_negative=audit.n_false_negative,
            n_true_negative=audit.n_true_negative,
            recall=_nan_to_none(audit.recall),
            precision=_nan_to_none(audit.precision),
            f1=_nan_to_none(audit.f1),
            n_known_anomaly=audit.n_known_anomaly,
            n_cafa_flagged=audit.n_cafa_flagged,
            warnings=audit.warnings,
            note=audit.note,
        ),
        summary=pipeline_result.summary,
        pipeline_elapsed_s=pipeline_result.pipeline_elapsed_s,
    )
