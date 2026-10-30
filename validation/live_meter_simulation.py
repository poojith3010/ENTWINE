"""Simulate live meter intervals and validate the existing CAFA/GrCF pipeline.

The simulator creates a realistic 15-minute telemetry stream with one known
operating event. The existing CAFA and GrCF implementations are called
unchanged; this module only supplies input data and interprets their outputs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from models.cafa_detector import run_cafa_detection
from models.grcf_explainer import FEATURE_COLUMNS, explain_anomaly

SIMULATION_SEED: Final[int] = 20261030
INTERVAL_SECONDS: Final[int] = 15 * 60
INTERVALS_PER_DAY: Final[int] = 24 * 60 * 60 // INTERVAL_SECONDS
DEFAULT_DAYS: Final[int] = 14
ANOMALY_LENGTH: Final[int] = 16


@dataclass(frozen=True)
class SimulationSpec:
    """Parameters describing the injected live-meter event."""

    start: pd.Timestamp
    end: pd.Timestamp
    affected_intervals: int
    expected_power_multiplier: float
    expected_power_offset_kw: float
    expected_power_factor_drop_pct: float
    interval_seconds: int


@dataclass(frozen=True)
class LiveValidationResult:
    """Auditable outputs from one simulated CAFA/GrCF validation run."""

    telemetry: pd.DataFrame
    detection: pd.DataFrame
    counterfactual: np.ndarray
    simulation: SimulationSpec
    target_detection_positions: tuple[int, ...]
    flagged_target_intervals: int
    target_recall: float
    target_precision: float
    false_positive_count: int
    observed_power_kw: float
    counterfactual_power_kw: float
    observed_current_a: float
    counterfactual_current_a: float
    observed_power_factor_pct: float
    counterfactual_power_factor_pct: float
    cafa_explanation: str
    grcf_explanation: str


def simulate_live_meter(
    days: int = DEFAULT_DAYS,
    start: datetime | str = "2026-10-01T00:00:00Z",
    seed: int = SIMULATION_SEED,
) -> tuple[pd.DataFrame, SimulationSpec]:
    """Create 15-minute telemetry with a known four-hour operating event.

    The returned DataFrame is shaped like the model connector output. The
    private ``known_anomaly`` column is validation metadata and is ignored by
    CAFA's feature engineering. CAFA treats each row as one interval and its
    lag constants retain their existing algorithm semantics.
    """
    if days < 3:
        raise ValueError("At least 3 days are required for the holdout demonstration.")

    rng = np.random.default_rng(seed)
    index = pd.date_range(
        start=start,
        periods=days * INTERVALS_PER_DAY,
        freq=f"{INTERVAL_SECONDS}s",
        tz="UTC",
    )
    slot = np.arange(len(index)) % INTERVALS_PER_DAY
    hour = slot / (3600 / INTERVAL_SECONDS)
    weekday = index.weekday.to_numpy()
    workday = ((weekday < 5) & (hour >= 7) & (hour < 19)).astype(float)
    daily_load = 22.0 * np.sin(2 * np.pi * (hour - 7) / 24.0)
    evening_load = 10.0 * np.sin(2 * np.pi * (hour - 17) / 24.0)
    weekly_load = np.where(weekday < 5, 1.0, 0.82)
    power = (118.0 + daily_load + evening_load) * weekly_load + 16.0 * workday
    power += rng.normal(0.0, 2.0, len(index))
    power = np.maximum(power, 35.0)

    current = power * 2.65 + rng.normal(0.0, 2.0, len(index))
    power_factor = np.clip(91.0 + rng.normal(0.0, 1.0, len(index)), 70.0, 99.0)
    frequency = 50.0 + rng.normal(0.0, 0.025, len(index))
    voltage_ln = 230.0 + rng.normal(0.0, 1.2, len(index))
    voltage_ll = 400.0 + rng.normal(0.0, 1.8, len(index))
    apparent_power = power / (power_factor / 100.0)

    anomaly_start = int(len(index) * 0.84)
    anomaly_start -= anomaly_start % ANOMALY_LENGTH
    anomaly_end = anomaly_start + ANOMALY_LENGTH
    anomaly_slice = slice(anomaly_start, anomaly_end)
    power[anomaly_slice] = power[anomaly_slice] * 1.62 + 48.0
    # Keep current near its normal context so CAFA sees a contextual
    # power-residual event rather than a fully self-consistent load increase.
    current[anomaly_slice] = current[anomaly_slice] * 1.05
    power_factor[anomaly_slice] = np.clip(power_factor[anomaly_slice] - 22.0, 45.0, 99.0)
    voltage_ln[anomaly_slice] -= 8.0
    voltage_ll[anomaly_slice] -= 14.0
    apparent_power[anomaly_slice] = power[anomaly_slice] / (power_factor[anomaly_slice] / 100.0)

    known_anomaly = np.zeros(len(index), dtype=bool)
    known_anomaly[anomaly_slice] = True
    telemetry = pd.DataFrame(
        {
            "real_power_kw": power,
            "current_avg_a": current,
            "frequency_hz": frequency,
            "power_factor_pct": power_factor,
            "voltage_ln_avg_v": voltage_ln,
            "voltage_ll_avg_v": voltage_ll,
            "apparent_power_kva": apparent_power,
            "real_power_a_kw": power * 0.34,
            "real_power_b_kw": power * 0.33,
            "real_power_c_kw": power * 0.33,
            "current_a_a": current * 0.34,
            "current_b_a": current * 0.33,
            "current_c_a": current * 0.33,
            "known_anomaly": known_anomaly,
        },
        index=index,
    )
    spec = SimulationSpec(
        start=index[anomaly_start],
        end=index[anomaly_end - 1],
        affected_intervals=ANOMALY_LENGTH,
        expected_power_multiplier=1.62,
        expected_power_offset_kw=48.0,
        expected_power_factor_drop_pct=22.0,
        interval_seconds=INTERVAL_SECONDS,
    )
    return telemetry, spec


def _target_positions(
    detection: pd.DataFrame, simulation: SimulationSpec
) -> tuple[int, ...]:
    """Map the known event timestamps to CAFA output positions."""
    return tuple(
        position
        for position, timestamp in enumerate(detection.index)
        if simulation.start <= timestamp <= simulation.end
    )


def _mean_feature(
    frame: pd.DataFrame, column: str, positions: tuple[int, ...]
) -> float:
    if not positions or column not in frame.columns:
        return float("nan")
    return float(frame.iloc[list(positions)][column].mean())


def run_live_validation(
    telemetry: pd.DataFrame | None = None,
    simulation: SimulationSpec | None = None,
) -> LiveValidationResult:
    """Run unchanged CAFA and GrCF against simulated meter telemetry."""
    if telemetry is None:
        telemetry, simulation = simulate_live_meter()
    if simulation is None:
        raise ValueError("simulation metadata is required with custom telemetry")

    detection, _summary = run_cafa_detection(telemetry.drop(columns=["known_anomaly"], errors="ignore"))
    positions = _target_positions(detection, simulation)
    if not positions:
        raise ValueError("The simulated event did not reach the CAFA output window.")

    flags = detection["cafa_if_flag"].to_numpy(dtype=bool)
    flagged = int(flags[list(positions)].sum())
    normal_mask = ~flags
    counterfactual = explain_anomaly(detection, normal_mask, positions[0])
    total_flagged = int(flags.sum())

    observed = telemetry.loc[simulation.start:simulation.end]
    observed_power = float(observed["real_power_kw"].mean())
    observed_current = float(observed["current_avg_a"].mean())
    observed_pf = float(observed["power_factor_pct"].mean())
    cf_power = float(counterfactual[:, FEATURE_COLUMNS.index("real_power_kw")].mean())
    cf_current = float(counterfactual[:, FEATURE_COLUMNS.index("current_avg_a")].mean())
    cf_pf = float(counterfactual[:, FEATURE_COLUMNS.index("power_factor_pct")].mean())
    cafa_explanation = explain_cafa_result(
        detection, positions, flagged, total_flagged
    )
    grcf_explanation = explain_grcf_result(
        observed_power,
        cf_power,
        observed_current,
        cf_current,
        observed_pf,
        cf_pf,
    )

    return LiveValidationResult(
        telemetry=telemetry,
        detection=detection,
        counterfactual=counterfactual,
        simulation=simulation,
        target_detection_positions=positions,
        flagged_target_intervals=flagged,
        target_recall=flagged / len(positions),
        target_precision=flagged / total_flagged if total_flagged else 0.0,
        false_positive_count=total_flagged - flagged,
        observed_power_kw=observed_power,
        counterfactual_power_kw=cf_power,
        observed_current_a=observed_current,
        counterfactual_current_a=cf_current,
        observed_power_factor_pct=observed_pf,
        counterfactual_power_factor_pct=cf_pf,
        cafa_explanation=cafa_explanation,
        grcf_explanation=grcf_explanation,
    )


def explain_cafa_result(
    detection: pd.DataFrame,
    target_positions: tuple[int, ...],
    flagged_target_intervals: int,
    total_flagged: int,
) -> str:
    """Explain CAFA flags using only measurable detector outputs."""
    target = detection.iloc[list(target_positions)]
    residual = float(target["residual_kw"].mean())
    predicted = float(target["predicted_power_kw"].mean())
    actual = float(target["real_power_kw"].mean())
    return (
        f"CAFA compared actual power ({actual:.1f} kW) with contextual expected "
        f"power ({predicted:.1f} kW). The average residual was {residual:+.1f} kW. "
        f"It flagged {flagged_target_intervals} of {len(target_positions)} known "
        f"abnormal intervals and {total_flagged - flagged_target_intervals} "
        "additional intervals outside the injected event."
    )


def explain_grcf_result(
    observed_power: float,
    counterfactual_power: float,
    observed_current: float,
    counterfactual_current: float,
    observed_pf: float,
    counterfactual_pf: float,
) -> str:
    """Explain GrCF as feature-by-feature observed-versus-alternative changes."""
    return (
        f"GrCF generated a 16-step alternative trajectory. Compared with the "
        f"known event, power changed by {counterfactual_power - observed_power:+.1f} kW, "
        f"current by {counterfactual_current - observed_current:+.1f} A, and "
        f"power factor by {counterfactual_pf - observed_pf:+.1f} percentage points. "
        "This is a model-generated alternative, not a confirmed physical diagnosis."
    )


def format_validation_report(result: LiveValidationResult) -> str:
    """Return a plain-English validation summary for operators and reviewers."""
    return f"""## Simulated live-meter validation

