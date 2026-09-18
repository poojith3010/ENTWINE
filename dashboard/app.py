"""ENTWINE Digital Twin — Judge-Ready Explainable Dashboard.

This dashboard is designed to present the full ENTWINE intelligence pipeline
to a technical audience. It answers three core questions:
  WHAT  was detected? — Alert summary + deviation KPI cards
  HOW   was it found? — Step-by-step methodology explainer
  PROOF — show the data: interactive Plotly charts + comparison table
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final, TypedDict

import gradio as gr
import httpx
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ── Constants ─────────────────────────────────────────────────────────────────

API_BASE: Final[str] = "http://127.0.0.1:8000"
METER: Final[str] = "PH-A-MAIN"
REQUEST_TIMEOUT: Final[float] = 10.0

# Counterfactual tensor column names (from models/grcf_explainer.py FEATURE_COLUMNS)
FEATURE_NAMES: Final[tuple[str, ...]] = (
    "Real Power (kW)",
    "Is Gap",
    "Is Holiday",
    "Is Weekend",
    "Is Work Hour",
    "Apparent Power (kVA)",
    "Current Avg (A)",
    "Frequency (Hz)",
    "Power Factor (%)",
    "Voltage L-L Avg (V)",
)

FEATURE_UNITS: Final[dict[str, str]] = {
    "Real Power (kW)": "kW",
    "Apparent Power (kVA)": "kVA",
    "Current Avg (A)": "A",
    "Frequency (Hz)": "Hz",
    "Power Factor (%)": "%",
    "Voltage L-L Avg (V)": "V",
    "Is Gap": "",
    "Is Holiday": "",
    "Is Weekend": "",
    "Is Work Hour": "",
}

# Colour palette
NAVY: Final[str] = "#1e2761"
DEEP: Final[str] = "#141b4d"
AMBER: Final[str] = "#f4a300"
RED: Final[str] = "#d64545"
GREEN: Final[str] = "#1f9d6b"
TEAL: Final[str] = "#0e8e8e"
SLATE: Final[str] = "#5b6584"
BG: Final[str] = "#f5f7fb"

# ── Data types ────────────────────────────────────────────────────────────────

class AnomalyPayload(TypedDict, total=False):
    time: str
    detector_type: str
    counterfactual_data: list[list[float]]
    natural_language_explanation: str | None

class WindowPoint(TypedDict, total=False):
    time: str
    real_power_kw: float | None
    power_factor_pct: float | None
    current_avg_a: float | None
    voltage_ln_avg_v: float | None
    frequency_hz: float | None
    apparent_power_kva: float | None


class MonthlyPoint(TypedDict, total=False):
    """Shape of a monthly telemetry summary returned by FastAPI."""

    month: str
    average_real_power_kw: float | None
    average_power_factor_pct: float | None


@dataclass
class DashboardState:
    event: AnomalyPayload | None = None
    window: list[WindowPoint] = field(default_factory=list)
    monthly: list[MonthlyPoint] = field(default_factory=list)
    error: str | None = None

    @property
    def anomaly_time(self) -> str:
        if self.event:
            return self.event.get("time", "Unknown")
        return "Unknown"

    @property
    def tensor(self) -> list[list[float]]:
        if self.event:
            return self.event.get("counterfactual_data", [])
        return []

    @property
    def explanation(self) -> str:
        if self.event:
            return self.event.get("natural_language_explanation") or ""
        return ""


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_state() -> DashboardState:
    """Fetch anomaly event + telemetry window from FastAPI."""
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            anomaly_resp = client.get(f"{API_BASE}/api/v1/anomalies/{METER}")
            anomaly_resp.raise_for_status()
            anomalies = anomaly_resp.json()
            if not anomalies:
                return DashboardState(error="No anomaly events found for PH-A-MAIN.")

            window_resp = client.get(
                f"{API_BASE}/api/v1/telemetry/{METER}/anomaly-window",
                params={"hours_before": 24, "hours_after": 24},
            )
            window_data: list[WindowPoint] = []
            if window_resp.status_code == 200:
                window_data = window_resp.json()
            monthly_resp = client.get(
                f"{API_BASE}/api/v1/telemetry/{METER}/monthly",
                params={"months": 24},
            )
            monthly_data: list[MonthlyPoint] = []
            if monthly_resp.status_code == 200:
                monthly_data = monthly_resp.json()

        return DashboardState(
            event=anomalies[0], window=window_data, monthly=monthly_data
        )

    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return DashboardState(error=f"Backend request failed: {type(exc).__name__}: {exc}")


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _parse_deviations(explanation: str) -> list[dict]:
    """Extract feature deviation dicts from the GridReason alert string."""
    deviations = []
    # Match patterns like: real_power_kw +14.4271 (+1442714953422.55%)
    pattern = r"(\w+)\s+([+-]?[\d.]+)\s+\(([+-]?[\d.]+)%\)"
    for match in re.finditer(pattern, explanation):
        name, abs_val, pct_val = match.group(1), float(match.group(2)), float(match.group(3))
        deviations.append({
            "name": name,
            "abs_val": abs_val,
            "pct_val": pct_val,
        })
    return deviations


def _friendly_name(col: str) -> str:
    mapping = {
        "real_power_kw": "Real Power (kW)",
        "power_factor_pct": "Power Factor (%)",
        "current_avg_a": "Current Avg (A)",
        "voltage_ln_avg_v": "Voltage L-N Avg (V)",
        "frequency_hz": "Frequency (Hz)",
        "apparent_power_kva": "Apparent Power (kVA)",
        "is_gap": "Signal Gap",
        "is_holiday": "Holiday Flag",
        "is_weekend": "Weekend Flag",
        "is_workhour": "Work-Hour Flag",
    }
    return mapping.get(col, col.replace("_", " ").title())


def _fmt_pct(val: float) -> str:
    """Format a percentage deviation without absurd scientific notation."""
    if abs(val) > 9999:
        return f"{'&#43;' if val > 0 else '&minus;'}&infin;% (near-zero baseline)"
    sign = "&#43;" if val > 0 else ""
    return f"{sign}{val:.1f}%"


def _hourly_average(
    window: list[WindowPoint], value_key: str
) -> tuple[list[str], list[float]]:
    """Aggregate live telemetry values into UTC hourly averages."""
    buckets: dict[str, list[float]] = {}
    for point in window:
        timestamp = point.get("time")
        value = point.get(value_key)  # type: ignore[literal-required]
        if timestamp and value is not None:
            hour = timestamp[:13] + ":00:00Z"
            buckets.setdefault(hour, []).append(float(value))
    times = sorted(buckets)
    return times, [sum(buckets[time]) / len(buckets[time]) for time in times]


# ── Chart builders ─────────────────────────────────────────────────────────────

CHART_LAYOUT = dict(
    paper_bgcolor="white",
    plot_bgcolor="#f8faff",
    font=dict(family="Inter, sans-serif", color=NAVY, size=12),
    margin=dict(l=60, r=30, t=60, b=50),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    hovermode="x unified",
)


def _build_counterfactual_times(anomaly_iso: str, num_steps: int) -> list[str]:
    """Generate ISO timestamps for each counterfactual step (15-min intervals)."""
    from datetime import timedelta
    try:
        base = datetime.fromisoformat(anomaly_iso.replace("Z", "+00:00"))
    except ValueError:
        return []
    return [
        (base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(num_steps)
    ]


def build_power_chart(state: DashboardState) -> go.Figure:
    """Build Observed vs Expected Real Power (kW) as two continuous lines."""
    fig = go.Figure()

    if not state.window:
        fig.update_layout(
            title="Real Power (kW) — No telemetry data",
            annotations=[dict(text="No telemetry data available", x=0.5, y=0.5,
                              xref="paper", yref="paper", showarrow=False,
                              font=dict(size=16, color=SLATE))],
            **CHART_LAYOUT,
        )
        return fig

    times, powers = _hourly_average(state.window, "real_power_kw")

    # ── Observed line (blue) ───────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=times, y=powers, mode="lines+markers",
        name="Observed (Actual)",
        line=dict(color=NAVY, width=2.5),
        marker=dict(size=5, color=NAVY),
        fill="tozeroy",
        fillcolor="rgba(30,39,97,0.04)",
        hovertemplate="<b>%{x}</b><br>Observed: %{y:.2f} kW<extra></extra>",
    ))

    # ── Expected line (green) from counterfactual tensor ───────────────────────
    tensor = state.tensor
    if tensor and state.anomaly_time != "Unknown":
        cf_times = _build_counterfactual_times(state.anomaly_time, len(tensor))
        cf_powers = [step[0] for step in tensor]  # FEATURE_NAMES[0] = Real Power (kW)

        if cf_times:
            fig.add_trace(go.Scatter(
                x=cf_times, y=cf_powers, mode="lines+markers",
                name="Expected (GrCF Counterfactual)",
                line=dict(color=GREEN, width=2.5, dash="solid"),
                marker=dict(size=5, color=GREEN, symbol="diamond"),
                hovertemplate="<b>%{x}</b><br>Expected: %{y:.2f} kW<extra></extra>",
            ))

    # ── Anomaly marker (subtle vertical line) ─────────────────────────────────
    if state.anomaly_time != "Unknown":
        fig.add_vline(
            x=state.anomaly_time,
            line_dash="dot",
            line_color=RED,
            line_width=2,
            annotation_text="&#9650; ANOMALY",
            annotation_font_color=RED,
            annotation_font_size=11,
        )

    fig.update_layout(
        title=dict(text="Real Power (kW) — Observed vs Expected", font=dict(size=15)),
        xaxis=dict(title="Time (UTC)", showgrid=True, gridcolor="#e8ecf5"),
        yaxis=dict(title="Real Power (kW)", showgrid=True, gridcolor="#e8ecf5", rangemode="tozero"),
        **CHART_LAYOUT,
    )
    return fig


def build_monthly_power_chart(state: DashboardState) -> go.Figure:
    """Build a monthly average real-power chart from live telemetry."""
    fig = go.Figure()
    if not state.monthly:
        fig.update_layout(
            title="Monthly Real Power (kW) — No telemetry data",
            annotations=[dict(text="No monthly telemetry data available", x=0.5, y=0.5,
                              xref="paper", yref="paper", showarrow=False,
                              font=dict(size=16, color=SLATE))],
            **CHART_LAYOUT,
        )
        return fig

    monthly = [
        point for point in state.monthly
        if point.get("average_real_power_kw") is not None
        or point.get("average_power_factor_pct") is not None
    ]
    months = [point.get("month", "") for point in monthly]
    power = [point.get("average_real_power_kw") for point in monthly]
    pf = [point.get("average_power_factor_pct") for point in monthly]
    fig.add_trace(go.Bar(
        x=months,
        y=power,
        name="Monthly Average Real Power",
        marker_color=NAVY,
        opacity=0.9,
        hovertemplate="<b>%{x}</b><br>Average: %{y:.2f} kW<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=months,
        y=pf,
        name="Monthly Average Power Factor",
        mode="lines+markers",
        line=dict(color=TEAL, width=2),
        yaxis="y2",
        hovertemplate="<b>%{x}</b><br>Power factor: %{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text="Monthly Live Energy Profile", font=dict(size=15)),
        xaxis=dict(title="Month (UTC)", showgrid=False),
        yaxis=dict(title="Average Real Power (kW)", showgrid=True, gridcolor="#e8ecf5"),
        yaxis2=dict(title="Average Power Factor (%)", overlaying="y", side="right", range=[0, 105]),
        **CHART_LAYOUT,
    )
    return fig


def build_pf_chart(state: DashboardState) -> go.Figure:
    """Build Observed vs Expected Power Factor (%) as two continuous lines."""
    fig = go.Figure()

    if not state.window:
        fig.update_layout(title="Power Factor (%) — No data", **CHART_LAYOUT)
        return fig

    times, pf_vals = _hourly_average(state.window, "power_factor_pct")

    # ── Observed line (blue/teal) ──────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=times, y=pf_vals, mode="lines+markers",
        name="Observed (Actual)",
        line=dict(color=NAVY, width=2.5),
        marker=dict(size=5, color=NAVY),
        hovertemplate="<b>%{x}</b><br>Observed PF: %{y:.1f}%<extra></extra>",
    ))

    # ── Expected line (green) from counterfactual tensor ───────────────────────
    tensor = state.tensor
    if tensor and state.anomaly_time != "Unknown":
        cf_times = _build_counterfactual_times(state.anomaly_time, len(tensor))
        cf_pf_vals = [step[8] for step in tensor]  # FEATURE_NAMES[8] = Power Factor (%)

        if cf_times:
            fig.add_trace(go.Scatter(
                x=cf_times, y=cf_pf_vals, mode="lines+markers",
                name="Expected (GrCF Counterfactual)",
                line=dict(color=GREEN, width=2.5, dash="solid"),
                marker=dict(size=5, color=GREEN, symbol="diamond"),
                hovertemplate="<b>%{x}</b><br>Expected PF: %{y:.1f}%<extra></extra>",
            ))

    # Healthy PF zone shading (80-100%)
    fig.add_hrect(y0=80, y1=100, fillcolor="rgba(31,157,107,0.06)",
                  line_width=0, annotation_text="Healthy PF Zone (80-100%)",
                  annotation_position="top left",
                  annotation_font=dict(color=GREEN, size=10))

    # ── Anomaly marker (subtle vertical line) ─────────────────────────────────
    if state.anomaly_time != "Unknown":
        fig.add_vline(x=state.anomaly_time, line_dash="dot",
                      line_color=RED, line_width=2,
                      annotation_text="&#9650; ANOMALY",
                      annotation_font_color=RED)

    fig.update_layout(
        title=dict(text="Power Factor (%) — Observed vs Expected", font=dict(size=15)),
        xaxis=dict(title="Time (UTC)", showgrid=True, gridcolor="#e8ecf5"),
        yaxis=dict(title="Power Factor (%)", showgrid=True, gridcolor="#e8ecf5"),
        **CHART_LAYOUT,
    )
    return fig


def build_comparison_chart(state: DashboardState) -> go.Figure:
    """Bar chart comparing observed snapshot vs counterfactual at anomaly time."""
    fig = go.Figure()

    if not state.tensor or not state.window:
        fig.update_layout(title="Observed vs Counterfactual — No data", **CHART_LAYOUT)
        return fig

    tensor = state.tensor
    cf_step = tensor[0]  # First step of counterfactual = anomaly moment

    # Find observed values at anomaly time from telemetry window
    observed_map: dict[str, float | None] = {}
    for p in state.window:
        if state.anomaly_time[:16] in (p.get("time", ""))[:16]:
            observed_map = {
                "Real Power (kW)": p.get("real_power_kw"),
                "Power Factor (%)": p.get("power_factor_pct"),
                "Current Avg (A)": p.get("current_avg_a"),
                "Frequency (Hz)": p.get("frequency_hz"),
                "Apparent Power (kVA)": p.get("apparent_power_kva"),
            }
            break

    # Map counterfactual to same labels
    cf_map = {
        "Real Power (kW)": cf_step[0],
        "Apparent Power (kVA)": cf_step[5],
        "Current Avg (A)": cf_step[6],
        "Frequency (Hz)": cf_step[7],
        "Power Factor (%)": cf_step[8],
    }

    labels = [l for l in cf_map if l in observed_map]
    obs_vals = [observed_map.get(l, 0) or 0 for l in labels]
    cf_vals = [cf_map.get(l, 0) or 0 for l in labels]

    fig.add_trace(go.Bar(
        name="Observed (Anomalous)",
        x=labels, y=obs_vals,
        marker_color=RED,
        opacity=0.85,
        hovertemplate="<b>Observed</b><br>%{x}: %{y:.3f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="GrCF Counterfactual (Expected Normal)",
        x=labels, y=cf_vals,
        marker_color=GREEN,
        opacity=0.85,
        hovertemplate="<b>Counterfactual</b><br>%{x}: %{y:.3f}<extra></extra>",
    ))

    fig.update_layout(
        barmode="group",
        title=dict(text="Observed vs GrCF Counterfactual — Anomaly Snapshot", font=dict(size=15)),
        xaxis=dict(title="Parameter"),
        yaxis=dict(title="Value"),
        **CHART_LAYOUT,
    )
    return fig


# ── HTML builders ─────────────────────────────────────────────────────────────

def build_kpi_strip(state: DashboardState) -> str:
    if state.error:
        return f"""
        <div class="kpi-strip">
            <div class="kpi-card kpi-bad"><div class="kpi-value">OFFLINE</div><div class="kpi-label">Backend connection failed</div></div>
        </div>"""

    ts = state.anomaly_time.replace("T", " ").replace("Z", " UTC")[:19] + " UTC"
    return f"""
    <div class="kpi-strip">
        <div class="kpi-card kpi-live"><div class="kpi-value">&#9679; LIVE</div><div class="kpi-label">FastAPI connected</div></div>
        <div class="kpi-card"><div class="kpi-value">CAFA</div><div class="kpi-label">XGBoost + Isolation Forest</div></div>
        <div class="kpi-card"><div class="kpi-value">GrCF</div><div class="kpi-label">DDPM-LSTM Counterfactual</div></div>
        <div class="kpi-card"><div class="kpi-value">{len(state.tensor)} &times; {len(state.tensor[0]) if state.tensor else 0}</div><div class="kpi-label">Counterfactual tensor</div></div>
        <div class="kpi-card kpi-alert"><div class="kpi-value">&#9888; ANOMALY</div><div class="kpi-label">{ts}</div></div>
    </div>"""


def build_alert_panel(state: DashboardState) -> str:
    if state.error:
        return f"""
        <div class="section-card offline-card">
            <h2>&#9888; Backend Offline</h2>
            <p>Start FastAPI: <code>uvicorn api.main:app --host 127.0.0.1 --port 8000</code></p>
            <p class="muted">{state.error}</p>
        </div>"""

    deviations = _parse_deviations(state.explanation)
    ts = state.anomaly_time.replace("T", " ").replace("Z", " UTC")

    # Build deviation cards
    dev_html = ""
    for d in deviations:
        sign_class = "dev-pos" if d["abs_val"] > 0 else "dev-neg"
        sign_sym = "&#43;" if d["abs_val"] > 0 else ""
        pct_str = _fmt_pct(d["pct_val"])
        dev_html += f"""
        <div class="dev-card {sign_class}">
            <div class="dev-feature">{_friendly_name(d['name'])}</div>
            <div class="dev-value">{sign_sym}{d['abs_val']:.3f}</div>
            <div class="dev-pct">{pct_str}</div>
        </div>"""

    return f"""
    <div class="section-card alert-card">
        <div class="section-badge badge-red">&#9888; ACTIVE ANOMALY DETECTED</div>
        <h2>What Was Detected?</h2>
        <div class="meta-row">
            <span class="meta-chip">&#128337; {ts}</span>
            <span class="meta-chip">&#128204; Meter: PH-A-MAIN</span>
            <span class="meta-chip">&#129302; Detector: CAFA-GrCF</span>
        </div>
        <div class="explanation-box">
            <p>{state.explanation or "No GridReason explanation available."}</p>
        </div>
        <h3>Feature Deviations</h3>
        <p class="muted">Difference between <strong>observed reading</strong> and GrCF-predicted <strong>normal baseline</strong>:</p>
        <div class="dev-strip">{dev_html}</div>
    </div>"""


def build_methodology_panel() -> str:
    return """
    <div class="section-card">
        <div class="section-badge badge-blue">ENTWINE INTELLIGENCE PIPELINE</div>
        <h2>How Was The Anomaly Detected?</h2>
        <p class="muted">The ENTWINE system runs a 4-stage pipeline that detects, explains, and translates energy anomalies in real time:</p>
        <div class="pipeline">

            <div class="pipe-step">
                <div class="pipe-num">01</div>
                <div class="pipe-body">
                    <div class="pipe-title">Historical Telemetry Ingestion</div>
                    <div class="pipe-desc">502,920 rows of 15-minute interval energy readings (Real Power, Current, Voltage, Power Factor, Frequency) from the A Block main meter (PH-A-MAIN) are loaded into TimescaleDB. All timestamps are UTC-normalised.</div>
                    <div class="pipe-tech">15-min intervals &middot; 365 days &middot; TimescaleDB hypertable</div>
                </div>
            </div>

            <div class="pipe-arrow">&#8595;</div>

            <div class="pipe-step">
                <div class="pipe-num pipe-num-amber">02</div>
                <div class="pipe-body">
                    <div class="pipe-title">CAFA — Contextual Anomaly Feature Analysis</div>
                    <div class="pipe-desc">
                        An <strong>XGBoost Regressor</strong> is trained on temporal and lag features (hour, weekday, 15-min lag, 1-hour rolling mean) to predict the <em>expected Real Power</em> for each timestamp. The residual (Actual &minus; Predicted) is then scored by an <strong>Isolation Forest</strong>, which partitions the feature space using random trees. Points that are isolated quickly (short average path length) are flagged as anomalous.
                    </div>
                    <div class="pipe-tech">XGBoost Regressor &middot; Isolation Forest &middot; 1,078 anomaly windows identified</div>
                </div>
            </div>

            <div class="pipe-arrow">&#8595;</div>

            <div class="pipe-step">
                <div class="pipe-num pipe-num-green">03</div>
                <div class="pipe-body">
                    <div class="pipe-title">GrCF — Granger Causal Counterfactual</div>
                    <div class="pipe-desc">
                        For the highest-score anomaly, GrCF generates a <strong>counterfactual trajectory</strong> — what 16 consecutive 15-minute readings would look like if the system had been operating normally. It uses a <strong>DDPM-warm-started LSTM</strong> optimiser guided by three penalty terms:
                        <ul>
                            <li><strong>Reconstruction loss</strong> — stay close to the normal distribution of the mentor denoiser</li>
                            <li><strong>Physics penalty</strong> — enforce S = V &times; I (apparent power identity)</li>
                            <li><strong>Causal penalty</strong> — preserve Granger causal relationships between Real Power, Current, Frequency, Power Factor, and Voltage</li>
                        </ul>
                        Output: a (16 &times; 10) tensor — 16 time steps, 10 causal feature values per step.
                    </div>
                    <div class="pipe-tech">DDPM &middot; LSTM &middot; Granger causality graph &middot; Physics constraints</div>
                </div>
            </div>

            <div class="pipe-arrow">&#8595;</div>

            <div class="pipe-step">
                <div class="pipe-num pipe-num-teal">04</div>
                <div class="pipe-body">
                    <div class="pipe-title">GridReason — Natural Language Translation</div>
                    <div class="pipe-desc">
                        GridReason computes the <strong>feature-level delta</strong> between the observed anomalous reading and step 0 of the GrCF counterfactual. The deltas are ranked by absolute magnitude and translated into a deterministic operator alert using a rule-based template: <em>"observed deviation: feature_name &plusmn;abs_value (pct_change%); requires operator review."</em>
                    </div>
                    <div class="pipe-tech">Rule-based delta translation &middot; No LLM hallucination risk</div>
                </div>
            </div>

        </div>
    </div>"""


def build_comparison_table(state: DashboardState) -> str:
    """HTML table: observed snapshot at anomaly vs GrCF counterfactual."""
    if not state.tensor or not state.window:
        return "<div class='section-card'><p class='muted'>No comparison data available.</p></div>"

    tensor = state.tensor
    cf_step = tensor[0]

    # Find observed values
    observed_map: dict[str, str] = {}
    for p in state.window:
        if state.anomaly_time[:16] in (p.get("time", ""))[:16]:
            observed_map = {
                "Real Power (kW)":     f"{p.get('real_power_kw', 0):.3f}" if p.get('real_power_kw') is not None else "—",
                "Apparent Power (kVA)":f"{p.get('apparent_power_kva', 0):.3f}" if p.get('apparent_power_kva') is not None else "—",
                "Current Avg (A)":     f"{p.get('current_avg_a', 0):.3f}" if p.get('current_avg_a') is not None else "—",
                "Frequency (Hz)":      f"{p.get('frequency_hz', 0):.3f}" if p.get('frequency_hz') is not None else "—",
                "Power Factor (%)":    f"{p.get('power_factor_pct', 0):.3f}" if p.get('power_factor_pct') is not None else "—",
            }
            break

    cf_vals = {
        "Real Power (kW)":      cf_step[0],
        "Apparent Power (kVA)": cf_step[5],
        "Current Avg (A)":      cf_step[6],
        "Frequency (Hz)":       cf_step[7],
        "Power Factor (%)":     cf_step[8],
    }

    rows_html = ""
    for label, cf_val in cf_vals.items():
        obs_str = observed_map.get(label, "—")
        cf_str = f"{cf_val:.3f}"
        unit = FEATURE_UNITS.get(label, "")

        # Compute delta
        try:
            obs_num = float(obs_str)
            delta = obs_num - cf_val
            delta_class = "delta-pos" if delta > 0 else "delta-neg"
            delta_str = f"{'&#43;' if delta > 0 else ''}{delta:.3f} {unit}"
        except (ValueError, TypeError):
            delta_class = ""
            delta_str = "—"

        rows_html += f"""
        <tr>
            <td class="tbl-label">{label}</td>
            <td class="tbl-obs">{obs_str} <span class="unit">{unit}</span></td>
            <td class="tbl-cf">{cf_str} <span class="unit">{unit}</span></td>
            <td class="tbl-delta {delta_class}">{delta_str}</td>
        </tr>"""

    return f"""
    <div class="section-card">
        <div class="section-badge badge-green">GrCF COUNTERFACTUAL</div>
        <h2>What Should It Have Been?</h2>
        <p class="muted">Comparison of the <strong>observed anomalous reading</strong> vs the <strong>GrCF-generated counterfactual</strong> (what normal operation would have looked like at <code>{state.anomaly_time}</code>):</p>
        <div class="table-wrap">
            <table class="compare-table">
                <thead>
                    <tr>
                        <th>Parameter</th>
                        <th class="tbl-obs-h">&#9888; Observed (Anomalous)</th>
                        <th class="tbl-cf-h">&#10003; Counterfactual (Expected Normal)</th>
                        <th>Delta</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
        </div>
        <p class="muted" style="margin-top:12px;">The counterfactual was generated by the GrCF DDPM-LSTM model constrained by physics penalties (S = V &times; I) and Granger causal relationships. A <strong>positive delta</strong> means the actual reading was higher than normal; <strong>negative</strong> means lower.</p>
    </div>"""


def build_tensor_viewer(state: DashboardState) -> list:
    """Return the counterfactual tensor as a 2D list for gr.DataFrame."""
    if not state.tensor:
        return []
    return [[round(v, 4) for v in step] for step in state.tensor]


# ── CSS ────────────────────────────────────────────────────────────────────────

CSS: Final[str] = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {
    --navy:#1e2761; --deep:#141b4d; --slate:#5b6584; --bg:#f5f7fb;
    --panel:#fff; --border:#dce3f0; --amber:#f4a300; --green:#1f9d6b;
    --teal:#0e8e8e; --red:#d64545; --alt:#eef2fa;
}
* { box-sizing:border-box; }
body, .gradio-container { font-family:'Inter',sans-serif !important; background:var(--bg) !important; }
.gradio-container { max-width:1340px !important; padding:0 20px 60px !important; margin:0 auto !important; }

/* Hero */
.hero { background:var(--deep); margin:0 -20px 28px; padding:22px 32px 20px;
    border-bottom:3px solid var(--amber); }
.hero-inner { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; }
.hero-badge { color:var(--amber) !important; border:1px solid rgba(244,163,0,.4);
    background:rgba(244,163,0,.1); padding:4px 10px; border-radius:3px;
    font-family:monospace; font-size:10px; font-weight:700; letter-spacing:1px; }
.hero h1 { color:#fff !important; margin:8px 0 4px; font-size:26px; font-weight:800; }
.hero p { color:#b9c2e0 !important; margin:0; font-size:12px; }

/* KPI strip */
.kpi-strip { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }
.kpi-card { background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:12px 16px; min-width:140px; flex:1; }
.kpi-value { font-size:20px; font-weight:800; color:var(--navy) !important; font-family:monospace; }
.kpi-label { font-size:10px; color:var(--slate) !important; margin-top:4px; }
.kpi-live .kpi-value { color:var(--green) !important; }
.kpi-alert .kpi-value { color:var(--red) !important; }
.kpi-bad .kpi-value { color:var(--red) !important; }

/* Section cards */
.section-card { background:var(--panel); border:1px solid var(--border);
    border-radius:12px; padding:24px 28px; margin-bottom:20px;
    box-shadow:0 2px 10px rgba(20,27,77,.05); }
.section-card h2 { color:var(--navy) !important; font-size:20px; font-weight:700;
    margin:8px 0 16px; font-family:Georgia,serif; }
.section-card h3 { color:var(--navy) !important; font-size:15px; font-weight:600; margin:18px 0 8px; }
.alert-card { border-top:4px solid var(--red) !important; }
.offline-card { border-top:4px solid var(--slate) !important; }

/* Badges */
.section-badge { display:inline-block; font-size:10px; font-weight:700; letter-spacing:1.2px;
    padding:4px 10px; border-radius:3px; margin-bottom:4px; }
.badge-red { background:rgba(214,69,69,.12); color:var(--red) !important; }
.badge-blue { background:rgba(30,39,97,.1); color:var(--navy) !important; }
.badge-green { background:rgba(31,157,107,.12); color:var(--green) !important; }

/* Meta chips */
.meta-row { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
.meta-chip { background:var(--alt); border:1px solid var(--border); border-radius:20px;
    padding:4px 12px; font-size:12px; color:var(--slate) !important; }

/* Explanation box */
.explanation-box { background:#fef9f0; border:1px solid #f4d08a; border-left:4px solid var(--amber);
    border-radius:6px; padding:14px 16px; margin-bottom:20px; }
.explanation-box p { margin:0; color:var(--navy) !important; font-size:13px; line-height:1.7; }

/* Deviation cards */
.dev-strip { display:flex; gap:10px; flex-wrap:wrap; }
.dev-card { border-radius:8px; padding:12px 16px; min-width:150px; border:1px solid var(--border); }
.dev-pos { background:#fff5f5; border-color:#f0a0a0; }
.dev-neg { background:#f0fff8; border-color:#80d4ad; }
.dev-feature { font-size:11px; color:var(--slate) !important; font-weight:600; margin-bottom:4px; }
.dev-value { font-size:22px; font-weight:800; font-family:monospace; color:var(--navy) !important; }
.dev-pos .dev-value { color:var(--red) !important; }
.dev-neg .dev-value { color:var(--green) !important; }
.dev-pct { font-size:11px; margin-top:2px; }
.dev-pos .dev-pct { color:var(--red) !important; }
.dev-neg .dev-pct { color:var(--green) !important; }

/* Pipeline */
.pipeline { display:flex; flex-direction:column; gap:0; }
.pipe-step { display:flex; gap:18px; align-items:flex-start; background:var(--alt);
    border:1px solid var(--border); border-radius:10px; padding:18px 20px; }
.pipe-arrow { text-align:center; font-size:22px; color:var(--slate); padding:6px 0; }
.pipe-num { font-size:28px; font-weight:900; color:var(--navy) !important;
    font-family:monospace; min-width:44px; opacity:.35; padding-top:2px; }
.pipe-num-amber { color:var(--amber) !important; opacity:1; }
.pipe-num-green { color:var(--green) !important; opacity:1; }
.pipe-num-teal { color:var(--teal) !important; opacity:1; }
.pipe-title { font-size:14px; font-weight:700; color:var(--navy) !important; margin-bottom:8px; }
.pipe-desc { font-size:12.5px; color:#1e2761 !important; line-height:1.75; }
.pipe-desc strong { color:var(--teal) !important; font-weight:800; }
.pipe-desc em { color:#9a6500 !important; font-style:normal; font-weight:700; }
.pipe-desc li strong { color:var(--green) !important; }
.pipe-desc ul { margin:8px 0 0 16px; padding:0; }
.pipe-desc li { margin-bottom:4px; }
.pipe-tech { margin-top:10px; font-size:10px; font-weight:700; letter-spacing:.8px;
    color:var(--teal) !important; background:var(--teal-soft); border:1px solid rgba(14,142,142,.3);
    border-radius:3px; padding:3px 8px; display:inline-block; }
.pipe-body { flex:1; }

/* Comparison table */
.table-wrap { overflow-x:auto; }
.compare-table { width:100%; border-collapse:collapse; font-size:13px; }
.compare-table thead th { background:var(--deep); color:#fff !important; padding:10px 14px;
    text-align:left; font-size:12px; font-weight:600; }
.tbl-obs-h { background:#d64545 !important; }
.tbl-cf-h { background:#1f9d6b !important; }
.compare-table tbody tr:nth-child(even) { background:var(--alt); }
.compare-table tbody td { padding:10px 14px; border-bottom:1px solid var(--border);
    color:var(--navy) !important; }
.tbl-label { font-weight:600; }
.tbl-obs { color:var(--red) !important; font-family:monospace; }
.tbl-cf { color:var(--green) !important; font-family:monospace; }
.tbl-delta { font-family:monospace; font-weight:700; }
.delta-pos { color:var(--red) !important; }
.delta-neg { color:var(--green) !important; }
.unit { font-size:11px; color:var(--slate) !important; margin-left:2px; }

/* Misc */
.muted { color:var(--slate) !important; font-size:13px; line-height:1.6; }
.gradio-container code { background:var(--alt) !important; border:1px solid var(--border) !important;
    border-radius:3px; padding:1px 5px; font-family:monospace; color:var(--navy) !important; }
.refresh-btn { background:var(--navy) !important; color:#fff !important;
    font-weight:700 !important; border:none !important; border-radius:8px !important;
    padding:10px 24px !important; font-size:14px !important; }
.refresh-btn:hover { background:var(--teal) !important; }
footer { display:none !important; }
.gradio-container label, .label-wrap span { color:var(--slate) !important; }
.gradio-container .block, .gradio-container .form { border-color:transparent !important; }
"""

