"""Unified end-to-end pipeline runner for the ENTWINE Block A Digital Twin.

Executes the full ENTWINE pipeline for Powerhouse 1 Block A:
  1. Seed the Asset Registry (PH-A-BLK building + PH-A-MAIN meter)
  2. Ingest historical A Block XLS telemetry into state_telemetry
  3. Run CAFA contextual anomaly detection
  4. Run GrCF counterfactual explanation
  5. Run GridReason natural language translation
  6. Optionally launch the FastAPI backend and Gradio dashboard
  7. Optionally verify the full end-to-end system

All steps are idempotent — running the pipeline multiple times without
--reset is completely safe and produces no duplicate records.

Usage
-----
    # Full fresh run (wipes Block A state_telemetry + anomaly_events first)
    python run_block_a_pipeline.py --reset

    # Idempotent re-run (skips already-ingested data automatically)
    python run_block_a_pipeline.py

    # Run ingestion + models only, then verify (no services launched)
    python run_block_a_pipeline.py --verify

    # Launch FastAPI + Gradio after pipeline completes
    python run_block_a_pipeline.py --start-services

    # Skip model retraining (use existing anomaly_events in DB)
    python run_block_a_pipeline.py --skip-models --start-services
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# ── Environment ───────────────────────────────────────────────────────────────

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent
_ENV_PATH: Final[Path] = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER: Final[logging.Logger] = logging.getLogger("pipeline")

PYTHON: Final[str] = sys.executable


# ── DB helper ─────────────────────────────────────────────────────────────────

def _engine() -> Engine:
    db_user     = os.getenv("DB_USER",     "entwine_admin")
    db_password = os.getenv("DB_PASSWORD", "change_me_now")
    db_host     = os.getenv("DB_HOST",     "localhost")
    db_port     = os.getenv("DB_PORT",     "5432")
    db_name     = os.getenv("DB_NAME",     "entwine_twin")
    return create_engine(
        f"postgresql+psycopg2://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}",
        pool_pre_ping=True,
    )


# ── Step helpers ──────────────────────────────────────────────────────────────

def _run(cmd: list[str], label: str) -> None:
    """Run a subprocess command; exit the pipeline on failure."""
    LOGGER.info("─── %s ───", label)
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        LOGGER.error("FAILED: %s (exit code %d)", label, result.returncode)
        sys.exit(result.returncode)
    LOGGER.info("OK: %s", label)


def step_seed(reset: bool) -> None:
    cmd = [PYTHON, "registry/seed_a_block.py"]
    if reset:
        cmd.append("--reset")
    _run(cmd, "Step 1 — Asset Registry: Seed Block A (PH-A-BLK / PH-A-MAIN)")


def step_ingest(reset: bool) -> None:
    cmd = [PYTHON, "-m", "ingestion.ingest_a_block"]
    if reset:
        cmd.append("--reset")
    _run(cmd, "Step 2 — Ingestion: Load A Block XLS into state_telemetry")


def step_run_cafa_grcf() -> None:
    _run(
        [PYTHON, "-m", "models.orchestrator"],
        "Step 3 — Intelligence: CAFA detection + GrCF counterfactual",
    )


def step_run_gridreason() -> None:
    _run(
        [PYTHON, "-m", "models.gridreason_llm"],
        "Step 4 — GridReason: Natural language anomaly translation",
    )


def step_verify(engine: Engine) -> bool:
    """Run end-to-end database assertions. Returns True if all pass."""
    LOGGER.info("─── Step 5 — Verification ───")
    checks: list[tuple[str, str, object]] = [
        (
            "PH-A-BLK building registered",
            "SELECT COUNT(*) FROM buildings WHERE building_code = 'PH-A-BLK'",
            1,
        ),
        (
            "PH-A-MAIN meter registered",
            "SELECT COUNT(*) FROM meters WHERE meter_code = 'PH-A-MAIN'",
            1,
        ),
        (
            "state_telemetry has Block A data",
            """
            SELECT COUNT(*) FROM state_telemetry st
            JOIN meters m ON st.meter_id = m.meter_id
            WHERE m.meter_code = 'PH-A-MAIN'
            """,
            None,  # any positive number
        ),
        (
            "anomaly_events has at least 1 event for PH-A-MAIN",
            """
            SELECT COUNT(*) FROM anomaly_events ae
            JOIN meters m ON ae.meter_id = m.meter_id
            WHERE m.meter_code = 'PH-A-MAIN'
            """,
            None,  # any positive number
        ),
        (
            "GridReason explanation written",
            """
            SELECT COUNT(*) FROM anomaly_events ae
            JOIN meters m ON ae.meter_id = m.meter_id
            WHERE m.meter_code = 'PH-A-MAIN'
              AND ae.natural_language_explanation IS NOT NULL
            """,
            None,  # any positive number
        ),
    ]

    all_pass = True
    with engine.connect() as conn:
        for label, sql, expected in checks:
            count = conn.execute(text(sql)).scalar()
            if expected is None:
                passed = count > 0  # type: ignore[operator]
            else:
                passed = count == expected

            status = "PASS" if passed else "FAIL"
            LOGGER.info("[%s] %s — result: %s", status, label, count)
            if not passed:
                all_pass = False

    if all_pass:
        LOGGER.info("Verification: ALL CHECKS PASSED ✓")
    else:
        LOGGER.error("Verification: SOME CHECKS FAILED ✗")
    return all_pass


def step_start_services() -> None:
    """Launch FastAPI and Gradio dashboard as background processes."""
    LOGGER.info("─── Step 6 — Starting Services ───")
    env = {**os.environ, "PYTHONUTF8": "1"}

    LOGGER.info("Launching FastAPI backend on http://127.0.0.1:8000 ...")
    api_proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    # Give FastAPI 3 seconds to start
    time.sleep(3)

    LOGGER.info("Launching Gradio dashboard on http://127.0.0.1:7860 ...")
    dashboard_proc = subprocess.Popen(
        [PYTHON, "dashboard/app.py"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    LOGGER.info(
        "\n"
        "═══════════════════════════════════════════════════════════\n"
        "  ENTWINE Block A Digital Twin — Services Running\n"
        "═══════════════════════════════════════════════════════════\n"
        "  FastAPI REST API  :  http://127.0.0.1:8000/docs\n"
        "  Gradio Dashboard  :  http://127.0.0.1:7860\n"
        "  API Health        :  http://127.0.0.1:8000/health\n"
        "  Anomalies         :  http://127.0.0.1:8000/api/v1/anomalies/PH-A-MAIN\n"
        "  Telemetry         :  http://127.0.0.1:8000/api/v1/telemetry/PH-A-MAIN\n"
        "═══════════════════════════════════════════════════════════\n"
        "  Press Ctrl+C to stop both services.\n"
        "═══════════════════════════════════════════════════════════"
    )

    try:
        api_proc.wait()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down services...")
        api_proc.terminate()
        dashboard_proc.terminate()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="ENTWINE Block A End-to-End Digital Twin Pipeline"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Wipe Block A state_telemetry and anomaly_events rows before running. "
            "Use this for a completely fresh demonstration run."
        ),
    )
    parser.add_argument(
        "--skip-models",
        action="store_true",
        help="Skip CAFA/GrCF/GridReason steps (use existing anomaly_events in DB).",
    )
    parser.add_argument(
        "--start-services",
        action="store_true",
        help="Launch FastAPI backend and Gradio dashboard after pipeline completes.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run end-to-end database assertions after all pipeline steps.",
    )
    args = parser.parse_args()

    LOGGER.info(
        "\n"
        "╔═══════════════════════════════════════════════════════════╗\n"
        "║   ENTWINE Digital Twin — Block A Pipeline Starting        ║\n"
        "║   Powerhouse 1 · A Block · PH-A-MAIN Meter               ║\n"
        "╚═══════════════════════════════════════════════════════════╝"
    )

    # ── Reset anomaly_events if requested ─────────────────────────────────────
    if args.reset:
        LOGGER.info("--reset: Clearing anomaly_events for PH-A-MAIN...")
        engine = _engine()
        with engine.begin() as conn:
            # Only delete if the table already exists
            conn.execute(text("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'anomaly_events') THEN
                        DELETE FROM anomaly_events ae
                        USING meters m
                        WHERE ae.meter_id = m.meter_id
                          AND m.meter_code = 'PH-A-MAIN';
                    END IF;
                END $$;
            """))
        engine.dispose()
        LOGGER.info("anomaly_events cleared for PH-A-MAIN.")

    # ── Pipeline steps ─────────────────────────────────────────────────────────
    step_seed(reset=args.reset)
    step_ingest(reset=args.reset)

    if not args.skip_models:
        step_run_cafa_grcf()
        step_run_gridreason()
    else:
        LOGGER.info("--skip-models: Skipping CAFA/GrCF/GridReason steps.")

    # ── Verification ───────────────────────────────────────────────────────────
    if args.verify or not args.start_services:
        engine = _engine()
        ok = step_verify(engine)
        engine.dispose()
        if not ok:
            return 1

    # ── Services ───────────────────────────────────────────────────────────────
    if args.start_services:
        step_start_services()

    LOGGER.info(
        "\n"
        "╔═══════════════════════════════════════════════════════════╗\n"
        "║   ENTWINE Block A Pipeline — COMPLETE ✓                  ║\n"
        "╚═══════════════════════════════════════════════════════════╝"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
