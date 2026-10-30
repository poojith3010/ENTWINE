"""Tests for the model-compatible data adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from validation.sim_to_model_adapter import (
    MIN_15MIN_ROWS,
    AdapterResult,
    adapt_simulated_telemetry,
    resample_to_15min,
    validate_adapter_frame,
)


def _generate_synthetic_1min(
    minutes: int = 120,
    with_duplicates: bool = False,
    with_nans: bool = False,
    with_anomaly: bool = True,
) -> pd.DataFrame:
    """Generate sample one-minute readings dataframe."""
    start = pd.Timestamp("2026-10-30 00:00:00", tz="UTC")
    idx = pd.date_range(start=start, periods=minutes, freq="1min", tz="UTC")
    rng = np.random.default_rng(42)

    power = 100.0 + rng.normal(0, 2.0, len(idx))
    if with_nans:
        power[5:10] = np.nan

    current = power * 2.65
    voltage_ln = np.full(len(idx), 230.0)
    voltage_ll = np.full(len(idx), 400.0)
    frequency = np.full(len(idx), 50.0)
    power_factor = np.full(len(idx), 92.0)
    apparent_power = power / (power_factor / 100.0)
    known_anomaly = np.zeros(len(idx), dtype=bool)
    if with_anomaly and minutes >= 30:
        known_anomaly[15:25] = True

    df = pd.DataFrame(
        {
            "real_power_kw": power,
            "current_avg_a": current,
            "voltage_ln_avg_v": voltage_ln,
            "voltage_ll_avg_v": voltage_ll,
            "frequency_hz": frequency,
            "power_factor_pct": power_factor,
            "apparent_power_kva": apparent_power,
            "known_anomaly": known_anomaly,
        },
        index=idx,
    )
    df.index.name = "time"

    if with_duplicates and len(df) > 5:
        # Re-insert row 2 with identical timestamp
        dup_row = df.iloc[[2]]
        df = pd.concat([df.iloc[:3], dup_row, df.iloc[3:]])

    return df


def test_adapter_resamples_to_15min_cadence() -> None:
    """Verify that one-minute data is properly aggregated to 15-minute buckets."""
    df_1min = _generate_synthetic_1min(minutes=60)
    df_15 = resample_to_15min(df_1min)

    assert len(df_15) == 4
    diffs = df_15.index.to_series().diff().dropna().unique()
    assert len(diffs) == 1
    assert diffs[0] == pd.Timedelta(minutes=15)
    assert df_15.index.tz == timezone.utc
    assert "real_power_kw" in df_15.columns
    # Check synthetic phase columns
    assert "real_power_a_kw" in df_15.columns
    assert "current_a_a" in df_15.columns


def test_adapter_preserves_ground_truth_separately() -> None:
    """Verify ground truth is extracted and not passed to model frame."""
    df_1min = _generate_synthetic_1min(minutes=60, with_anomaly=True)
    res = adapt_simulated_telemetry(df_1min)

    assert res.frame is not None
    assert "known_anomaly" not in res.frame.columns
    assert res.ground_truth_preserved is not None
    assert isinstance(res.ground_truth_preserved, pd.Series)
    # The anomaly is at 15..24 min (so bucket 00:15:00 has known_anomaly=True)
    assert bool(res.ground_truth_preserved.any()) is True


def test_adapter_handles_duplicates_and_nans() -> None:
    """Verify deduplication and invalid value handling."""
    df_1min = _generate_synthetic_1min(minutes=60, with_duplicates=True, with_nans=True)
    res = adapt_simulated_telemetry(df_1min)

    assert res.dropped_duplicates == 1
    assert res.dropped_invalid == 5
    assert res.frame is not None


def test_adapter_insufficient_history_status() -> None:
    """Verify insufficient_history status when rows < MIN_15MIN_ROWS (97)."""
    df_1min = _generate_synthetic_1min(minutes=30)
    res = adapt_simulated_telemetry(df_1min)

    assert res.status == "insufficient_history"
    assert res.n_15min_rows == 2
    assert res.is_ready is False
    assert "Only 2 15-min rows available" in str(res.error)


def test_adapter_ready_when_sufficient_history() -> None:
    """Verify ready status when rows >= MIN_15MIN_ROWS."""
    # 97 intervals * 15 minutes = 1455 minutes
    df_1min = _generate_synthetic_1min(minutes=1460)
    res = adapt_simulated_telemetry(df_1min)

    assert res.status == "ready"
    assert res.n_15min_rows >= MIN_15MIN_ROWS
    assert res.is_ready is True
    assert res.error is None


def test_validate_adapter_frame_detects_cadence_drift() -> None:
    """Verify warning when there are gaps > 30 minutes."""
    idx = [
        pd.Timestamp("2026-10-30 00:00:00", tz="UTC"),
        pd.Timestamp("2026-10-30 00:15:00", tz="UTC"),
        pd.Timestamp("2026-10-30 01:15:00", tz="UTC"),  # 60 min gap!
    ]
    df = pd.DataFrame({"real_power_kw": [100.0, 105.0, 102.0]}, index=pd.DatetimeIndex(idx))
    warnings = validate_adapter_frame(df)
    assert any("gap(s) > 30 min detected" in w for w in warnings)

