"""Headless CAFA contextual anomaly detection for the A Block meter.

The module converts live TimescaleDB telemetry into engineered features,
trains the fallback contextual XGBoost regressor from the CAFA research
notebook, and emits context-aware Isolation Forest anomaly flags. It does not
consume ground-truth labels and does not produce plots or fairness tables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from xgboost import XGBRegressor

from models.twin_data_connector import get_building_state_df

LOGGER: Final[logging.Logger] = logging.getLogger("models.cafa_detector")
TARGET: Final[str] = "real_power_kw"
METER_CODE: Final[str] = "PH-A-MAIN"
CONTAMINATION: Final[float] = 0.033
SEED: Final[int] = 42
LAGS: Final[tuple[int, ...]] = (1, 2, 4, 8, 16, 96)
ROLLING_WINDOWS: Final[dict[str, int]] = {
    "power_roll_4h": 16,
    "power_roll_24h": 96,
    "power_roll_7d": 672,
}
TEMPORAL_FEATURES: Final[tuple[str, ...]] = (
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
    "month_sin",
    "month_cos",
    "dayofyear_sin",
    "dayofyear_cos",
    "is_weekend",
)
ELECTRICAL_FEATURES: Final[tuple[str, ...]] = (
    "frequency_hz",
    "voltage_ln_avg_v",
    "voltage_ll_avg_v",
    "current_avg_a",
    "current_a_a",
    "current_b_a",
    "current_c_a",
    "power_factor_pct",
    "phase_imbalance",
)


@dataclass(frozen=True)
class DetectionSummary:
    """Aggregate metrics emitted by one CAFA inference run."""

    records_processed: int
    mean_absolute_error_kw: float
    anomalies_flagged: int



def _empty_output() -> pd.DataFrame:
    """Return an empty result with the detector's stable output columns."""
    return pd.DataFrame(
        columns=[
            "predicted_power_kw",
            "residual_kw",
            "abs_residual_kw",
            "residual_rolling_4h",
            "residual_rolling_24h",
            "residual_zscore",
            "cafa_if_flag",
        ],
        index=pd.DatetimeIndex([], name="time"),
    )



def engineer_features(state_df: pd.DataFrame) -> pd.DataFrame:
    """Build CAFA temporal, lag, rolling, and phase features.

    Args:
        state_df: UTC-indexed wide telemetry returned by the data connector.

    Returns:
        A chronologically sorted feature DataFrame. Ground-truth columns such
        as ``anomaly`` and ``anomaly_source`` are ignored if present.

    Raises:
        ValueError: If the target signal or a usable timezone-aware time index
            is missing.
    """
    if TARGET not in state_df.columns:
        raise ValueError(f"Required target column is missing: {TARGET}")
    if not isinstance(state_df.index, pd.DatetimeIndex):
        raise ValueError("State data must use a DatetimeIndex named 'time'.")

    frame = state_df.copy()
    frame.drop(columns=["anomaly", "anomaly_source"], errors="ignore", inplace=True)
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    frame.sort_index(inplace=True)
    frame = frame[~frame.index.duplicated(keep="first")]

    frame[TARGET] = pd.to_numeric(frame[TARGET], errors="coerce")
    frame.dropna(subset=[TARGET], inplace=True)
    if frame.empty:
        return frame

    frame["hour"] = frame.index.hour
    frame["weekday"] = frame.index.weekday
    frame["month"] = frame.index.month
    frame["dayofyear"] = frame.index.dayofyear
    for column, period in (
        ("hour", 24),
        ("weekday", 7),
        ("month", 12),
        ("dayofyear", 365),
    ):
        frame[f"{column}_sin"] = np.sin(2 * np.pi * frame[column] / period)
        frame[f"{column}_cos"] = np.cos(2 * np.pi * frame[column] / period)
    frame["is_weekend"] = (frame["weekday"] >= 5).astype(int)

    for lag in LAGS:
        frame[f"power_lag_{lag}"] = frame[TARGET].shift(lag)
    for name, window in ROLLING_WINDOWS.items():
        frame[name] = frame[TARGET].shift(1).rolling(window, min_periods=1).mean()

    phase_columns = [
        column
        for column in (
            "real_power_a_kw",
            "real_power_b_kw",
            "real_power_c_kw",
        )
        if column in frame.columns
    ]
    if len(phase_columns) == 3:
        phase_mean = frame[phase_columns].mean(axis=1).abs()
        frame["phase_imbalance"] = frame[phase_columns].std(axis=1) / (
            phase_mean + 1e-6
        )
    else:
        frame["phase_imbalance"] = np.nan

    frame.dropna(subset=[f"power_lag_{lag}" for lag in LAGS], inplace=True)
    frame.drop(columns=["hour", "weekday", "month", "dayofyear"], inplace=True)
    return frame



