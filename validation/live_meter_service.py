"""Configurable one-minute simulated meter service for local demonstrations.

The service generates independent synthetic readings for the registered
``PH-A-MAIN`` meter. It does not modify the real historical telemetry table;
callers may persist readings through the supplied callback and keep simulated
records separate from production history.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Final

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine

from models.twin_data_connector import get_database_engine

LOGGER: Final[logging.Logger] = logging.getLogger("validation.live_meter_service")
ReadingCallback = Callable[["SimulatedReading"], bool | None]


@dataclass(frozen=True)
class SimulationConfig:
    """Configuration for one reproducible simulated meter session."""

    meter_code: str = "PH-A-MAIN"
    simulation_id: str = "demo-ph-a"
    start_time: datetime = datetime(2026, 10, 30, tzinfo=timezone.utc)
    duration_minutes: int = 15
    interval_seconds: float = 60.0
    abnormal_start_minute: int = 5
    abnormal_duration_minutes: int = 4
    abnormal_multiplier: float = 1.6
    random_seed: int = 20261030

    def __post_init__(self) -> None:
        """Validate simulation bounds before a worker can start."""
        if self.start_time.tzinfo is None:
            raise ValueError("start_time must be timezone-aware.")
        if self.duration_minutes < 1 or self.interval_seconds <= 0:
            raise ValueError("duration_minutes and interval_seconds must be positive.")
        if self.abnormal_start_minute < 0 or self.abnormal_duration_minutes < 1:
            raise ValueError("Abnormal-event timing must be non-negative and non-empty.")
        if self.abnormal_start_minute >= self.duration_minutes:
            raise ValueError("abnormal_start_minute must fall within the session.")


@dataclass(frozen=True)
class SimulatedReading:
    """One synthetic meter observation with explicit ground truth metadata."""

    time: datetime
    meter_code: str
    simulation_id: str
    real_power_kw: float
    current_avg_a: float
    voltage_ln_avg_v: float
    voltage_ll_avg_v: float
    frequency_hz: float
    power_factor_pct: float
    apparent_power_kva: float
    known_anomaly: bool
    simulation_status: str = "simulated"


class SimulatedReadingStore:
    """Persist simulated readings without mixing them into real telemetry."""

    def __init__(self, engine: Engine | None = None) -> None:
        """Create a lazy database-backed store."""
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """Return the configured or environment-backed SQLAlchemy engine."""
        if self._engine is None:
            self._engine = get_database_engine()
        return self._engine

    def ensure_table(self) -> None:
        """Create the isolated simulated telemetry table idempotently."""
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS simulated_telemetry (
                    time TIMESTAMPTZ NOT NULL,
                    meter_id BIGINT NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
                    simulation_id VARCHAR(100) NOT NULL,
                    real_power_kw NUMERIC(14, 6) NOT NULL,
                    current_avg_a NUMERIC(14, 6) NOT NULL,
                    voltage_ln_avg_v NUMERIC(14, 6) NOT NULL,
                    voltage_ll_avg_v NUMERIC(14, 6) NOT NULL,
                    frequency_hz NUMERIC(14, 6) NOT NULL,
                    power_factor_pct NUMERIC(14, 6) NOT NULL,
                    apparent_power_kva NUMERIC(14, 6) NOT NULL,
                    known_anomaly BOOLEAN NOT NULL,
                    simulation_status VARCHAR(32) NOT NULL DEFAULT 'simulated',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (time, meter_id, simulation_id)
                )
            """))

    def persist(self, reading: SimulatedReading) -> bool:
        """Insert one reading idempotently against the registered meter."""
        self.ensure_table()
        with self.engine.begin() as connection:
            meter_exists = connection.execute(text(
                "SELECT 1 FROM meters WHERE meter_code = :meter_code"
            ), {"meter_code": reading.meter_code}).scalar_one_or_none()
            if meter_exists is None:
                raise ValueError(f"Meter is not registered: {reading.meter_code}")
            result = connection.execute(text("""
                INSERT INTO simulated_telemetry (
                    time, meter_id, simulation_id, real_power_kw, current_avg_a,
                    voltage_ln_avg_v, voltage_ll_avg_v, frequency_hz,
                    power_factor_pct, apparent_power_kva, known_anomaly,
                    simulation_status
                )
                SELECT :time, meter_id, :simulation_id, :real_power_kw, :current_avg_a,
                       :voltage_ln_avg_v, :voltage_ll_avg_v, :frequency_hz,
                       :power_factor_pct, :apparent_power_kva, :known_anomaly,
                       :simulation_status
                FROM meters
                WHERE meter_code = :meter_code
                ON CONFLICT (time, meter_id, simulation_id) DO NOTHING
            """), {
                "time": reading.time,
                "meter_code": reading.meter_code,
                "simulation_id": reading.simulation_id,
                "real_power_kw": reading.real_power_kw,
                "current_avg_a": reading.current_avg_a,
                "voltage_ln_avg_v": reading.voltage_ln_avg_v,
                "voltage_ll_avg_v": reading.voltage_ll_avg_v,
                "frequency_hz": reading.frequency_hz,
                "power_factor_pct": reading.power_factor_pct,
                "apparent_power_kva": reading.apparent_power_kva,
                "known_anomaly": reading.known_anomaly,
                "simulation_status": reading.simulation_status,
            })
            return bool(result.rowcount)


