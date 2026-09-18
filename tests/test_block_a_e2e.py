"""End-to-end integration tests for the ENTWINE Block A Digital Twin.

These tests verify the complete data flow from:
  1. Asset Registry (PH-A-BLK / PH-A-MAIN correctly registered)
  2. state_telemetry (Block A data ingested, pivotable, UTC-indexed)
  3. anomaly_events (CAFA/GrCF produced a valid event for PH-A-MAIN)
  4. GridReason (natural language explanation written to DB)
  5. FastAPI endpoints (health, telemetry, anomalies return 200 OK)
  6. Dashboard data flow (fetch_latest_anomaly returns live data without errors)

IMPORTANT: These tests require:
  - A running Docker TimescaleDB instance
  - The full pipeline (python run_block_a_pipeline.py) to have run at least once
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final
from unittest.mock import patch

import pandas as pd
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# ── Load project credentials ──────────────────────────────────────────────────

_ENV_PATH: Final[Path] = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def db_engine() -> Engine:
    """Return a live database engine for the test session."""
    db_user     = os.getenv("DB_USER",     "entwine_admin")
    db_password = os.getenv("DB_PASSWORD", "change_me_now")
    db_host     = os.getenv("DB_HOST",     "localhost")
    db_port     = os.getenv("DB_PORT",     "5432")
    db_name     = os.getenv("DB_NAME",     "entwine_twin")
    engine = create_engine(
        f"postgresql+psycopg2://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}",
        pool_pre_ping=True,
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def api_client():
    """FastAPI TestClient for the ENTWINE REST API."""
    from api.main import app  # noqa: PLC0415
    with TestClient(app) as client:
        yield client


# ── 1. Asset Registry ─────────────────────────────────────────────────────────

class TestAssetRegistry:
    """Block A must be correctly registered in the Module 1 asset registry."""

    def test_pha_blk_building_exists(self, db_engine: Engine) -> None:
        with db_engine.connect() as conn:
            row = conn.execute(
                text("SELECT building_name, occupancy_type FROM buildings WHERE building_code = 'PH-A-BLK'")
            ).fetchone()
        assert row is not None, "PH-A-BLK building must be registered"
        assert row[0] == "Powerhouse A Block"
        assert row[1] == "utility"

    def test_pha_main_meter_exists(self, db_engine: Engine) -> None:
        with db_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT m.meter_type, m.protocol, m.sampling_interval_seconds, m.is_active
                    FROM meters m
                    JOIN buildings b ON m.building_id = b.building_id
                    WHERE m.meter_code = 'PH-A-MAIN' AND b.building_code = 'PH-A-BLK'
                    """
                )
            ).fetchone()
        assert row is not None, "PH-A-MAIN meter must be registered under PH-A-BLK"
        assert row[0] == "main"
        assert row[1] == "manual_export"
        assert row[2] == 900       # 15-minute sampling
        assert row[3] is True      # active


# ── 2. State Telemetry (Ingestion) ────────────────────────────────────────────

class TestStateTelemetry:
    """Block A telemetry must be ingested and queryable from state_telemetry."""

    def test_telemetry_exists(self, db_engine: Engine) -> None:
        with db_engine.connect() as conn:
            count = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM state_telemetry st
                    JOIN meters m ON st.meter_id = m.meter_id
                    WHERE m.meter_code = 'PH-A-MAIN'
                    """
                )
            ).scalar()
        assert count > 0, "state_telemetry must have rows for PH-A-MAIN"

    def test_telemetry_utc_timestamps(self, db_engine: Engine) -> None:
        with db_engine.connect() as conn:
            null_ts = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM state_telemetry st
                    JOIN meters m ON st.meter_id = m.meter_id
                    WHERE m.meter_code = 'PH-A-MAIN'
                      AND st.time AT TIME ZONE 'UTC' IS NULL
                    """
                )
            ).scalar()
        assert null_ts == 0, "All state_telemetry timestamps must be valid UTC"

    def test_twin_data_connector_returns_df(self) -> None:
        """twin_data_connector must return a properly shaped DataFrame."""
        from models.twin_data_connector import get_building_state_df  # noqa: PLC0415
        df = get_building_state_df("PH-A-MAIN")

        assert not df.empty, "DataFrame must not be empty for PH-A-MAIN"
        assert isinstance(df.index, pd.DatetimeIndex), "Index must be DatetimeIndex"
        assert df.index.tz is not None, "Index must be timezone-aware"
        assert str(df.index.tz) == "UTC", "Index must be UTC"
        assert df.index.is_monotonic_increasing, "Data must be chronologically sorted"
        assert "real_power_kw" in df.columns, "Mapped column real_power_kw must exist"


