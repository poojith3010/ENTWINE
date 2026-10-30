"""Tests for the configurable one-minute simulated meter service."""

from __future__ import annotations

import time
from datetime import timedelta

from validation.live_meter_service import LiveMeterSimulator, SimulationConfig


def _run_short_session(seed: int) -> list:
    """Run an accelerated session and collect all generated readings."""
    readings = []
    simulator = LiveMeterSimulator(
        SimulationConfig(
            duration_minutes=8,
            interval_seconds=0.005,
            abnormal_start_minute=3,
            abnormal_duration_minutes=2,
            random_seed=seed,
        ),
        readings.append,
    )
    assert simulator.start() is True
    deadline = time.time() + 2.0
    while time.time() < deadline and simulator.stats.status == "running":
        time.sleep(0.01)
    assert simulator.stats.status == "completed"
    return readings


def test_service_generates_one_timestamp_per_simulated_minute() -> None:
    """Verify the synthetic clock advances by one minute per reading."""
    readings = _run_short_session(7)
    assert len(readings) == 8
    assert all(
        current.time - previous.time == timedelta(minutes=1)
        for previous, current in zip(readings, readings[1:])
    )
    assert sum(reading.known_anomaly for reading in readings) == 2


def test_service_is_reproducible_with_a_fixed_seed() -> None:
    """Verify fixed seed and configuration produce identical readings."""
    first = _run_short_session(99)
    second = _run_short_session(99)
    assert [reading.real_power_kw for reading in first] == [
        reading.real_power_kw for reading in second
    ]
    assert [reading.known_anomaly for reading in first] == [
        reading.known_anomaly for reading in second
    ]


def test_service_prevents_duplicate_start() -> None:
    """Verify a running service cannot create a second uncontrolled worker."""
    simulator = LiveMeterSimulator(
        SimulationConfig(duration_minutes=2, interval_seconds=0.2, abnormal_start_minute=0),
    )
    assert simulator.start() is True
    assert simulator.start() is False
    simulator.stop()
