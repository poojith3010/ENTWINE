"""Live Gradio presentation layer for the ENTWINE Powerhouse A Block twin."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, TypedDict

import gradio as gr
import httpx

API_URL: Final[str] = "http://127.0.0.1:8000/api/v1/anomalies/PH-A-MAIN"
REQUEST_TIMEOUT: Final[float] = 8.0


class AnomalyPayload(TypedDict, total=False):
    """Shape of an anomaly event returned by the FastAPI backend."""

    time: str
    detector_type: str
    counterfactual_data: list[list[float]]
    natural_language_explanation: str | None


@dataclass(frozen=True)
class DashboardState:
    """Values rendered by the dashboard after one API poll."""

    alert: str
    metadata: str
    counterfactual: str
    status: str
    kpis: str


CSS: Final[str] = """
:root { --bg:#f5f7fb; --panel:#fff; --alt:#eef2fa; --border:#dce3f0; --navy:#1e2761; --deep:#141b4d; --slate:#5b6584; --amber:#f4a300; --green:#1f9d6b; --teal:#0e8e8e; --red:#d64545; }
* { box-sizing:border-box; }
body, .gradio-container, .gradio-container * { color:var(--navy) !important; }
.gradio-container { max-width:1240px !important; min-height:100vh !important; padding:0 24px 52px !important; margin:0 auto !important; background:var(--bg); }
.hero { background:var(--deep); margin:0 -24px 24px; padding:20px 30px; display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; border-bottom:3px solid var(--amber); }
.hero h1 { color:#fff !important; margin:7px 0 3px; font-family:Georgia,serif; font-size:25px; }
.hero p { color:#b9c2e0 !important; margin:0; font-size:12px; }
.hero-badge { color:var(--amber) !important; border:1px solid rgba(244,163,0,.45); background:rgba(244,163,0,.12); padding:5px 9px; border-radius:3px; font-family:monospace; font-size:10px; font-weight:700; }
.intro-panel { background:var(--panel) !important; border:1px solid var(--border) !important; border-radius:10px !important; padding:14px 18px !important; margin-bottom:14px; }
.kpi-strip { margin-bottom:18px; }
.kpi-card { background:var(--panel) !important; border:1px solid var(--border) !important; border-radius:10px !important; padding:13px 15px !important; min-height:78px; }
.kpi-value { color:var(--navy) !important; font-family:monospace; font-size:21px; font-weight:800; }
.kpi-label { color:var(--slate) !important; font-size:11px; line-height:1.35; margin-top:4px; }
.alert-panel, .data-panel { border:1px solid var(--border) !important; border-radius:10px !important; background:var(--panel) !important; box-shadow:0 2px 8px rgba(20,27,77,.04); padding:16px !important; }
.alert-panel { border-top:4px solid var(--red) !important; }
.alert-panel h2, .data-panel h2 { color:var(--navy) !important; font-family:Georgia,serif; font-size:22px !important; letter-spacing:0; }
.alert-panel h3 { color:var(--red) !important; font-size:19px !important; }
.alert-panel p, .alert-panel strong, .data-panel p, .data-panel strong { color:var(--navy) !important; }
.alert-panel code, .data-panel code { color:var(--navy) !important; background:var(--alt) !important; border:1px solid var(--border); }
.status-ok { color:var(--green) !important; font-weight:700; }
.status-bad { color:var(--red) !important; font-weight:700; }
.refresh-button { background:var(--navy) !important; color:#fff !important; font-weight:700 !important; border:none !important; border-radius:6px !important; margin-top:10px !important; }
.refresh-button:hover { background:var(--teal) !important; }
.gradio-container button { border-radius:6px !important; }
.gradio-container .json-holder { background:#fbfcff !important; border:1px solid var(--border) !important; font-family:Consolas,monospace; font-size:12px; }
.gradio-container label, .gradio-container .label-wrap span { color:var(--slate) !important; }
.gradio-container .block, .gradio-container .form { border-color:transparent !important; }
footer { display:none !important; }
"""


def _empty_state(message: str, *, offline: bool = False) -> DashboardState:
    """Build a safe empty dashboard state for unavailable or empty data."""
    status_class = "status-bad" if offline else ""
    return DashboardState(
        alert=("### Backend Offline\n\nStart FastAPI with `uvicorn api.main:app --reload`." if offline else "### No active anomaly\n\nNo event was returned for `PH-A-MAIN`."),
        metadata="No event metadata available.",
        counterfactual="[]",
        status=f'<span class="{status_class}">{message}</span>',
        kpis="<div class='kpi-card'><div class='kpi-value'>—</div><div class='kpi-label'>No live event loaded</div></div>",
    )


def _format_metadata(event: AnomalyPayload) -> str:
    """Format event identity fields for compact operator scanning."""
    return (f"**Event time (UTC)**  \n`{event.get('time', 'Unknown')}`\n\n"
            f"**Detector**  \n`{event.get('detector_type', 'Unknown')}`\n\n"
            "**Meter**  \n`PH-A-MAIN`")


def fetch_latest_anomaly() -> DashboardState:
    """Fetch the newest anomaly and convert it into dashboard values."""
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(API_URL)
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            return _empty_state("Connected, but no anomaly event was returned.")
        event = payload[0]
        if not isinstance(event, dict):
            return _empty_state("Backend returned an invalid anomaly payload.")
        typed_event = AnomalyPayload(
            time=str(event.get("time", "Unknown")),
            detector_type=str(event.get("detector_type", "Unknown")),
            counterfactual_data=event.get("counterfactual_data", []),
            natural_language_explanation=event.get("natural_language_explanation"),
        )
        explanation = typed_event.get("natural_language_explanation") or "### Explanation pending\n\nNo GridReason explanation has been written for this event yet."
        tensor = typed_event.get("counterfactual_data", [])
        return DashboardState(
            alert=f"### Active anomaly\n\n{explanation}",
            metadata=_format_metadata(typed_event),
            counterfactual=json.dumps(tensor, indent=2),
            status='<span class="status-ok">● Live connection · latest event loaded</span>',
            kpis=("<div class='kpi-card'><div class='kpi-value'>LIVE</div><div class='kpi-label'>FastAPI connection</div></div>"
                   "<div class='kpi-card'><div class='kpi-value'>CAFA</div><div class='kpi-label'>Detection engine</div></div>"
                   "<div class='kpi-card'><div class='kpi-value'>GrCF</div><div class='kpi-label'>Explanation engine</div></div>"
                   f"<div class='kpi-card'><div class='kpi-value'>{len(tensor)} × {len(tensor[0]) if tensor else 0}</div><div class='kpi-label'>Counterfactual tensor</div></div>"),
        )
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _empty_state(f"Backend request failed: {type(exc).__name__}.", offline=True)


def refresh_dashboard() -> tuple[str, str, str, str, str]:
    """Poll the API and return values for all dashboard components."""
    state = fetch_latest_anomaly()
    return state.alert, state.metadata, state.counterfactual, state.status, state.kpis


def build_dashboard() -> gr.Blocks:
    """Construct the live institutional-style ENTWINE dashboard."""
    with gr.Blocks(title="ENTWINE Digital Twin: Powerhouse A Block") as dashboard:
        gr.HTML("""<section class="hero"><div><div class="hero-badge">KCT POWERHOUSE · LIVE RESEARCH OUTPUT</div><h1>ENTWINE Digital Twin: Powerhouse A Block</h1><p>Operator console · CAFA detection · GrCF explanation · PH-A-MAIN</p></div></section>""")
        gr.Markdown("**Live intelligence output** from the FastAPI backend. This view presents the newest persisted event and its generated counterfactual.", elem_classes=["intro-panel"])
        kpis = gr.HTML(value="<div class='kpi-card'><div class='kpi-value'>LOADING</div><div class='kpi-label'>Polling intelligence layer</div></div>", elem_classes=["kpi-strip"])
        with gr.Row():
            with gr.Column(scale=2, elem_classes=["alert-panel"]):
                gr.Markdown("## Current intelligence alert")
                alert = gr.Markdown(value="Loading latest event...")
            with gr.Column(scale=1, elem_classes=["data-panel"]):
                gr.Markdown("## Event details")
                metadata = gr.Markdown(value="Connecting to FastAPI...")
        with gr.Row():
            with gr.Column(scale=2, elem_classes=["data-panel"]):
                gr.Markdown("## Counterfactual state tensor")
                counterfactual = gr.JSON(value=[], label="16-step GrCF output")
            with gr.Column(scale=1, elem_classes=["data-panel"]):
                gr.Markdown("## Service status")
                status = gr.Markdown(value="Checking backend...")
                refresh = gr.Button("Refresh Data", elem_classes=["refresh-button"])
                gr.Markdown("Newest persisted event returned by the FastAPI backend.")
        refresh.click(fn=refresh_dashboard, inputs=[], outputs=[alert, metadata, counterfactual, status, kpis])
    return dashboard


if __name__ == "__main__":
    build_dashboard().launch(server_name="127.0.0.1", server_port=7860, css=CSS, theme=gr.themes.Monochrome(primary_hue="blue", secondary_hue="teal", neutral_hue="slate"))