# ── 3. Anomaly Events (CAFA + GrCF) ──────────────────────────────────────────

class TestAnomalyEvents:
    """CAFA/GrCF must produce valid anomaly_events for PH-A-MAIN."""

    def test_anomaly_event_exists(self, db_engine: Engine) -> None:
        with db_engine.connect() as conn:
            count = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM anomaly_events ae
                    JOIN meters m ON ae.meter_id = m.meter_id
                    WHERE m.meter_code = 'PH-A-MAIN'
                    """
                )
            ).scalar()
        assert count > 0, "At least one anomaly_event must exist for PH-A-MAIN"

    def test_anomaly_counterfactual_tensor_shape(self, db_engine: Engine) -> None:
        """Counterfactual tensor must be a 2D list with SEQ_LEN=16 rows."""
        with db_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT ae.counterfactual_data
                    FROM anomaly_events ae
                    JOIN meters m ON ae.meter_id = m.meter_id
                    WHERE m.meter_code = 'PH-A-MAIN'
                    ORDER BY ae.time DESC
                    LIMIT 1
                    """
                )
            ).fetchone()
        assert row is not None, "Must have at least one anomaly event"
        tensor = row[0]
        assert isinstance(tensor, list), "counterfactual_data must be a list"
        assert len(tensor) == 16, f"Tensor must have 16 time-steps; got {len(tensor)}"
        assert all(isinstance(step, list) for step in tensor), (
            "Each time-step must be a list of feature values"
        )
        n_features = len(tensor[0])
        assert n_features > 0, "Each time-step must have at least one feature"
        assert all(len(step) == n_features for step in tensor), (
            "All time-steps must have the same number of features"
        )

    def test_gridreason_explanation_written(self, db_engine: Engine) -> None:
        with db_engine.connect() as conn:
            explanation = conn.execute(
                text(
                    """
                    SELECT ae.natural_language_explanation
                    FROM anomaly_events ae
                    JOIN meters m ON ae.meter_id = m.meter_id
                    WHERE m.meter_code = 'PH-A-MAIN'
                      AND ae.natural_language_explanation IS NOT NULL
                    ORDER BY ae.time DESC
                    LIMIT 1
                    """
                )
            ).scalar()
        assert explanation is not None, "GridReason must write an explanation"
        assert len(explanation) > 20, "Explanation must be a non-trivial string"
        assert "PH-A-MAIN" in explanation or "meter_id" in explanation, (
            "Explanation must reference the Block A meter"
        )


# ── 4. FastAPI REST Endpoints ─────────────────────────────────────────────────

class TestFastAPIEndpoints:
    """All REST endpoints must respond correctly."""

    def test_health_endpoint(self, api_client: TestClient) -> None:
        response = api_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "online"}

    def test_telemetry_endpoint_returns_data(self, api_client: TestClient) -> None:
        response = api_client.get("/api/v1/telemetry/PH-A-MAIN")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0
        first = data[0]
        assert "time" in first
        assert "parameter_name" in first
        assert "reading_value" in first

    def test_anomalies_endpoint_returns_event(self, api_client: TestClient) -> None:
        response = api_client.get("/api/v1/anomalies/PH-A-MAIN")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0
        event = data[0]
        assert "time" in event
        assert "detector_type" in event
        assert event["detector_type"] == "CAFA-GrCF"
        assert "counterfactual_data" in event
        assert len(event["counterfactual_data"]) == 16

    def test_unknown_meter_returns_404(self, api_client: TestClient) -> None:
        response = api_client.get("/api/v1/telemetry/NONEXISTENT-METER-XYZ")
        assert response.status_code == 404


# ── 5. Dashboard Data Flow ────────────────────────────────────────────────────

class TestDashboard:
    """Dashboard must fetch live anomaly data without errors."""

    def test_fetch_latest_anomaly_returns_live_data(self, api_client: TestClient) -> None:
        """Dashboard state must contain a live alert with the GridReason explanation."""
        from dashboard.app import fetch_latest_anomaly, DashboardState  # noqa: PLC0415

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value = api_client
            state = fetch_latest_anomaly()

        assert isinstance(state, DashboardState)
        assert "Active anomaly" in state.alert or "CAFA" in state.alert
        assert "PH-A-MAIN" in state.metadata
        assert "CAFA-GrCF" in state.metadata
        assert "offline" not in state.status.lower() or "status-ok" in state.status
