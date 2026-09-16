"""Interactive Gradio dashboard for the ENTWINE Powerhouse A Block twin.

The dashboard is intentionally API-only: it reads the latest anomaly event from
FastAPI and does not connect directly to PostgreSQL. A backend outage is shown
as a visible dashboard state instead of raising an unhandled callback error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, TypedDict

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


CSS: Final[str] = """
:root {
    --ink: #e8f1f7;
    --muted: #91a8b7;
    --line: #284657;
    --paper: #07151f;
    --panel: #0d202c;
    --panel-2: #102a39;
    --navy: #061019;
    --cyan: #38d9e8;
    --orange: #ff8a5b;
    --green: #65e6b0;
}
body, .gradio-container, .gradio-container * { color: var(--ink) !important; }
.gradio-container {
    min-height: 100vh !important;
    max-width: 1380px !important;
    padding: 28px 34px 44px !important;
    margin: 0 auto !important;
    background:
        radial-gradient(circle at 92% 4%, rgba(56, 217, 232, 0.13), transparent 26%),
        radial-gradient(circle at 6% 80%, rgba(255, 138, 91, 0.07), transparent 25%),
        linear-gradient(145deg, #061019 0%, #091923 52%, #07151f 100%);
}
.gradio-container > .prose { max-width: none !important; }
.hero {
    position: relative;
    overflow: hidden;
    background: linear-gradient(115deg, #0d2939 0%, #0c4554 55%, #087f8c 100%);
    border: 1px solid rgba(93, 229, 239, 0.32);
    border-radius: 20px;
    padding: 30px 34px 28px;
    margin-bottom: 18px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.24), inset 0 1px 0 rgba(255,255,255,.08);
}
.hero::after {
    content: "";
    position: absolute;
    right: -70px;
    top: -100px;
    width: 280px;
    height: 280px;
    border: 1px solid rgba(255,255,255,.18);
    border-radius: 50%;
    box-shadow: 0 0 0 20px rgba(255,255,255,.04), 0 0 0 42px rgba(255,255,255,.025);
}
.gradio-container {
    color-scheme: dark;
}
.hero h1 { color: #f5fcff !important; margin: 0 0 8px; letter-spacing: .02em; font-size: 30px; }
.hero p { color: #c7f7fa !important; margin: 0; font-size: 14px; letter-spacing: .04em; }
.hero-badge { color: #8ff7ef !important; font-size: 11px; letter-spacing: .16em; text-transform: uppercase; margin-bottom: 13px; }
.alert-panel, .data-panel {
    border: 1px solid var(--line) !important;
    border-radius: 14px !important;
    background: linear-gradient(145deg, rgba(16,42,57,.96), rgba(9,27,38,.96)) !important;
    box-shadow: 0 14px 35px rgba(0,0,0,.18), inset 0 1px 0 rgba(255,255,255,.035);
    padding: 18px !important;
}
.alert-panel { border-top: 4px solid var(--orange) !important; }
.alert-panel h2, .data-panel h2 { color: #bdeaf0 !important; font-size: 14px !important; text-transform: uppercase; letter-spacing: .12em; }
.alert-panel h3 { color: #ffad86 !important; font-size: 20px !important; }
.alert-panel p, .alert-panel strong, .data-panel p, .data-panel strong { color: var(--ink) !important; }
.alert-panel code, .data-panel code { color: #8ff7ef !important; background: rgba(56,217,232,.1) !important; border: 1px solid rgba(56,217,232,.18); }
.status-ok { color: var(--green) !important; font-weight: 700; }
.status-bad { color: #ff9b85 !important; font-weight: 700; }
.refresh-button {
    background: linear-gradient(135deg, #19b9cb, #087f8c) !important;
    border: 1px solid #58e5ed !important;
    color: #041217 !important;
    font-weight: 800 !important;
    letter-spacing: .04em;
    box-shadow: 0 8px 20px rgba(17, 194, 211, .2);
}
.refresh-button:hover { filter: brightness(1.12); transform: translateY(-1px); }
.gradio-container button { border-radius: 9px !important; }
.gradio-container textarea, .gradio-container input, .gradio-container .wrap, .gradio-container .json-holder {
    background: #071821 !important;
    border-color: #315365 !important;
    color: var(--ink) !important;
}
.gradio-container .json-holder { font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
.gradio-container label, .gradio-container .label-wrap span { color: var(--muted) !important; }
.gradio-container .block, .gradio-container .form { border-color: transparent !important; }
.data-panel > .prose { margin-bottom: 12px; }
footer { display: none !important; }
"""


def _empty_state(message: str, *, offline: bool = False) -> DashboardState:
    """Build a safe empty dashboard state for unavailable or empty data."""
    status_class = "status-bad" if offline else ""
    return DashboardState(
        alert="### Backend Offline\n\nThe FastAPI service is unavailable. Start it with `uvicorn api.main:app --reload` and refresh this dashboard."
        if offline
        else "### No active anomaly\n\nThe backend returned no anomaly events for `PH-A-MAIN`.",
        metadata="No event metadata available.",
        counterfactual="[]",
        status=f'<span class="{status_class}">{message}</span>',
    )



def _format_metadata(event: AnomalyPayload) -> str:
    """Format the event identity fields for compact operator scanning."""
    event_time = event.get("time", "Unknown")
    detector = event.get("detector_type", "Unknown")
    return (
        f"**Event time (UTC)**  \n`{event_time}`\n\n"
        f"**Detector**  \n`{detector}`\n\n"
        "**Meter**  \n`PH-A-MAIN`"
    )



def fetch_latest_anomaly() -> DashboardState:
    """Fetch the newest anomaly and convert it into dashboard output values."""
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
            natural_language_explanation=event.get(
                "natural_language_explanation"
            ),
        )
        explanation = typed_event.get("natural_language_explanation")
        alert = explanation or "### Explanation pending\n\nNo GridReason explanation has been written for this event yet."
        counterfactual: Any = typed_event.get("counterfactual_data", [])
        return DashboardState(
            alert=f"### Active anomaly\n\n{alert}",
            metadata=_format_metadata(typed_event),
            counterfactual=json.dumps(counterfactual, indent=2),
            status='<span class="status-ok">● Live connection · latest event loaded</span>',
        )
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _empty_state(f"Backend request failed: {type(exc).__name__}.", offline=True)



def refresh_dashboard() -> tuple[str, str, str, str]:
    """Poll the API and return values for all dashboard components."""
    state = fetch_latest_anomaly()
    return state.alert, state.metadata, state.counterfactual, state.status



def build_dashboard() -> gr.Blocks:
    """Construct the ENTWINE operator dashboard as a Gradio Blocks app."""
    with gr.Blocks(
        title="ENTWINE Digital Twin: Powerhouse A Block",
    ) as dashboard:
        gr.HTML(
            """
                        <section class="hero">
                            <div class="hero-badge">LIVE ENERGY INTELLIGENCE · PHASE 2</div>
              <h1>ENTWINE Digital Twin: Powerhouse A Block</h1>
                            <p>Operator console · CAFA detection · GrCF explanation · PH-A-MAIN</p>
            </section>
            """
        )
        with gr.Row():
            with gr.Column(scale=2, elem_classes=["alert-panel"]):
                gr.Markdown("## Current intelligence alert")
                alert = gr.Markdown(value="Loading latest event...")
            with gr.Column(scale=1, elem_classes=["data-panel"]):
                gr.Markdown("## Event details")
                metadata = gr.Markdown(value="Connecting to FastAPI...")
        with gr.Row():
            with gr.Column(elem_classes=["data-panel"]):
                gr.Markdown("## Counterfactual state tensor")
                counterfactual = gr.JSON(value=[], label="16-step GrCF output")
            with gr.Column(elem_classes=["data-panel"]):
                gr.Markdown("## Service status")
                status = gr.Markdown(value="Checking backend...")
                refresh = gr.Button("Refresh Data", variant="primary", elem_classes=["refresh-button"])
                gr.Markdown(
                    "The view displays the newest persisted event returned by the FastAPI backend."
                )

        refresh.click(
            fn=refresh_dashboard,
            inputs=[],
            outputs=[alert, metadata, counterfactual, status],
        )
    return dashboard


if __name__ == "__main__":
    build_dashboard().launch(
        server_name="127.0.0.1",
        server_port=7860,
        theme=gr.themes.Soft(
            primary_hue="cyan",
            secondary_hue="blue",
            neutral_hue="slate",
        ),
        css=CSS,
    )
