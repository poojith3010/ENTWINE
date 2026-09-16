"""Translate persisted GrCF counterfactuals into controlled operator alerts.

This module is the GridReason translation layer. It reads the most recent
uninterpreted event, computes deterministic feature deltas against a robust
expected-state baseline, and writes a short technical explanation back to
PostgreSQL. A local Transformers model can be enabled explicitly; otherwise a
conservative rule-based explanation is used and no physical condition is
invented.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from models.twin_data_connector import get_database_engine

LOGGER: Final[logging.Logger] = logging.getLogger("models.gridreason_llm")
DEFAULT_MODEL: Final[str] = os.getenv("GRIDREASON_MODEL", "distilgpt2")
MAX_ALERT_LENGTH: Final[int] = 480
FEATURE_NAMES: Final[tuple[str, ...]] = (
    "real_power_kw",
    "is_gap",
    "is_holiday",
    "is_weekend",
    "is_workhour",
    "apparent_power_kva",
    "current_avg_a",
    "frequency_hz",
    "power_factor_pct",
    "voltage_ll_avg_v",
)


class TextGenerator(Protocol):
    """Protocol implemented by a Transformers text-generation pipeline."""

    def __call__(self, prompt: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Generate text from a prompt."""


@dataclass(frozen=True)
class AnomalyEvent:
    """Database representation of an uninterpreted anomaly event."""

    event_time: pd.Timestamp
    meter_id: int
    detector_type: str
    counterfactual_data: np.ndarray


@dataclass(frozen=True)
class StateDelta:
    """Deterministic summary of expected-versus-counterfactual changes."""

    feature: str
    expected: float
    counterfactual: float
    delta: float
    relative_percent: float



