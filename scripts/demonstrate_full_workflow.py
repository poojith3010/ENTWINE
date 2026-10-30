"""Repeatable demonstration script for ENTWINE simulated live-meter pipeline.

Demonstrates all 10 steps of the objective:
1. Starts the simulator.
2. Generates one-minute readings.
3. Injects the configured abnormal event.
4. Displays the live telemetry trend summary.
5. Converts eligible data into model-compatible inputs using the adapter.
6. Executes GridReason.
7. Generates GrCF explanations for selected observations.
8. Runs CAFA when sufficient correctly aligned labeled observations exist.
9. Displays actual predictions, explanations, and audit findings.
10. Produces a summary of the run.
"""

from __future__ import annotations

import logging
import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import time
from datetime import datetime, timezone

# Ensure project root is in python path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd

from validation.live_meter_service import LiveMeterSimulator, SimulationConfig
from validation.sim_pipeline import run_full_sim_pipeline
from validation.sim_to_model_adapter import adapt_simulated_telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("demonstration")


def run_demonstration() -> int:
    print("=" * 80)
    print("  ENTWINE DIGITAL TWIN — FULL LIVE SIMULATION & ANALYTICAL WORKFLOW")
    print("=" * 80)

    # ── Step 1 & 2: Start simulator and generate 1-minute readings ─────────────
    print("\n[Step 1 & 2] Initializing and starting LiveMeterSimulator (1-minute cadence)...")
    collected_readings = []
    config = SimulationConfig(
        simulation_id="demo-workflow-run",
        meter_code="PH-A-MAIN",
        duration_minutes=8,
        interval_seconds=0.01,  # accelerated for demonstration
        abnormal_start_minute=3,
        abnormal_duration_minutes=2,
        abnormal_multiplier=1.65,
        random_seed=20261030,
    )
    simulator = LiveMeterSimulator(config, on_reading=collected_readings.append)
    assert simulator.start() is True
    print(f"Simulator started successfully: status={simulator.stats.status}")

    # Wait for completion
    timeout = time.time() + 5.0
    while time.time() < timeout and simulator.stats.status == "running":
        time.sleep(0.02)

    stats = simulator.stats
    print(f"Simulator session complete: status={stats.status}, generated={stats.generated} readings")

    # ── Step 3: Verify injected abnormal event ─────────────────────────────────
    print("\n[Step 3] Checking ground-truth injected abnormal event...")
    n_anom = sum(r.known_anomaly for r in collected_readings)
    print(f"Injected anomaly readings: {n_anom}/{len(collected_readings)}")
    for r in collected_readings:
        if r.known_anomaly:
            print(f"  * Anomalous Reading at {r.time.isoformat()}: Power={r.real_power_kw:.2f} kW, PF={r.power_factor_pct:.1f}%")

    # ── Step 4: Display live telemetry trend ───────────────────────────────────
    print("\n[Step 4] Live Telemetry Trend Summary:")
    df_1min = pd.DataFrame([r.__dict__ for r in collected_readings])
    df_1min["time"] = pd.to_datetime(df_1min["time"], utc=True)
    df_1min = df_1min.set_index("time")
    normal_pow = df_1min.loc[~df_1min["known_anomaly"], "real_power_kw"].mean()
    anom_pow = df_1min.loc[df_1min["known_anomaly"], "real_power_kw"].mean()
    print(f"  * Baseline normal power: {normal_pow:.2f} kW")
    print(f"  * Injected abnormal power: {anom_pow:.2f} kW (delta: +{anom_pow - normal_pow:.2f} kW)")

    # ── Step 5: Convert eligible data into model-compatible inputs ────────────
    print("\n[Step 5] Converting 1-minute data via Model Adapter (sim_to_model_adapter)...")
    adapter_res = adapt_simulated_telemetry(df_1min)
    print(f"  * Adapter status: {adapter_res.status}")
    print(f"  * 1-min rows in: {adapter_res.n_1min_rows}")
    print(f"  * 15-min buckets out: {adapter_res.n_15min_rows}")
    print(f"  * Duplicates dropped: {adapter_res.dropped_duplicates}")
    print(f"  * Ground truth separated: {adapter_res.ground_truth_preserved is not None}")
    if adapter_res.status == "insufficient_history":
        print(f"  * Honest status: {adapter_res.error}")
        print("    (As designed: 8 minutes is insufficient for CAFA's 24-hour lag-96 requirement.)")

    # ── Steps 6, 7, 8, 9: Full Pipeline on Sufficient Simulated Context ──────
    print("\n[Steps 6, 7, 8 & 9] Executing Full Analytical Pipeline (CAFA -> GrCF -> GridReason -> Audit)...")
    print("  (Running with full 14-day simulated history to satisfy CAFA 96-interval feature window)")
    pipeline_res = run_full_sim_pipeline(df_1min=df_1min)

    print(f"\n--- CAFA Detection ---")
    cafa = pipeline_res.cafa
    print(f"  Status: {cafa.status}")
    print(f"  Records Processed: {cafa.records_processed}")
    print(f"  Anomalies Flagged: {cafa.anomalies_flagged}")
    print(f"  XGBoost Holdout MAE: {cafa.mae_kw:.4f} kW")
    print(f"  Elapsed: {cafa.elapsed_s:.2f}s")

    print(f"\n--- GrCF Explanations ---")
    grcf = pipeline_res.grcf
    print(f"  Status: {grcf.status}")
    print(f"  Target Anomaly Timestamp: {grcf.anomaly_time}")
    if grcf.counterfactual is not None:
        print(f"  Counterfactual Tensor Shape: {grcf.counterfactual.shape} (16 time steps x 10 features)")
        print("  Key Counterfactual Deltas (CF - Observed):")
        for feat, delta in sorted(grcf.feature_changes.items(), key=lambda x: abs(x[1]), reverse=True)[:4]:
            print(f"    - {feat}: {delta:+.4f}")
    print(f"  Elapsed: {grcf.elapsed_s:.2f}s")

    print(f"\n--- GridReason Alert ---")
    gr = pipeline_res.gridreason
    print(f"  Status: {gr.status}")
    print(f"  Alert text: {gr.alert}")

    print(f"\n--- CAFA Audit ---")
    audit = pipeline_res.audit
    print(f"  Status: {audit.status}")
    print(f"  Evaluated Observations: {audit.n_evaluated}")
    print(f"  Known Injected Intervals: {audit.n_known_anomaly}")
    print(f"  Model Flagged Intervals: {audit.n_cafa_flagged}")
    print(f"  True Positives (TP): {audit.n_true_positive}")
    print(f"  False Positives (FP): {audit.n_false_positive}")
    print(f"  False Negatives (FN): {audit.n_false_negative}")
    print(f"  Recall: {audit.recall:.2%}" if np.isfinite(audit.recall) else "  Recall: N/A")
    print(f"  Precision: {audit.precision:.2%}" if np.isfinite(audit.precision) else "  Precision: N/A")
    print(f"  F1 Score: {audit.f1:.2%}" if np.isfinite(audit.f1) else "  F1 Score: N/A")
    print(f"  Note: {audit.note}")

    # ── Step 10: Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("  [Step 10] DEMONSTRATION RUN SUMMARY")
    print("=" * 80)
    print(pipeline_res.summary)
    print(f"\nWorkflow finished successfully in {pipeline_res.pipeline_elapsed_s:.2f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_demonstration())
