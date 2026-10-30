"""Adapter: one-minute simulated telemetry -> 15-minute model-compatible DataFrame.

The CAFA detector and GrCF explainer use row-based lag and rolling-window
constants calibrated for 15-minute interval data:

    power_lag_96   = lag-96 rows  = 24 h  @ 15-min cadence
    power_roll_4h  = 16-row mean  = 4 h   @ 15-min cadence
    power_roll_24h = 96-row mean  = 24 h  @ 15-min cadence
    power_roll_7d  = 672-row mean = 7 d   @ 15-min cadence

Feeding raw one-minute readings to CAFA would silently corrupt all of these
temporal semantics.  This adapter performs explicit, documented resampling
before the model sees any data and refuses to return a frame that would
produce meaningless features.

Public API
----------
``resample_to_15min(df_1min)``         -> 15-min wide DataFrame or raises
``validate_adapter_frame(df_15min)``   -> list[str] of warnings (empty = ok)
``AdapterResult``                      -> typed output with status + warnings
``adapt_simulated_telemetry(df_1min)`` -> AdapterResult (never raises)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final

import numpy as np
import pandas as pd

LOGGER: Final[logging.Logger] = logging.getLogger("validation.sim_to_model_adapter")

# ── Constants ─────────────────────────────────────────────────────────────────

TARGET_FREQ: Final[str] = "15min"
# CAFA needs at least 96 rows for power_lag_96 dropna to keep *any* rows.
# +1 so at least one row survives after the dropna.
MIN_15MIN_ROWS: Final[int] = 97

# Columns produced by LiveMeterSimulator that map directly to CAFA/GrCF inputs
DIRECT_COLUMNS: Final[tuple[str, ...]] = (
    "real_power_kw",
    "current_avg_a",
    "voltage_ln_avg_v",
    "voltage_ll_avg_v",
    "frequency_hz",
    "power_factor_pct",
    "apparent_power_kva",
)

# Additional columns present in the 15-min simulation that CAFA uses for phase
PHASE_COLUMNS: Final[tuple[str, ...]] = (
    "real_power_a_kw",
    "real_power_b_kw",
    "real_power_c_kw",
    "current_a_a",
    "current_b_a",
    "current_c_a",
)

# Ground-truth metadata column — must never enter the model
GROUND_TRUTH_COLUMNS: Final[tuple[str, ...]] = ("known_anomaly", "simulation_status")


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class AdapterResult:
    """Output of one simulated-telemetry resampling attempt."""

    frame: pd.DataFrame | None
    """15-min wide DataFrame ready for ``run_cafa_detection``, or None."""

    status: str
    """'ready', 'insufficient_history', or 'error'."""

    warnings: list[str] = field(default_factory=list)
    """Non-fatal conditions flagged during validation."""

    error: str | None = None
    """Error description when status == 'error'."""

    n_1min_rows: int = 0
    """Number of one-minute input rows received."""

    n_15min_rows: int = 0
    """Number of 15-minute output rows produced."""

    dropped_duplicates: int = 0
    """One-minute rows dropped because of duplicate timestamps."""

    dropped_invalid: int = 0
    """One-minute rows dropped because real_power_kw was NaN/non-numeric."""

    ground_truth_preserved: pd.Series | None = None
    """Boolean Series aligned to the 15-min index — True when any contributing
    1-min reading in that bucket had ``known_anomaly == True``.  Available only
    when the input contained a ``known_anomaly`` column."""

    @property
    def is_ready(self) -> bool:
        """Return True when the frame is safe to pass to CAFA."""
        return self.status == "ready" and self.frame is not None


# ── Resampling logic ──────────────────────────────────────────────────────────

def _coerce_and_clean(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Coerce types, strip ground-truth columns, drop bad rows.

    Returns the cleaned frame plus counts of (duplicates_dropped, invalid_dropped).
    """
    frame = df.copy()

    # Drop ground-truth metadata — must never enter the model
    frame.drop(columns=list(GROUND_TRUTH_COLUMNS), errors="ignore", inplace=True)

    # Ensure UTC-aware DatetimeIndex
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("Input must have a DatetimeIndex.")
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    frame.sort_index(inplace=True)

    # Drop duplicate timestamps (keep first occurrence)
    n_before_dedup = len(frame)
    frame = frame[~frame.index.duplicated(keep="first")]
    duplicates_dropped = n_before_dedup - len(frame)

    # Drop rows with non-numeric or NaN real_power_kw
    if "real_power_kw" in frame.columns:
        frame["real_power_kw"] = pd.to_numeric(frame["real_power_kw"], errors="coerce")
        n_before_nan = len(frame)
        frame = frame.dropna(subset=["real_power_kw"])
        invalid_dropped = n_before_nan - len(frame)
    else:
        invalid_dropped = 0

    return frame, duplicates_dropped, invalid_dropped


