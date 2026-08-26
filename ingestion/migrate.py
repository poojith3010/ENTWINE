"""
ingestion/migrate.py
Versioned migration runner for Module 2 state-layer tables.

Behaviour
---------
* Creates schema_migrations table if it does not exist.
* Reads .sql files from migrations/ in lexicographic order.
* For each migration file:
    - filename already recorded AND checksum matches  → SKIP (already applied).
    - filename already recorded AND checksum DIFFERS  → FAIL HARD (migration was
      edited after being applied; this is a data-integrity error, not a warning).
    - filename not yet recorded                       → apply in a transaction,
      then record filename + checksum + applied_at.
* Returns the number of migrations applied (0 = already up to date).

Usage
-----
    python -m ingestion.run --migrate
    # or directly:
    python -m ingestion.migrate
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import psycopg2

from ingestion.config import MIGRATIONS_DIR, get_dsn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# bootstrap: ensure schema_migrations exists before anything else
# ---------------------------------------------------------------------------
_BOOTSTRAP_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          SERIAL       PRIMARY KEY,
    filename    VARCHAR(200) UNIQUE NOT NULL,
    checksum    VARCHAR(64)  NOT NULL,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
"""


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's content."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _applied_migrations(cur) -> dict[str, str]:
    """Return {filename: checksum} for all already-applied migrations."""
    cur.execute(
        "SELECT filename, checksum FROM schema_migrations ORDER BY filename"
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def run_migrations(dsn: str | None = None, dry_run: bool = False) -> int:
    """Apply all pending migrations.

    Parameters
    ----------
    dsn:
        psycopg2 DSN string.  Defaults to ``get_dsn()``.
    dry_run:
        If True, discover and validate but do not apply anything.

    Returns
    -------
    int
        Number of migrations applied (0 if already up-to-date).

    Raises
    ------
    SystemExit
        If any already-applied migration file has a changed checksum
        (tampered migration detected).
    """
    dsn = dsn or get_dsn()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    applied = 0

    try:
        with conn.cursor() as cur:
            # Ensure the bookkeeping table exists.
            cur.execute(_BOOTSTRAP_DDL)

            already_applied = _applied_migrations(cur)

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not migration_files:
            log.warning("No migration files found in %s", MIGRATIONS_DIR)
            return 0

        for mf in migration_files:
            fname    = mf.name
            checksum = _sha256_file(mf)

            if fname in already_applied:
                recorded_checksum = already_applied[fname]
                if recorded_checksum == checksum:
                    log.info("SKIP  %s (already applied, checksum OK)", fname)
                    continue
                else:
                    # --- HARD FAIL: migration was altered after application ---
                    log.error(
                        "FATAL: migration '%s' has been modified since it was "
                        "applied.\n"
                        "  Recorded checksum : %s\n"
                        "  Current checksum  : %s\n"
                        "Never edit a migration after it has been applied.  "
                        "Create a new migration file instead.",
                        fname, recorded_checksum, checksum,
                    )
                    sys.exit(1)

            # Not yet applied — apply it.
            sql = mf.read_text(encoding="utf-8")
            log.info("APPLY %s ...", fname)

            if dry_run:
                log.info("  [dry-run] would apply %s (%d bytes)", fname, len(sql))
                applied += 1
                continue

            # Use a transaction for the migration body + bookkeeping insert.
            # Note: some DDL (e.g. CREATE TABLE) is auto-committed in PG even
            # inside a transaction block, so we set autocommit=False only for
            # the bookkeeping INSERT.
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (filename, checksum) "
                        "VALUES (%s, %s)",
                        (fname, checksum),
                    )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                log.error("FAILED to apply %s: %s", fname, exc)
                raise
            finally:
                conn.autocommit = True

            log.info("  Applied %s (checksum %s)", fname, checksum[:12])
            applied += 1

    finally:
        conn.close()

    if applied == 0:
        log.info("All migrations already applied — schema is up to date.")
    else:
        log.info("Applied %d migration(s).", applied)

    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    run_migrations()
