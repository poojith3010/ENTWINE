"""API integration tests for simulated live meter and analytical pipeline endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Create test client with lifespan context."""
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client: TestClient) -> None:
    """Verify service health endpoint returns 200 online."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "online"}


def test_simulator_status_endpoint(client: TestClient) -> None:
    """Verify simulator status returns expected schema."""
    response = client.get("/api/v1/simulator/status")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "meter_code" in data
    assert data["meter_code"] == "PH-A-MAIN"
    assert "generated" in data
    assert "injected_anomalies" in data


def test_simulator_start_stop_lifecycle(client: TestClient) -> None:
    """Verify simulator start, duplicate start prevention, and stop."""
    # Ensure stopped first
    client.post("/api/v1/simulator/stop")

    # Start
    start_payload = {
        "simulation_id": "test-session",
        "duration_minutes": 2,
        "interval_seconds": 0.05,
        "abnormal_start_minute": 0,
        "abnormal_duration_minutes": 1,
        "random_seed": 42,
    }
    resp_start = client.post("/api/v1/simulator/start", json=start_payload)
    assert resp_start.status_code in (200, 409)

    if resp_start.status_code == 200:
        # Second start should be rejected with 409 Conflict
        resp_dup = client.post("/api/v1/simulator/start", json=start_payload)
        assert resp_dup.status_code == 409
        assert "already running" in resp_dup.json()["detail"].lower()

    # Stop
    resp_stop = client.post("/api/v1/simulator/stop")
    assert resp_stop.status_code == 200
    assert resp_stop.json()["status"] == "stopped"


def test_simulator_readings_endpoint_schema(client: TestClient) -> None:
    """Verify simulator readings endpoint returns valid schema."""
    response = client.get("/api/v1/simulator/readings?limit=5")
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        items = response.json()
        assert isinstance(items, list)
        if items:
            first = items[0]
            assert "time" in first
            assert "meter_code" in first
            assert "real_power_kw" in first
            assert "known_anomaly" in first


def test_simulator_pipeline_endpoint_schema(client: TestClient) -> None:
    """Verify /api/v1/simulator/pipeline endpoint returns full dashboard schema."""
    response = client.post("/api/v1/simulator/pipeline")
    assert response.status_code == 200
    data = response.json()

    # Top-level keys
    assert "pipeline_status" in data
    assert "adapter_status" in data
    assert "cafa" in data
    assert "grcf" in data
    assert "gridreason" in data
    assert "audit" in data
    assert "summary" in data

    # CAFA summary schema
    cafa = data["cafa"]
    assert "status" in cafa
    assert "records_processed" in cafa
    assert "anomalies_flagged" in cafa

    # GrCF summary schema
    grcf = data["grcf"]
    assert "status" in grcf
    assert "feature_changes" in grcf
    assert isinstance(grcf["feature_changes"], list)

    # GridReason summary schema
    gr = data["gridreason"]
    assert "status" in gr

    # Audit summary schema
    audit = data["audit"]
    assert "status" in audit
    assert "n_evaluated" in audit
    assert "n_true_positive" in audit
    assert "n_false_positive" in audit
    assert "warnings" in audit

