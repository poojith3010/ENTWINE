"""Execute the ENTWINE CAFA detection and GrCF explanation workflow.

The orchestrator retrieves PH-A-MAIN state telemetry, runs the headless CAFA
 detector, generates a GrCF counterfactual for the first chronological anomaly,
and persists the event in TimescaleDB. It contains no plotting or notebook-only
logic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from models.cafa_detector import run_cafa_detection
from models.grcf_explainer import explain_anomaly
from models.twin_data_connector import get_building_state_df, get_database_engine

LOGGER: Final[logging.Logger] = logging.getLogger("models.orchestrator")
METER_CODE: Final[str] = "PH-A-MAIN"
DETECTOR_TYPE: Final[str] = "CAFA-GrCF"
ANOMALY_EVENTS_TABLE: Final[str] = "anomaly_events"


@dataclass(frozen=True)
class OrchestrationResult:
    """Summary of one intelligence-layer execution."""

    records_processed: int
    anomalies_detected: int
    anomaly_time: pd.Timestamp | None
    event_saved: bool



def configure_logging() -> None:
    """Configure concise console logging for the orchestration process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )



def get_meter_id(connection: Connection, meter_code: str) -> int:
    """Return the database identifier for a registered meter code."""
    result = connection.execute(
        text("SELECT meter_id FROM meters WHERE meter_code = :meter_code"),
        {"meter_code": meter_code},
    )
    meter_id = result.scalar_one_or_none()
    if meter_id is None:
        raise ValueError(f"Meter is not registered: {meter_code}")
    return int(meter_id)



def ensure_anomaly_events_table(connection: Connection) -> None:
    """Create the anomaly event table and foreign-key relationship if needed."""
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS anomaly_events (
                time TIMESTAMPTZ NOT NULL,
                meter_id BIGINT NOT NULL REFERENCES meters(meter_id),
                detector_type VARCHAR(64) NOT NULL,
                counterfactual_data JSONB NOT NULL,
                natural_language_explanation TEXT
            )
            """
        )
    )



def save_anomaly_event(
    engine: Engine,
    anomaly_time: pd.Timestamp,
    meter_code: str,
    counterfactual: np.ndarray,
) -> None:
    """Persist one GrCF event in a single safe database transaction."""
    counterfactual_json = json.dumps(counterfactual.tolist(), allow_nan=False)
    event_time: datetime = anomaly_time.to_pydatetime()
    with engine.begin() as connection:
        ensure_anomaly_events_table(connection)
        meter_id = get_meter_id(connection, meter_code)
        connection.execute(
            text(
                """
                INSERT INTO anomaly_events (
                    time,
                    meter_id,
                    detector_type,
                    counterfactual_data
                )
                VALUES (
                    :event_time,
                    :meter_id,
                    :detector_type,
                    CAST(:counterfactual_data AS JSONB)
                )
                """
            ),
            {
                "event_time": event_time,
                "meter_id": meter_id,
                "detector_type": DETECTOR_TYPE,
                "counterfactual_data": counterfactual_json,
            },
        )



def run_intelligence(
    meter_code: str = METER_CODE,
    engine: Engine | None = None,
) -> OrchestrationResult:
    """Run CAFA, explain the first anomaly, and save its counterfactual.

    Args:
        meter_code: Registered meter to process; defaults to ``PH-A-MAIN``.
        engine: Optional SQLAlchemy engine, primarily for integration tests.

    Returns:
        A typed summary containing detection and persistence results.

    Raises:
        ConnectionError: If state telemetry cannot be retrieved.
        SQLAlchemyError: If database persistence fails.
        ValueError: If the state data, registry, or anomaly is invalid.
    """
    owned_engine = engine is None
    active_engine = engine or get_database_engine()
    try:
        state_df = get_building_state_df(meter_code, engine=active_engine)
        detection_df, detection_summary = run_cafa_detection(state_df)
        flags = detection_df["cafa_if_flag"].to_numpy(dtype=int)
        anomaly_positions = np.flatnonzero(flags == 1)
        LOGGER.info("Anomalies Detected: %d", len(anomaly_positions))

        if len(anomaly_positions) == 0:
            return OrchestrationResult(
                records_processed=len(detection_df),
                anomalies_detected=0,
                anomaly_time=None,
                event_saved=False,
            )

        anomaly_idx = int(anomaly_positions[0])
        normal_mask = flags == 0
        counterfactual = explain_anomaly(
            detection_df,
            normal_mask,
            anomaly_idx,
        )
        LOGGER.info(
            "GrCF Generation Complete: shape=%s",
            tuple(counterfactual.shape),
        )

        anomaly_time = pd.Timestamp(detection_df.index[anomaly_idx]).tz_convert("UTC")
        save_anomaly_event(
            active_engine,
            anomaly_time,
            meter_code,
            counterfactual,
        )
        LOGGER.info("Event Saved to Database: %s", anomaly_time.isoformat())
        return OrchestrationResult(
            records_processed=detection_summary.records_processed,
            anomalies_detected=len(anomaly_positions),
            anomaly_time=anomaly_time,
            event_saved=True,
        )
    finally:
        if owned_engine:
            active_engine.dispose()



def main() -> int:
    """Run the default PH-A-MAIN intelligence workflow."""
    configure_logging()
    try:
        run_intelligence(METER_CODE)
    except (ConnectionError, SQLAlchemyError, ValueError, TypeError) as exc:
        LOGGER.exception("Intelligence workflow failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
