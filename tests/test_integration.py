"""
tests/test_integration.py
Integration tests for database migration, asset registration, and quality gates.
"""
import psycopg2
import unittest
from ingestion.config import get_dsn
from ingestion.migrate import run_migrations
from ingestion.quality import run_quality_gates
from ingestion.register_assets import register_assets


class TestIntegration(unittest.TestCase):
    def test_database_connection(self):
        dsn = get_dsn()
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()

    def test_migration_runner_idempotent(self):
        applied = run_migrations()
        self.assertGreaterEqual(applied, 0)

        applied_again = run_migrations()
        self.assertEqual(applied_again, 0)

    def test_register_assets_idempotent(self):
        stats = register_assets()
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["quarantined"], 2)

        stats_again = register_assets()
        self.assertEqual(stats_again["errors"], 0)
        self.assertEqual(stats_again["conflicts"], 0)


if __name__ == "__main__":
    unittest.main()