def _context_columns(frame: pd.DataFrame) -> list[str]:
    """Return available model features in the defined CAFA order."""
    lag_columns = [f"power_lag_{lag}" for lag in LAGS]
    return [
        column
        for column in (*TEMPORAL_FEATURES, *ELECTRICAL_FEATURES, *lag_columns)
        if column in frame.columns
    ] + [
        column for column in ROLLING_WINDOWS if column in frame.columns
    ]



def _fill_model_features(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Fill missing feature values using training-independent column medians."""
    features = frame[columns].apply(pd.to_numeric, errors="coerce")
    medians = features.median().fillna(0.0)
    return features.fillna(medians).replace([np.inf, -np.inf], 0.0).fillna(0.0)



def _fit_context_model(
    frame: pd.DataFrame, columns: list[str]
) -> tuple[XGBRegressor, float]:
    """Fit the fallback XGBoost model and return it with holdout MAE."""
    features = _fill_model_features(frame, columns)
    target = frame[TARGET].to_numpy(dtype=float)
    split = max(1, int(len(frame) * 0.8))
    if split >= len(frame):
        split = len(frame) - 1
    model = XGBRegressor(
        n_estimators=700,
        learning_rate=0.03,
        max_depth=7,
        subsample=0.85,
        colsample_bytree=0.85,
        tree_method="hist",
        random_state=SEED,
        n_jobs=-1,
        objective="reg:squarederror",
    )
    model.fit(features.iloc[:split], target[:split], verbose=False)
    validation_prediction = model.predict(features.iloc[split:])
    mae = float(np.mean(np.abs(target[split:] - validation_prediction)))
    return model, mae



def run_cafa_detection(state_df: pd.DataFrame) -> tuple[pd.DataFrame, DetectionSummary]:
    """Run contextual regression and Isolation Forest on state telemetry.

    Args:
        state_df: Wide, UTC-indexed telemetry for one registered meter.

    Returns:
        A tuple containing the engineered output DataFrame and aggregate
        records/MAE/anomaly counts. No ground-truth labels are required.

    Raises:
        ValueError: If telemetry is structurally invalid or too short for the
            required 96-interval history.
    """
    frame = engineer_features(state_df)
    if frame.empty:
        return _empty_output(), DetectionSummary(0, float("nan"), 0)

    columns = _context_columns(frame)
    if not columns:
        raise ValueError("No usable CAFA context features are available.")
    if len(frame) < 2:
        raise ValueError("At least two engineered records are required.")

    model, mae = _fit_context_model(frame, columns)
    features = _fill_model_features(frame, columns)
    frame["predicted_power_kw"] = model.predict(features)
    frame["residual_kw"] = frame[TARGET] - frame["predicted_power_kw"]
    frame["abs_residual_kw"] = frame["residual_kw"].abs()
    frame["residual_rolling_4h"] = frame["residual_kw"].rolling(
        16, min_periods=1
    ).mean()
    frame["residual_rolling_24h"] = frame["residual_kw"].rolling(
        96, min_periods=4
    ).mean()
    residual_mean = frame["residual_kw"].rolling(96, min_periods=4).mean()
    residual_std = frame["residual_kw"].rolling(96, min_periods=4).std()
    frame["residual_zscore"] = (
        (frame["residual_kw"] - residual_mean) / (residual_std + 1e-6)
    )

    detector_features = frame[
        [
            "residual_kw",
            "abs_residual_kw",
            "residual_rolling_4h",
            "residual_rolling_24h",
            "residual_zscore",
        ]
    ].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    scaled_features = StandardScaler().fit_transform(detector_features)
    detector = IsolationForest(
        n_estimators=200,
        contamination=CONTAMINATION,
        random_state=SEED,
        n_jobs=-1,
    )
    frame["cafa_if_flag"] = (detector.fit_predict(scaled_features) == -1).astype(
        int
    )

    summary = DetectionSummary(
        records_processed=len(frame),
        mean_absolute_error_kw=mae,
        anomalies_flagged=int(frame["cafa_if_flag"].sum()),
    )
    return frame, summary



def detect_meter(meter_code: str = METER_CODE) -> pd.DataFrame:
    """Fetch one meter from TimescaleDB and return CAFA inference output."""
    try:
        state_df = get_building_state_df(meter_code)
        output, summary = run_cafa_detection(state_df)
    except (ConnectionError, OSError, ValueError) as exc:
        LOGGER.error("CAFA inference unavailable for %s: %s", meter_code, exc)
        return _empty_output()

    LOGGER.info("Records processed: %d", summary.records_processed)
    LOGGER.info("XGBoost MAE: %.6f kW", summary.mean_absolute_error_kw)
    LOGGER.info("CAFA anomalies flagged: %d", summary.anomalies_flagged)
    return output



def main() -> int:
    """Run the default A Block CAFA inference pipeline."""
    configure_logging()
    detect_meter(METER_CODE)
    return 0



def configure_logging() -> None:
    """Configure concise console logging for headless model execution."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