@dataclass
class SimulatorStats:
    """Thread-safe service counters exposed to API/dashboard clients."""

    status: str = "stopped"
    generated: int = 0
    persisted: int = 0
    failed: int = 0
    injected_anomalies: int = 0
    last_reading: SimulatedReading | None = None
    error: str | None = None


class LiveMeterSimulator:
    """Start, stop, and restart a reproducible one-minute meter stream."""

    def __init__(
        self,
        config: SimulationConfig | None = None,
        on_reading: ReadingCallback | None = None,
    ) -> None:
        """Create a stopped simulator with an optional persistence callback."""
        self.config = config or SimulationConfig()
        self.on_reading = on_reading
        self._stats = SimulatorStats()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def stats(self) -> SimulatorStats:
        """Return a snapshot of current simulator counters."""
        with self._lock:
            return SimulatorStats(**self._stats.__dict__)

    def start(self) -> bool:
        """Start one worker if stopped; return false when already running."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._stats = SimulatorStats(status="running")
            self._thread = threading.Thread(
                target=self._run,
                name=f"live-meter-{self.config.simulation_id}",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> bool:
        """Stop the worker and wait briefly for its current callback to finish."""
        with self._lock:
            thread = self._thread
            if not thread or not thread.is_alive():
                self._stats.status = "stopped"
                return False
            self._stop_event.set()
        thread.join(timeout=max(2.0, self.config.interval_seconds + 1.0))
        with self._lock:
            self._stats.status = "stopped"
        return True

    def _run(self) -> None:
        """Generate readings until stopped or the configured duration ends."""
        rng = np.random.default_rng(self.config.random_seed)
        current_time = self.config.start_time.astimezone(timezone.utc)
        for minute in range(self.config.duration_minutes):
            if self._stop_event.is_set():
                break
            reading = self._generate_reading(current_time, minute, rng)
            try:
                persisted = self.on_reading(reading) if self.on_reading else None
                with self._lock:
                    self._stats.generated += 1
                    self._stats.persisted += int(persisted is not False)
                    self._stats.injected_anomalies += int(reading.known_anomaly)
                    self._stats.last_reading = reading
                    self._stats.error = None
            except Exception as exc:  # noqa: BLE001 - worker must stay observable
                LOGGER.exception("Simulated reading callback failed.")
                with self._lock:
                    self._stats.generated += 1
                    self._stats.failed += 1
                    self._stats.error = str(exc)
            current_time += timedelta(minutes=1)
            if self._stop_event.wait(self.config.interval_seconds):
                break
        with self._lock:
            if self._stats.status == "running":
                self._stats.status = "completed"

    def _generate_reading(
        self, timestamp: datetime, minute: int, rng: np.random.Generator
    ) -> SimulatedReading:
        """Generate one plausible reading and apply the configured event."""
        hour = timestamp.hour + timestamp.minute / 60.0
        workday = timestamp.weekday() < 5 and 7 <= hour < 19
        baseline = 118.0 + 18.0 * np.sin(2 * np.pi * (hour - 7) / 24)
        baseline += 14.0 if workday else -4.0
        power = max(35.0, baseline + rng.normal(0.0, 2.0))
        power_factor = float(np.clip(91.0 + rng.normal(0.0, 1.0), 70.0, 99.0))
        voltage_ln = 230.0 + rng.normal(0.0, 1.0)
        voltage_ll = 400.0 + rng.normal(0.0, 1.5)
        known_anomaly = (
            self.config.abnormal_start_minute
            <= minute
            < self.config.abnormal_start_minute + self.config.abnormal_duration_minutes
        )
        if known_anomaly:
            power = power * self.config.abnormal_multiplier + 35.0
            power_factor = max(45.0, power_factor - 20.0)
            voltage_ln -= 7.0
            voltage_ll -= 12.0
        apparent = power / max(power_factor / 100.0, 1e-6)
        return SimulatedReading(
            time=timestamp,
            meter_code=self.config.meter_code,
            simulation_id=self.config.simulation_id,
            real_power_kw=float(power),
            current_avg_a=float(power * 2.65 + rng.normal(0.0, 1.0)),
            voltage_ln_avg_v=float(voltage_ln),
            voltage_ll_avg_v=float(voltage_ll),
            frequency_hz=float(50.0 + rng.normal(0.0, 0.02)),
            power_factor_pct=float(power_factor),
            apparent_power_kva=float(apparent),
            known_anomaly=known_anomaly,
        )
