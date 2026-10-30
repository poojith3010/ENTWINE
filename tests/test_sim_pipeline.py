"""Tests for the simulation analytics pipeline (CAFA, GrCF, GridReason, Audit)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from validation.sim_pipeline import (
    CafaAuditResult,
    CafaRunResult,
    GrCFRunResult,
    GridReasonRunResult,
    run_full_sim_pipeline,
    run_sim_cafa_audit,
    run_sim_cafa_pipeline,
    run_sim_grcf_pipeline,
    run_sim_gridreason,
)


def test_sim_cafa_pipeline_insufficient_history() -> None:
    """Verify CAFA handles insufficient history without throwing exceptions."""
    idx = pd.date_range("2026-10-30", periods=20, freq="15min", tz="UTC")
    df = pd.DataFrame({"real_power_kw": np.ones(20) * 100.0}, index=idx)
    res = run_sim_cafa_pipeline(df)
    assert res.status == "insufficient_history"
    assert "need" in res.error.lower()


def test_sim_cafa_pipeline_empty() -> None:
    """Verify CAFA handles empty input safely."""
    res = run_sim_cafa_pipeline(pd.DataFrame())
    assert res.status == "insufficient_history"


def test_sim_grcf_pipeline_input_validation() -> None:
    """Verify GrCF catches invalid inputs cleanly."""
    # Empty frame
    res_empty = run_sim_grcf_pipeline(pd.DataFrame())
    assert res_empty.status == "unsupported_input"

    # Frame with no anomalies
    idx = pd.date_range("2026-10-30", periods=30, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {
            "real_power_kw": np.ones(30) * 100.0,
            "cafa_if_flag": np.zeros(30, dtype=int),
        },
        index=idx,
    )
    res_no_anom = run_sim_grcf_pipeline(df)
    assert res_no_anom.status == "skipped"

    # Out of range anomaly_idx
    df.loc[df.index[5], "cafa_if_flag"] = 1
    res_oor = run_sim_grcf_pipeline(df, anomaly_idx=100)
    assert res_oor.status == "unsupported_input"


def test_sim_gridreason_deterministic_alert() -> None:
    """Verify GridReason builds valid deterministic alert from GrCF result."""
    # When GrCF is skipped
    res_skip = run_sim_gridreason(GrCFRunResult(status="skipped"))
    assert res_skip.status == "skipped"
    assert res_skip.alert is None

    # When GrCF succeeded with a 16x10 counterfactual
    cf_data = np.ones((16, 10)) * 105.0
    grcf_ok = GrCFRunResult(
        status="ok",
        anomaly_idx=10,
        anomaly_time=pd.Timestamp("2026-10-30 02:30:00", tz="UTC"),
        counterfactual=cf_data,
    )
    res_ok = run_sim_gridreason(grcf_ok)
    assert res_ok.status == "ok"
    assert res_ok.alert is not None
    assert "observed deviation" in res_ok.alert
    assert "requires operator review" in res_ok.alert


def test_sim_cafa_audit_evaluation_metrics() -> None:
    """Verify CAFA audit accurately computes TP, FP, FN, TN, precision, recall, F1."""
    idx = pd.date_range("2026-10-30", periods=10, freq="15min", tz="UTC")
    # Ground truth: 4 positives at indices 0, 1, 2, 3
    gt = pd.Series([True, True, True, True, False, False, False, False, False, False], index=idx)

    # Model predictions: flagged at 2, 3 (TP=2, FN=2), and at 4 (FP=1), rest normal (TN=5)
    det_df = pd.DataFrame(
        {
            "cafa_if_flag": [0, 0, 1, 1, 1, 0, 0, 0, 0, 0],
            "real_power_kw": np.ones(10) * 100.0,
        },
        index=idx,
    )

    audit = run_sim_cafa_audit(det_df, gt)
    assert audit.status == "ok"
    assert audit.n_evaluated == 10
    assert audit.n_true_positive == 2
    assert audit.n_false_positive == 1
    assert audit.n_false_negative == 2
    assert audit.n_true_negative == 5
    assert pytest.approx(audit.recall, 0.01) == 2 / 4  # 0.50
    assert pytest.approx(audit.precision, 0.01) == 2 / 3  # 0.667
    expected_f1 = 2 * (0.5 * (2 / 3)) / (0.5 + 2 / 3)
    assert pytest.approx(audit.f1, 0.01) == expected_f1


def test_sim_cafa_audit_insufficient_or_partial_data() -> None:
    """Verify audit handles small sample edge cases and sets status='partial'."""
    idx = pd.date_range("2026-10-30", periods=2, freq="15min", tz="UTC")
    gt = pd.Series([True, False], index=idx)
    det_df = pd.DataFrame({"cafa_if_flag": [1, 0]}, index=idx)

    audit = run_sim_cafa_audit(det_df, gt)
    assert audit.status == "partial"
    assert any("preliminary" in w.lower() for w in audit.warnings)


def test_full_sim_pipeline_execution() -> None:
    """End-to-end integration test of the full simulation analytical pipeline.
    
    Verifies that the adapter, CAFA, GrCF, GridReason, and Audit connect
    correctly, execute the real implementations, and produce auditable outputs.
    """
    result = run_full_sim_pipeline()

    assert result.adapter is not None
    assert result.cafa is not None
    assert result.cafa.status == "ok"
    assert result.cafa.records_processed > 0
    assert result.cafa.anomalies_flagged > 0

    assert result.grcf is not None
    assert result.grcf.status == "ok"
    assert result.grcf.counterfactual is not None
    assert result.grcf.counterfactual.shape == (16, 10)
    assert np.isfinite(result.grcf.counterfactual).all()

    assert result.gridreason is not None
    assert result.gridreason.status == "ok"
    assert result.gridreason.alert is not None
    assert "CAFA/GrCF alert" in result.gridreason.alert

    assert result.audit is not None
    assert result.audit.status in ("ok", "partial")
    assert result.audit.n_evaluated > 0

    assert len(result.summary) > 0
    assert "Simulation Pipeline Summary" in result.summary