**Known event:** {result.simulation.start.isoformat()} to {result.simulation.end.isoformat()} ({result.simulation.affected_intervals} intervals at {result.simulation.interval_seconds} seconds each)

### CAFA
- CAFA received **{len(result.telemetry):,} simulated 15-minute readings**.
- It flagged **{result.flagged_target_intervals}/{result.simulation.affected_intervals}** injected event intervals (**{result.target_recall:.0%} recall**).
- Among all CAFA flags, **{result.target_precision:.0%}** belonged to the injected event (**precision against this synthetic event**).
- It also flagged **{result.false_positive_count}** intervals outside the injected event.
- **Rule-based explanation:** {result.cafa_explanation}
- This is a validation experiment with a known synthetic event, not proof of real equipment failure.

### GrCF
- Output shape: **{result.counterfactual.shape[0]} time steps x {result.counterfactual.shape[1]} features**.
- Observed event power: **{result.observed_power_kw:.1f} kW** -> counterfactual: **{result.counterfactual_power_kw:.1f} kW**.
- Observed event current: **{result.observed_current_a:.1f} A** -> counterfactual: **{result.counterfactual_current_a:.1f} A**.
- Observed event power factor: **{result.observed_power_factor_pct:.1f}%** -> counterfactual: **{result.counterfactual_power_factor_pct:.1f}%**.
- **Rule-based explanation:** {result.grcf_explanation}

