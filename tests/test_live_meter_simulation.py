"""Tests for the isolated simulated live-meter validation layer."""

import numpy as np

from validation.live_meter_simulation import (
    ANOMALY_LENGTH,
    LiveValidationResult,
    format_validation_report,
    simulate_live_meter,
)


def test_simulation_has_a_known_four_hour_event_at_15_minutes() -> None:
    telemetry, specification = simulate_live_meter(days=14)

    assert len(telemetry) == 14 * 24 * 4
    assert telemetry.index.to_series().diff().dropna().unique().tolist() == [
        np.timedelta64(15, "m")
    ]
    assert int(telemetry["known_anomaly"].sum()) == ANOMALY_LENGTH
    assert specification.affected_intervals == ANOMALY_LENGTH
    assert telemetry.index.is_monotonic_increasing
    assert telemetry.index.tz is not None


def test_simulation_event_is_measurably_different() -> None:
    telemetry, specification = simulate_live_meter(days=14)
    normal = telemetry.loc[~telemetry["known_anomaly"]]
    event = telemetry.loc[telemetry["known_anomaly"]]

    assert event["real_power_kw"].mean() > normal["real_power_kw"].mean()
    assert event["current_avg_a"].mean() > normal["current_avg_a"].mean()
    assert event["power_factor_pct"].mean() < normal["power_factor_pct"].mean()
    assert np.isfinite(event["real_power_kw"]).all()
    assert specification.start < specification.end


def test_report_describes_validation_boundary() -> None:
    telemetry, specification = simulate_live_meter(days=14)
    result = LiveValidationResult(
        telemetry=telemetry,
        detection=telemetry.iloc[:0],
        counterfactual=np.zeros((16, 10)),
        simulation=specification,
        target_detection_positions=(),
        flagged_target_intervals=0,
        target_recall=0.0,
        target_precision=0.0,
        false_positive_count=0,
        observed_power_kw=0.0,
        counterfactual_power_kw=0.0,
        observed_current_a=0.0,
        counterfactual_current_a=0.0,
        observed_power_factor_pct=0.0,
        counterfactual_power_factor_pct=0.0,
        cafa_explanation="CAFA explanation",
        grcf_explanation="GrCF explanation",
    )
    assert "known synthetic event" in format_validation_report(result)