def configure_logging() -> None:
    """Configure console logging for the GridReason translation process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )



def ensure_explanation_column(connection: Connection) -> None:
    """Add the write-back column to anomaly events without changing existing data."""
    connection.execute(
        text(
            """
            ALTER TABLE anomaly_events
            ADD COLUMN IF NOT EXISTS natural_language_explanation TEXT
            """
        )
    )



def fetch_latest_uninterpreted_event(connection: Connection) -> AnomalyEvent | None:
    """Fetch and decode the newest anomaly that has no explanation yet."""
    result = connection.execute(
        text(
            """
            SELECT time, meter_id, detector_type, counterfactual_data
            FROM anomaly_events
            WHERE natural_language_explanation IS NULL
            ORDER BY time DESC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
    )
    row = result.mappings().first()
    if row is None:
        return None

    payload: Any = row["counterfactual_data"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    counterfactual = np.asarray(payload, dtype=np.float64)
    if counterfactual.ndim != 2 or counterfactual.shape[0] == 0:
        raise ValueError("counterfactual_data must be a non-empty 2D tensor.")
    timestamp = pd.Timestamp(row["time"])
    timestamp = (
        timestamp.tz_localize("UTC")
        if timestamp.tzinfo is None
        else timestamp.tz_convert("UTC")
    )
    return AnomalyEvent(
        event_time=timestamp,
        meter_id=int(row["meter_id"]),
        detector_type=str(row["detector_type"]),
        counterfactual_data=counterfactual,
    )



def _feature_names(feature_count: int) -> tuple[str, ...]:
    """Return stable names for the stored tensor width."""
    if feature_count <= len(FEATURE_NAMES):
        return FEATURE_NAMES[:feature_count]
    return FEATURE_NAMES + tuple(
        f"feature_{index}" for index in range(len(FEATURE_NAMES), feature_count)
    )



def calculate_state_deltas(counterfactual: np.ndarray) -> list[StateDelta]:
    """Compare each feature with its robust expected-state median baseline.

    The anomaly event stores only the generated counterfactual. Therefore the
    expected state is defined deterministically as the median of each feature
    across the 16-step counterfactual window, avoiding fabricated operating
    conditions and making the comparison reproducible.
    """
    expected_state = np.nanmedian(counterfactual, axis=0)
    counterfactual_state = np.nanmean(counterfactual, axis=0)
    deltas: list[StateDelta] = []
    for name, expected, actual in zip(
        _feature_names(counterfactual.shape[1]), expected_state, counterfactual_state
    ):
        if not np.isfinite(expected) or not np.isfinite(actual):
            continue
        delta = float(actual - expected)
        relative = float(100.0 * delta / (abs(expected) + 1e-9))
        deltas.append(
            StateDelta(
                feature=name,
                expected=float(expected),
                counterfactual=float(actual),
                delta=delta,
                relative_percent=relative,
            )
        )
    return deltas



def build_prompt(event: AnomalyEvent, deltas: list[StateDelta]) -> str:
    """Build the strict, short GridReason triage prompt."""
    delta_lines = "\n".join(
        f"- {item.feature}: expected={item.expected:.4f}; "
        f"counterfactual={item.counterfactual:.4f}; "
        f"delta={item.delta:+.4f} ({item.relative_percent:+.2f}%)"
        for item in deltas
    )
    return (
        "SYSTEM: You are GridReason, a deterministic energy-grid triage formatter.\n"
        "Output exactly one technical alert of at most 45 words.\n"
        "Use only the measurements supplied below. Do not diagnose equipment, "
        "infer causes, recommend actions, or invent conditions.\n"
        "Use cautious language: 'observed deviation' and 'requires operator review'.\n"
        "Include meter, UTC time, detector, and the two largest absolute deltas.\n\n"
        f"METER_ID: {event.meter_id}\n"
        f"UTC_TIME: {event.event_time.isoformat()}\n"
        f"DETECTOR: {event.detector_type}\n"
        "STATE_DELTAS:\n"
        f"{delta_lines}\n\n"
        "ALERT:"
    )



def _largest_deltas(deltas: list[StateDelta], count: int = 2) -> list[StateDelta]:
    """Return the largest finite absolute relative changes."""
    return sorted(deltas, key=lambda item: abs(item.relative_percent), reverse=True)[:count]



def build_deterministic_alert(event: AnomalyEvent, deltas: list[StateDelta]) -> str:
    """Create a conservative technical alert without generative speculation."""
    largest = _largest_deltas(deltas)
    differences = "; ".join(
        f"{item.feature} {item.delta:+.4f} ({item.relative_percent:+.2f}%)"
        for item in largest
    )
    if not differences:
        differences = "no finite feature delta available"
    return (
        f"CAFA/GrCF alert | meter_id={event.meter_id} | "
        f"time={event.event_time.isoformat()} | detector={event.detector_type} | "
        f"observed deviation: {differences}; requires operator review."
    )[:MAX_ALERT_LENGTH]



def load_text_generator(model_name: str = DEFAULT_MODEL) -> TextGenerator:
    """Load a local Transformers generator with deterministic decoding settings.

    The import is lazy because model weights are optional and may require a
    network download. Callers should use the deterministic fallback if loading
    fails or if no local model has been approved for deployment.
    """
    from transformers import pipeline

    generator = pipeline(
        "text-generation",
        model=model_name,
        device=-1,
    )
    return cast(TextGenerator, generator)



def generate_explanation(
    event: AnomalyEvent,
    deltas: list[StateDelta],
    generator: TextGenerator | None = None,
) -> str:
    """Generate and sanitize one deterministic technical triage alert."""
    fallback = build_deterministic_alert(event, deltas)
    if generator is None:
        return fallback
    prompt = build_prompt(event, deltas)
    try:
        outputs = generator(
            prompt,
            max_new_tokens=70,
            do_sample=False,
            temperature=0.0,
            top_p=1.0,
            return_full_text=False,
        )
        generated = str(outputs[0].get("generated_text", "")).strip()
        generated = re.sub(r"\s+", " ", generated)
        if not generated or len(generated) > MAX_ALERT_LENGTH:
            return fallback
        return generated
    except (RuntimeError, TypeError, ValueError, IndexError, KeyError) as exc:
        LOGGER.warning("Transformer generation failed; using deterministic alert: %s", exc)
        return fallback



def process_latest_event(
    engine: Engine | None = None,
    generator: TextGenerator | None = None,
) -> str | None:
    """Translate and persist the newest uninterpreted anomaly event."""
    owned_engine = engine is None
    active_engine = engine or get_database_engine()
    try:
        with active_engine.begin() as connection:
            ensure_explanation_column(connection)
            event = fetch_latest_uninterpreted_event(connection)
            if event is None:
                LOGGER.info("No uninterpreted anomaly event found.")
                return None
            deltas = calculate_state_deltas(event.counterfactual_data)
            explanation = generate_explanation(event, deltas, generator)
            connection.execute(
                text(
                    """
                    UPDATE anomaly_events
                    SET natural_language_explanation = :explanation
                    WHERE time = :event_time
                      AND meter_id = :meter_id
                      AND natural_language_explanation IS NULL
                    """
                ),
                {
                    "explanation": explanation,
                    "event_time": event.event_time.to_pydatetime(),
                    "meter_id": event.meter_id,
                },
            )
            LOGGER.info("GridReason explanation saved for event at %s", event.event_time)
            return explanation
    except (SQLAlchemyError, ConnectionError, ValueError, TypeError) as exc:
        LOGGER.error("GridReason processing failed: %s", exc)
        raise
    finally:
        if owned_engine:
            active_engine.dispose()



def main() -> int:
    """Run GridReason using the deterministic fallback unless explicitly configured."""
    configure_logging()
    try:
        explanation = process_latest_event()
        if explanation is not None:
            LOGGER.info("GridReason alert: %s", explanation)
    except (SQLAlchemyError, ConnectionError, ValueError, TypeError) as exc:
        LOGGER.exception("GridReason execution failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