**Interpretation:** CAFA is being tested against a deliberately injected high-load/low-power-factor event. GrCF supplies a model-generated alternative trajectory; it is not a diagnosis or proof of the physical cause.

**Cadence note:** the simulator uses the same 15-minute cadence as the
historical meter data. The unchanged algorithms operate on rows, so CAFA's lag
and rolling constants remain measured in intervals.
"""


def build_validation_figure(result: LiveValidationResult) -> go.Figure:
    """Plot observed simulated power and the GrCF sequence around the event."""
    figure = go.Figure()
    event_window = result.telemetry.loc[
        result.simulation.start - pd.Timedelta(hours=8):
        result.simulation.end + pd.Timedelta(hours=8)
    ]
    figure.add_trace(
        go.Scatter(
            x=event_window.index,
            y=event_window["real_power_kw"],
            mode="lines",
            name="Simulated meter power",
            line=dict(color="#1e2761", width=2),
        )
    )
    detected = result.detection[
        (result.detection["cafa_if_flag"] == 1)
        & (result.detection.index >= event_window.index.min())
        & (result.detection.index <= event_window.index.max())
    ]
    figure.add_trace(
        go.Scatter(
            x=detected.index,
            y=event_window.reindex(detected.index)["real_power_kw"],
            mode="markers",
            name="CAFA flagged interval",
            marker=dict(color="#d64545", size=7, symbol="x"),
            hovertemplate="CAFA flagged<br>%{x}<br>%{y:.1f} kW<extra></extra>",
        )
    )
    cf_index = pd.date_range(
        start=result.simulation.start,
        periods=result.counterfactual.shape[0],
        freq=f"{result.simulation.interval_seconds}s",
        tz="UTC",
    )
    figure.add_trace(
        go.Scatter(
            x=cf_index,
            y=result.counterfactual[:, FEATURE_COLUMNS.index("real_power_kw")],
            mode="lines+markers",
            name="GrCF counterfactual",
            line=dict(color="#1f9d6b", dash="dash", width=2),
        )
    )
    figure.add_vrect(
        x0=result.simulation.start,
        x1=result.simulation.end + pd.Timedelta(minutes=15),
        fillcolor="#d64545",
        opacity=0.12,
        line_width=0,
        annotation_text="Injected event",
    )
    figure.update_layout(
        title="Simulated Live Meter: Observed Power vs GrCF Alternative",
        xaxis_title="Time (UTC)",
        yaxis_title="Real power (kW)",
        hovermode="x unified",
        template="plotly_white",
    )
    return figure


def validation_table(result: LiveValidationResult) -> list[list[str | float]]:
    """Return an operator-readable observed/counterfactual comparison table."""
    return [
        ["Real power (kW)", round(result.observed_power_kw, 2), round(result.counterfactual_power_kw, 2)],
        ["Current (A)", round(result.observed_current_a, 2), round(result.counterfactual_current_a, 2)],
        ["Power factor (%)", round(result.observed_power_factor_pct, 2), round(result.counterfactual_power_factor_pct, 2)],
        ["CAFA flagged target intervals", result.flagged_target_intervals, result.simulation.affected_intervals],
    ]


def main() -> int:
    """Run the simulation and print the validation report."""
    parser = argparse.ArgumentParser(description="Validate CAFA and GrCF with simulated live meter data.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()
    result = run_live_validation(*simulate_live_meter(days=args.days))
    print(format_validation_report(result))
    print(f"Counterfactual finite: {bool(np.isfinite(result.counterfactual).all())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
