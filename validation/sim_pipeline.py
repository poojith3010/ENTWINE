"""End-to-end simulation → CAFA → GrCF → GridReason pipeline.

This module connects the simulated live-meter telemetry to the existing
analytical pipeline without modifying any algorithm.  It provides:

* ``run_sim_cafa_pipeline``  – runs CAFA on a 15-min-resampled simulated frame
* ``run_sim_grcf_pipeline``  – generates a GrCF counterfactual for a flagged row
* ``run_sim_gridreason``     – produces a deterministic GridReason alert
* ``run_sim_cafa_audit``     – aligns CAFA predictions with ground-truth labels
* ``run_full_sim_pipeline``  – end-to-end orchestration returning ``SimPipelineResult``

All ground-truth labels (``known_anomaly``) are kept strictly separate from
model inputs throughout.  The pipeline never forces predictions to match labels.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Final

import numpy as np
import pandas as pd

from models.cafa_detector import run_cafa_detection
from models.grcf_explainer import FEATURE_COLUMNS, SEQ_LEN, explain_anomaly
from models.gridreason_llm import (
    AnomalyEvent,
    StateDelta,
    build_deterministic_alert,
    calculate_state_deltas,
)
from validation.live_meter_simulation import simulate_live_meter
from validation.sim_to_model_adapter import (
    MIN_15MIN_ROWS,
    AdapterResult,
    adapt_simulated_telemetry,
)

LOGGER: Final[logging.Logger] = logging.getLogger("validation.sim_pipeline")


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class CafaRunResult:
    """Output of one CAFA run on simulated model-compatible data."""

    status: str
    """'ok', 'insufficient_history', 'no_anomalies', or 'error'."""

    detection_df: pd.DataFrame | None = None
    records_processed: int = 0
    anomalies_flagged: int = 0
    mae_kw: float = float("nan")
    error: str | None = None
    elapsed_s: float = 0.0


@dataclass
class GrCFRunResult:
    """Output of one GrCF counterfactual generation on simulated data."""

    status: str
    """'ok', 'unsupported_input', 'execution_failure', or 'skipped'."""

    anomaly_idx: int = -1
    anomaly_time: pd.Timestamp | None = None
    counterfactual: np.ndarray | None = None
    """Shape (SEQ_LEN, len(FEATURE_COLUMNS)) in original feature units, or None."""

    feature_changes: dict[str, float] = field(default_factory=dict)
    """Observed → counterfactual delta for each FEATURE_COLUMN."""

    final_total_loss: float = float("nan")
    error: str | None = None
    elapsed_s: float = 0.0


@dataclass
class GridReasonRunResult:
    """Output of the GridReason alert translation layer."""

    status: str
    """'ok' or 'skipped'."""

    alert: str | None = None
    deltas: list[StateDelta] = field(default_factory=list)


@dataclass
class CafaAuditResult:
    """CAFA predictions aligned with ground-truth known_anomaly labels."""

    status: str
    """'ok', 'partial', 'insufficient_data', or 'skipped'."""

    n_evaluated: int = 0
    n_true_positive: int = 0
    n_false_positive: int = 0
    n_false_negative: int = 0
    n_true_negative: int = 0
    recall: float = float("nan")
    precision: float = float("nan")
    f1: float = float("nan")
    n_known_anomaly: int = 0
    n_cafa_flagged: int = 0
    warnings: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class SimPipelineResult:
    """Complete end-to-end simulation pipeline result."""

    # Adapter
    adapter: AdapterResult | None = None

    # CAFA
    cafa: CafaRunResult | None = None

    # GrCF
    grcf: GrCFRunResult | None = None

    # GridReason
    gridreason: GridReasonRunResult | None = None

    # Audit
    audit: CafaAuditResult | None = None

    # Summary
    summary: str = ""
    pipeline_elapsed_s: float = 0.0


# ── Step 3: CAFA on simulated 15-min data ─────────────────────────────────────

def run_sim_cafa_pipeline(df_15min: pd.DataFrame) -> CafaRunResult:
    """Run CAFA on a 15-min model-compatible frame from the adapter.

    ``known_anomaly`` must already be stripped from ``df_15min`` before this
    function is called (the adapter guarantees this).
    """
    if df_15min is None or df_15min.empty:
        return CafaRunResult(status="insufficient_history",
                             error="Empty frame passed to CAFA.")

    if len(df_15min) < MIN_15MIN_ROWS:
        return CafaRunResult(
            status="insufficient_history",
            records_processed=len(df_15min),
            error=f"Only {len(df_15min)} 15-min rows; CAFA needs ≥ {MIN_15MIN_ROWS}.",
        )

    t0 = time.monotonic()
    try:
        detection_df, summary = run_cafa_detection(df_15min)
        elapsed = time.monotonic() - t0
        if summary.anomalies_flagged == 0:
            return CafaRunResult(
                status="no_anomalies",
                detection_df=detection_df,
                records_processed=summary.records_processed,
                anomalies_flagged=0,
                mae_kw=summary.mean_absolute_error_kw,
                elapsed_s=elapsed,
            )
        return CafaRunResult(
            status="ok",
            detection_df=detection_df,
            records_processed=summary.records_processed,
            anomalies_flagged=summary.anomalies_flagged,
            mae_kw=summary.mean_absolute_error_kw,
            elapsed_s=elapsed,
        )
    except (ValueError, TypeError) as exc:
        return CafaRunResult(
            status="error",
            error=str(exc),
            elapsed_s=time.monotonic() - t0,
        )


# ── Step 4: GrCF on a simulated CAFA detection frame ─────────────────────────

def run_sim_grcf_pipeline(
    detection_df: pd.DataFrame,
    anomaly_idx: int | None = None,
) -> GrCFRunResult:
    """Generate a GrCF counterfactual for one CAFA-flagged simulated window.

    Args:
        detection_df:  Output of ``run_cafa_detection`` on the 15-min frame.
        anomaly_idx:   Zero-based index of the target window in ``detection_df``.
            If None, the first CAFA-flagged row is used.

    Returns:
        A ``GrCFRunResult`` that is always populated — never raises.
    """
    if detection_df is None or detection_df.empty:
        return GrCFRunResult(status="unsupported_input",
                             error="Empty detection frame.")

    flags = detection_df["cafa_if_flag"].to_numpy(dtype=bool)
    anomaly_positions = np.flatnonzero(flags)

    if len(anomaly_positions) == 0:
        return GrCFRunResult(status="skipped",
                             error="No CAFA-flagged rows to explain.")

    if anomaly_idx is None:
        anomaly_idx = int(anomaly_positions[0])

    if anomaly_idx < 0 or anomaly_idx >= len(detection_df):
        return GrCFRunResult(
            status="unsupported_input",
            anomaly_idx=anomaly_idx,
            error=f"anomaly_idx={anomaly_idx} is out of range for frame of length {len(detection_df)}.",
        )

    normal_mask = ~flags
    if normal_mask.sum() < SEQ_LEN:
        return GrCFRunResult(
            status="unsupported_input",
            anomaly_idx=anomaly_idx,
            error=(
                f"Only {normal_mask.sum()} normal rows available; "
                f"GrCF needs ≥ {SEQ_LEN} fully-normal sequence windows."
            ),
        )

    t0 = time.monotonic()
    try:
        anomaly_time = pd.Timestamp(detection_df.index[anomaly_idx])
        if anomaly_time.tzinfo is None:
            anomaly_time = anomaly_time.tz_localize("UTC")

        counterfactual = explain_anomaly(detection_df, normal_mask, anomaly_idx)
        elapsed = time.monotonic() - t0

        # Compute observed vs counterfactual feature changes
        anomaly_row = detection_df.iloc[anomaly_idx]
        feature_changes: dict[str, float] = {}
        for feat in FEATURE_COLUMNS:
            if feat in detection_df.columns:
                observed_val = float(anomaly_row[feat]) if not pd.isna(anomaly_row.get(feat, float("nan"))) else float("nan")
                cf_val = float(np.mean(counterfactual[:, list(FEATURE_COLUMNS).index(feat)]))
                feature_changes[feat] = round(cf_val - observed_val, 4)

        return GrCFRunResult(
            status="ok",
            anomaly_idx=anomaly_idx,
            anomaly_time=anomaly_time,
            counterfactual=counterfactual,
            feature_changes=feature_changes,
            elapsed_s=elapsed,
        )

    except (ValueError, TypeError, RuntimeError) as exc:
        return GrCFRunResult(
            status="execution_failure",
            anomaly_idx=anomaly_idx,
            error=str(exc),
            elapsed_s=time.monotonic() - t0,
        )


# ── Step 5: GridReason on simulated GrCF output ───────────────────────────────

def run_sim_gridreason(grcf_result: GrCFRunResult) -> GridReasonRunResult:
    """Run the deterministic GridReason translation on a simulated GrCF result.

    Uses the same ``calculate_state_deltas`` + ``build_deterministic_alert``
    functions as the production GridReason pipeline.  No DB interaction.
    """
    if grcf_result.status != "ok" or grcf_result.counterfactual is None:
        return GridReasonRunResult(
            status="skipped",
            alert=None,
        )

    # Build a synthetic AnomalyEvent from the simulation run
    event_time = grcf_result.anomaly_time or pd.Timestamp.utcnow()
    synthetic_event = AnomalyEvent(
        event_time=event_time,
        meter_id=0,          # simulated — no real meter_id
        detector_type="CAFA-GrCF-SIM",
        counterfactual_data=grcf_result.counterfactual,
    )

    deltas = calculate_state_deltas(grcf_result.counterfactual)
    alert = build_deterministic_alert(synthetic_event, deltas)

    return GridReasonRunResult(status="ok", alert=alert, deltas=deltas)


# ── Step 6: CAFA audit against ground-truth labels ────────────────────────────

def run_sim_cafa_audit(
    detection_df: pd.DataFrame,
    ground_truth: pd.Series,
) -> CafaAuditResult:
    """Align CAFA flags with ground-truth ``known_anomaly`` labels.

    Args:
        detection_df:   CAFA output frame (from ``run_cafa_detection``).
        ground_truth:   Boolean Series indexed identically to ``detection_df``,
            where True = known anomalous interval.  This is the adapter's
            ``ground_truth_preserved`` output, re-indexed to the detection index.

    Returns:
        A ``CafaAuditResult`` with precision / recall / F1.

    The audit is valid only when the detection frame and ground_truth share
    the same index.  If sample counts are too small (< 2 positives and
    < 2 negatives), the result is flagged as 'partial'.
    """
    if detection_df is None or detection_df.empty:
        return CafaAuditResult(
            status="insufficient_data",
            warnings=["Detection frame is empty."],
        )

    if ground_truth is None or ground_truth.empty:
        return CafaAuditResult(
            status="skipped",
            warnings=["No ground-truth labels available."],
        )

    # Align on common timestamps
    common_idx = detection_df.index.intersection(ground_truth.index)
    if len(common_idx) == 0:
        return CafaAuditResult(
            status="insufficient_data",
            warnings=["Detection frame and ground-truth have no overlapping timestamps."],
        )

    y_true = ground_truth.reindex(common_idx).fillna(False).astype(bool)
    y_pred = detection_df.reindex(common_idx)["cafa_if_flag"].fillna(0).astype(bool)

    n = len(y_true)
    tp = int((y_true & y_pred).sum())
    fp = int((~y_true & y_pred).sum())
    fn = int((y_true & ~y_pred).sum())
    tn = int((~y_true & ~y_pred).sum())
    n_pos = tp + fn
    n_flagged = tp + fp

    warnings = []
    if n_pos == 0:
        warnings.append(
            "No known-anomaly intervals in the evaluated window — "
            "recall cannot be computed."
        )
    if n_flagged == 0:
        warnings.append("CAFA flagged no intervals — precision cannot be computed.")

    recall = tp / n_pos if n_pos > 0 else float("nan")
    precision = tp / n_flagged if n_flagged > 0 else float("nan")
    f1 = (
        2 * precision * recall / (precision + recall)
        if (np.isfinite(precision) and np.isfinite(recall) and (precision + recall) > 0)
        else float("nan")
    )

    status = "ok"
    if n_pos < 2 or (n - n_pos) < 2:
        status = "partial"
        warnings.append(
            f"Very small sample: {n_pos} positive labels and {n - n_pos} negatives. "
            "Metrics are preliminary — not generalizable."
        )

    note = (
        "This audit evaluates CAFA predictions against a deliberately injected "
        "synthetic event. Results do not establish general model performance."
    )

    return CafaAuditResult(
        status=status,
        n_evaluated=n,
        n_true_positive=tp,
        n_false_positive=fp,
        n_false_negative=fn,
        n_true_negative=tn,
        recall=recall,
        precision=precision,
        f1=f1,
        n_known_anomaly=n_pos,
        n_cafa_flagged=n_flagged,
        warnings=warnings,
        note=note,
    )


# ── Full pipeline orchestrator ─────────────────────────────────────────────────

def run_full_sim_pipeline(
    df_1min: pd.DataFrame | None = None,
    grcf_anomaly_idx: int | None = None,
) -> SimPipelineResult:
    """Run the complete simulated telemetry → model pipeline.

    If ``df_1min`` is None, a fresh 15-min simulation (14 days) is generated
    and pre-resampled to the model-compatible 15-min cadence.  This avoids the
    insufficient-history problem because 14 days × 96 intervals = 1344 rows
    (well above MIN_15MIN_ROWS = 97).

    For the one-minute service stream (15 readings max in the demo session),
    the adapter returns ``insufficient_history`` and the pipeline records that
    honestly.  The 15-min simulation path is used for the actual model run.

    Args:
        df_1min:           Optional one-minute wide DataFrame from the service.
            Pass None to use the built-in 14-day simulation.
        grcf_anomaly_idx:  Optional explicit window index for GrCF.

    Returns:
        ``SimPipelineResult`` with all sub-results populated.
    """
    t_pipeline = time.monotonic()
    result = SimPipelineResult()

    # ── Adapter ───────────────────────────────────────────────────────────────
    if df_1min is not None:
        adapter = adapt_simulated_telemetry(df_1min)
        result.adapter = adapter

        if adapter.status == "ready" and adapter.frame is not None:
            df_model = adapter.frame
            ground_truth = adapter.ground_truth_preserved
        else:
            # Insufficient history from the live service — fall back to the
            # purpose-built 15-min simulation which has 14 days of context.
            LOGGER.info(
                "Live service stream insufficient (%s) — falling back to 14-day simulation.",
                adapter.status,
            )
            telem, sim_spec = simulate_live_meter()
            df_model = telem.drop(columns=["known_anomaly"], errors="ignore")
            ground_truth = telem["known_anomaly"] if "known_anomaly" in telem.columns else None
    else:
        # No live data — run on the built-in 14-day simulation
        LOGGER.info("No live data supplied — running 14-day simulation.")
        telem, sim_spec = simulate_live_meter()
        df_model = telem.drop(columns=["known_anomaly"], errors="ignore")
        ground_truth = telem["known_anomaly"] if "known_anomaly" in telem.columns else None
        # Create a synthetic adapter result for the response schema
        result.adapter = AdapterResult(
            frame=df_model,
            status="ready",
            n_1min_rows=0,
            n_15min_rows=len(df_model),
            warnings=["Built-in 14-day simulation used (no live service data)."],
        )
        result.adapter.ground_truth_preserved = ground_truth

    # ── CAFA ──────────────────────────────────────────────────────────────────
    cafa = run_sim_cafa_pipeline(df_model)
    result.cafa = cafa
    LOGGER.info("CAFA status=%s flagged=%d", cafa.status, cafa.anomalies_flagged)

    if cafa.status != "ok" or cafa.detection_df is None:
        result.summary = _build_summary(result)
        result.pipeline_elapsed_s = time.monotonic() - t_pipeline
        return result

    # ── GrCF ──────────────────────────────────────────────────────────────────
    grcf = run_sim_grcf_pipeline(cafa.detection_df, grcf_anomaly_idx)
    result.grcf = grcf
    LOGGER.info("GrCF status=%s", grcf.status)

    # ── GridReason ────────────────────────────────────────────────────────────
    result.gridreason = run_sim_gridreason(grcf)

    # ── CAFA audit ────────────────────────────────────────────────────────────
    if ground_truth is not None and cafa.detection_df is not None:
        # Re-index ground truth to match the CAFA detection frame (after engineer_features dropna)
        gt_aligned = (
            ground_truth.reindex(cafa.detection_df.index).fillna(False).astype(bool)
        )
        audit = run_sim_cafa_audit(cafa.detection_df, gt_aligned)
    else:
        audit = CafaAuditResult(status="skipped", warnings=["No ground-truth labels."])
    result.audit = audit
    LOGGER.info(
        "Audit status=%s TP=%d FP=%d FN=%d recall=%.2f precision=%.2f",
        audit.status, audit.n_true_positive, audit.n_false_positive,
        audit.n_false_negative,
        audit.recall if np.isfinite(audit.recall) else -1,
        audit.precision if np.isfinite(audit.precision) else -1,
    )

    result.summary = _build_summary(result)
    result.pipeline_elapsed_s = time.monotonic() - t_pipeline
    return result


# ── Summary formatter ─────────────────────────────────────────────────────────

def _build_summary(r: SimPipelineResult) -> str:
    """Return a concise plain-text summary of the pipeline run."""
    lines = ["## Simulation Pipeline Summary"]

    # Adapter
    if r.adapter:
        lines.append(
            f"- **Adapter:** {r.adapter.status} | "
            f"{r.adapter.n_1min_rows} 1-min rows → {r.adapter.n_15min_rows} 15-min rows"
        )
        if r.adapter.warnings:
            for w in r.adapter.warnings:
                lines.append(f"  - ⚠ {w}")

    # CAFA
    if r.cafa:
        lines.append(
            f"- **CAFA:** {r.cafa.status} | "
            f"records={r.cafa.records_processed} | "
            f"flagged={r.cafa.anomalies_flagged} | "
            f"MAE={r.cafa.mae_kw:.3f} kW | "
            f"elapsed={r.cafa.elapsed_s:.1f}s"
        )
        if r.cafa.error:
            lines.append(f"  - error: {r.cafa.error}")

    # GrCF
    if r.grcf:
        lines.append(f"- **GrCF:** {r.grcf.status}")
        if r.grcf.status == "ok" and r.grcf.feature_changes:
            top3 = sorted(r.grcf.feature_changes.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
            changes = "; ".join(f"{k}={v:+.3f}" for k, v in top3)
            lines.append(f"  - Top 3 changes: {changes}")
        if r.grcf.error:
            lines.append(f"  - error: {r.grcf.error}")

    # GridReason
    if r.gridreason and r.gridreason.alert:
        lines.append(f"- **GridReason:** {r.gridreason.alert[:200]}")

    # Audit
    if r.audit:
        if r.audit.status in ("ok", "partial"):
            lines.append(
                f"- **CAFA Audit ({r.audit.status}):** "
                f"N={r.audit.n_evaluated} | "
                f"TP={r.audit.n_true_positive} FP={r.audit.n_false_positive} "
                f"FN={r.audit.n_false_negative} TN={r.audit.n_true_negative} | "
                f"Recall={'N/A' if not np.isfinite(r.audit.recall) else f'{r.audit.recall:.2%}'} | "
                f"Precision={'N/A' if not np.isfinite(r.audit.precision) else f'{r.audit.precision:.2%}'}"
            )
            if r.audit.warnings:
                for w in r.audit.warnings:
                    lines.append(f"  - ⚠ {w}")
            lines.append(f"  - _{r.audit.note}_")
        else:
            lines.append(f"- **CAFA Audit:** {r.audit.status}")

    if r.pipeline_elapsed_s:
        lines.append(f"\n_Total pipeline time: {r.pipeline_elapsed_s:.1f}s_")

    return "\n".join(lines)