# ── Main refresh function ─────────────────────────────────────────────────────

def refresh_all() -> tuple[str, str, str, go.Figure, go.Figure, go.Figure, go.Figure, str, list[list[float]]]:
    """Fetch all data and return values for every dashboard component."""
    state = fetch_state()
    return (
        build_kpi_strip(state),
        build_alert_panel(state),
        build_methodology_panel(),
        build_power_chart(state),
        build_pf_chart(state),
        build_comparison_chart(state),
        build_monthly_power_chart(state),
        build_comparison_table(state),
        build_tensor_viewer(state),
    )


def refresh_dashboard() -> tuple[str, str, str, go.Figure, go.Figure, go.Figure, go.Figure, str, list[list[float]]]:
    """Compatibility alias for callers that refresh the dashboard directly."""
    return refresh_all()


# ── Dashboard layout ──────────────────────────────────────────────────────────

def build_dashboard() -> gr.Blocks:
    """Construct the full judge-ready ENTWINE dashboard."""

    # Pre-fetch initial state so the dashboard loads populated
    state = fetch_state()
    init_kpis = build_kpi_strip(state)
    init_alert = build_alert_panel(state)
    init_method = build_methodology_panel()
    init_power = build_power_chart(state)
    init_pf = build_pf_chart(state)
    init_comp = build_comparison_chart(state)
    init_monthly = build_monthly_power_chart(state)
    init_table = build_comparison_table(state)
    init_tensor = build_tensor_viewer(state)

    with gr.Blocks(title="ENTWINE Digital Twin: Powerhouse A Block") as dashboard:

        # ── Hero ──────────────────────────────────────────────────────────────
        gr.HTML("""
        <section class="hero">
            <div class="hero-inner">
                <div>
                    <div class="hero-badge">KCT POWERHOUSE &middot; BLOCK A &middot; LIVE RESEARCH OUTPUT</div>
                    <h1>ENTWINE Digital Twin: Powerhouse A Block</h1>
                    <p>Operator console &middot; CAFA anomaly detection &middot; GrCF counterfactual explanation &middot; GridReason alerts &middot; PH-A-MAIN</p>
                </div>
                <div style="text-align:right">
                    <div style="color:#b9c2e0;font-size:11px;">Powerhouse 1 &middot; Kumaraguru College of Technology</div>
                    <div style="color:var(--amber);font-size:12px;font-weight:700;">Module 3 &middot; Intelligence Dashboard</div>
                </div>
            </div>
        </section>""")

        # ── KPI Strip ─────────────────────────────────────────────────────────
        kpis = gr.HTML(value=init_kpis)

        # ── Refresh button ────────────────────────────────────────────────────
        with gr.Row():
            refresh_btn = gr.Button("Refresh Data", elem_classes=["refresh-btn"])
            gr.Markdown(
                "_Dashboard auto-loads from FastAPI backend. "
                "Click Refresh to re-poll for the latest anomaly event._",
                elem_classes=["muted"],
            )

        # ── Section 1: What Was Detected ─────────────────────────────────────
        alert_html = gr.HTML(value=init_alert)

        # ── Section 2: How Was It Detected (Methodology) ─────────────────────
        method_html = gr.HTML(value=init_method)

        # ── Section 3: Charts ─────────────────────────────────────────────────
        gr.HTML("""
        <div class="section-card" style="padding-bottom:8px">
            <div class="section-badge badge-blue">INTERACTIVE CHARTS</div>
            <h2>Show Me the Data</h2>
            <p class="muted">
                The charts below compare <span style="color:#1e2761;font-weight:700;">Observed (Actual)</span> readings
                against the <span style="color:#1f9d6b;font-weight:700;">Expected (GrCF Counterfactual)</span> prediction
                across the anomaly window. The <span style="color:#d64545;font-weight:700;">red dotted line</span> marks
                when the anomaly was detected. Where the two lines diverge, it indicates abnormal behaviour.
            </p>
        </div>""")

        with gr.Row():
            power_chart = gr.Plot(value=init_power, label="")
        with gr.Row():
            pf_chart = gr.Plot(value=init_pf, label="")
        with gr.Row():
            comp_chart = gr.Plot(value=init_comp, label="")
        with gr.Row():
            monthly_chart = gr.Plot(value=init_monthly, label="")

        # ── Section 4: Observed vs Counterfactual Table ───────────────────────
        table_html = gr.HTML(value=init_table)

        # ── Section 5: Raw Tensor ─────────────────────────────────────────────
        with gr.Accordion("&#128202; Raw Counterfactual Tensor (16 &times; 10)", open=False):
            gr.Markdown(
                "Each row is a 15-minute time-step. Each column is one of the 10 causal features "
                "in the order: **Real Power (kW)**, Is Gap, Is Holiday, Is Weekend, Is Work Hour, "
                "**Apparent Power (kVA)**, **Current Avg (A)**, **Frequency (Hz)**, "
                "**Power Factor (%)**, **Voltage L-L Avg (V)**. "
                "This is the GrCF output — the reconstructed normal trajectory.",
                elem_classes=["muted"],
            )
            tensor_df = gr.DataFrame(
                value=init_tensor,
                headers=list(FEATURE_NAMES),
                label="GrCF Counterfactual Tensor",
                interactive=False,
            )

        # ── Wire up Refresh ───────────────────────────────────────────────────
        outputs = [kpis, alert_html, method_html, power_chart, pf_chart,
               comp_chart, monthly_chart, table_html, tensor_df]

        dashboard.load(fn=refresh_all, inputs=[], outputs=outputs)
        refresh_btn.click(fn=refresh_all, inputs=[], outputs=outputs)

    return dashboard


if __name__ == "__main__":
    build_dashboard().launch(
        server_name="127.0.0.1",
        server_port=7860,
        css=CSS,
        theme=gr.themes.Monochrome(
            primary_hue="blue",
            secondary_hue="teal",
            neutral_hue="slate",
        ),
    )