def _extract_ground_truth(df_1min: pd.DataFrame, index_15min: pd.DatetimeIndex) -> pd.Series | None:
    """Compute a 15-min ground-truth label: True if any 1-min bucket row had
    ``known_anomaly == True``.  Returns None when the column is absent."""
    if "known_anomaly" not in df_1min.columns:
        return None
    gt = df_1min["known_anomaly"].astype(bool)
    # Resample: True if any 1-min row in the 15-min window was anomalous
    gt_15 = (
        gt.resample(TARGET_FREQ, label="left", closed="left")
        .max().fillna(False).astype(bool)
        .reindex(index_15min)
        .fillna(False)
    )
    return gt_15


def _synthesise_phase_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Synthesize per-phase columns if they are absent but total power is present.

    The one-minute service does not emit per-phase channels.  CAFA uses them
    only for the optional ``phase_imbalance`` feature; CAFA falls back to
    ``np.nan`` gracefully when they are absent, so this is a best-effort
    synthesis that keeps the feature pipeline intact.
    """
    has_phases = all(c in frame.columns for c in PHASE_COLUMNS[:3])
    if not has_phases and "real_power_kw" in frame.columns:
        # Equal-split synthesis — marks the imbalance as zero
        frame["real_power_a_kw"] = frame["real_power_kw"] * 0.34
        frame["real_power_b_kw"] = frame["real_power_kw"] * 0.33
        frame["real_power_c_kw"] = frame["real_power_kw"] * 0.33
    if all(c not in frame.columns for c in ("current_a_a", "current_b_a", "current_c_a")):
        if "current_avg_a" in frame.columns:
            frame["current_a_a"] = frame["current_avg_a"] * 0.34
            frame["current_b_a"] = frame["current_avg_a"] * 0.33
            frame["current_c_a"] = frame["current_avg_a"] * 0.33
    return frame


def resample_to_15min(df_1min: pd.DataFrame) -> pd.DataFrame:
    """Resample one-minute telemetry to 15-minute averages.

    Args:
        df_1min: UTC-indexed one-minute wide DataFrame from the simulated
            service.  Must include ``real_power_kw``.

    Returns:
        A 15-minute wide DataFrame with the same column schema as
        ``get_building_state_df`` output, suitable for ``run_cafa_detection``.

    Raises:
        ValueError: If the input is empty, malformed, or lacks required columns.
    """
    if df_1min.empty:
        raise ValueError("Cannot resample an empty DataFrame.")

    frame, _, _ = _coerce_and_clean(df_1min)
    if frame.empty:
        raise ValueError("All rows were dropped after cleaning (NaN real_power_kw).")

    # Select numeric columns only for resampling
    numeric_cols = [c for c in frame.columns if c not in GROUND_TRUTH_COLUMNS]
    numeric_frame = frame[numeric_cols].apply(pd.to_numeric, errors="coerce")

    # Resample: mean of each 15-min bucket, closed/label on the left edge
    df_15 = (
        numeric_frame
        .resample(TARGET_FREQ, label="left", closed="left")
        .mean()
    )
    df_15.index.name = "time"

    # Synthesize per-phase columns for the phase_imbalance CAFA feature
    df_15 = _synthesise_phase_columns(df_15)

    return df_15


# ── Validation ────────────────────────────────────────────────────────────────

def validate_adapter_frame(df_15min: pd.DataFrame) -> list[str]:
    """Validate a 15-minute adapter output frame before CAFA ingestion.

    Returns a list of warning strings.  An empty list means the frame is
    fully compatible.  This function never raises.
    """
    warnings: list[str] = []
    n = len(df_15min)

    if n == 0:
        warnings.append("Frame is empty — CAFA cannot run.")
        return warnings

    if n < MIN_15MIN_ROWS:
        warnings.append(
            f"Only {n} 15-min rows available; CAFA requires >= {MIN_15MIN_ROWS} rows "
            f"(= {MIN_15MIN_ROWS * 15 // 60} h) for lag-96 dropna.  "
            "Predictions will be 'insufficient_history'."
        )

    if "real_power_kw" not in df_15min.columns:
        warnings.append("Required column 'real_power_kw' is missing.")

    nan_frac = df_15min["real_power_kw"].isna().mean() if "real_power_kw" in df_15min.columns else 1.0
    if nan_frac > 0.05:
        warnings.append(
            f"{nan_frac:.1%} of real_power_kw values are NaN — "
            "missing buckets may degrade CAFA residual quality."
        )

    # Check for index monotonicity
    if not df_15min.index.is_monotonic_increasing:
        warnings.append("15-min index is not monotonically increasing.")

    # Detect obvious cadence drift: any gap > 30 min indicates missing buckets
    if n > 1:
        diffs = df_15min.index.to_series().diff().dropna()
        max_gap = diffs.max()
        if max_gap > pd.Timedelta("30min"):
            n_gaps = int((diffs > pd.Timedelta("30min")).sum())
            warnings.append(
                f"{n_gaps} gap(s) > 30 min detected in 15-min index "
                f"(largest = {max_gap}).  CAFA rolling features will span gaps."
            )

    return warnings


# ── Main entry point ──────────────────────────────────────────────────────────

def adapt_simulated_telemetry(df_1min: pd.DataFrame) -> AdapterResult:
    """Convert one-minute simulated readings into a model-ready 15-min frame.

    This is the primary public interface.  It never raises; errors are captured
    in ``AdapterResult.status`` and ``AdapterResult.error``.

    The ``known_anomaly`` column, if present, is preserved in
    ``AdapterResult.ground_truth_preserved`` and is **never** included in
    the returned frame that CAFA sees.

    Time-granularity contract
    -------------------------
    Input:  1-min readings from ``LiveMeterSimulator``
    Output: 15-min aggregated DataFrame with:
        - index freq = 15T UTC
        - columns = CAFA's expected sensor schema
        - ``known_anomaly`` stripped from frame, preserved separately
    """
    n_in = len(df_1min)
    result = AdapterResult(frame=None, status="error", n_1min_rows=n_in)

    if n_in == 0:
        result.error = "Input DataFrame is empty."
        result.status = "insufficient_history"
        return result

    try:
        # Preserve ground truth before cleaning strips it
        gt_15 = None
        if "known_anomaly" in df_1min.columns:
            # Resample on raw index before cleaning
            _gt_frame = df_1min.copy()
            if _gt_frame.index.tz is None:
                _gt_frame.index = _gt_frame.index.tz_localize("UTC")
            # Will be realigned after resampling
            _gt_raw = _gt_frame["known_anomaly"].astype(bool)
        else:
            _gt_raw = None

        frame_15, dup_dropped, inv_dropped = _coerce_and_clean(df_1min)
        result.dropped_duplicates = dup_dropped
        result.dropped_invalid = inv_dropped

        if frame_15.empty:
            result.error = "All one-minute rows were dropped during cleaning."
            result.status = "error"
            return result

        numeric_cols = [c for c in frame_15.columns if c not in GROUND_TRUTH_COLUMNS]
        numeric_frame = frame_15[numeric_cols].apply(pd.to_numeric, errors="coerce")
        df_15 = (
            numeric_frame
            .resample(TARGET_FREQ, label="left", closed="left")
            .mean()
        )
        df_15.index.name = "time"
        df_15 = _synthesise_phase_columns(df_15)

        n_15 = len(df_15)
        result.n_15min_rows = n_15

        # Align ground truth to 15-min index
        if _gt_raw is not None:
            gt_15 = (
                _gt_raw.resample(TARGET_FREQ, label="left", closed="left")
                .max().fillna(False).astype(bool)
                .reindex(df_15.index)
                .fillna(False)
            )
            result.ground_truth_preserved = gt_15

        # Validate
        warnings = validate_adapter_frame(df_15)
        result.warnings = warnings

        if n_15 < MIN_15MIN_ROWS:
            result.status = "insufficient_history"
            result.frame = df_15  # return it anyway so callers can inspect
            result.error = (
                f"Only {n_15} 15-min rows available; need >= {MIN_15MIN_ROWS} "
                f"({MIN_15MIN_ROWS * 15 // 60} h) for CAFA lag-96 features."
            )
        else:
            result.status = "ready"
            result.frame = df_15

        LOGGER.info(
            "Adapter: %d one-min rows -> %d 15-min rows (dup=%d, inv=%d, status=%s)",
            n_in, n_15, dup_dropped, inv_dropped, result.status,
        )

    except (ValueError, TypeError) as exc:
        result.error = str(exc)
        result.status = "error"
        LOGGER.exception("Adapter resampling failed: %s", exc)

    return result

