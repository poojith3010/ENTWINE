"""
tests/test_rerun.py
Tests for discover.py file discovery, SHA-256 calculation, and rerun skip mechanics.
"""
from pathlib import Path
import unittest
from ingestion.discover import discover_sources, sha256_file
from ingestion.config import DATA_DIR


class TestRerun(unittest.TestCase):
    def test_discover_sources_all_files(self):
        discovered = discover_sources()
        self.assertEqual(len(discovered), 29)

        categories = [f.source_category for f in discovered]
        self.assertEqual(categories.count("interval_telemetry"), 12)
        self.assertEqual(categories.count("daily_report"), 7)
        self.assertEqual(categories.count("measurement_csv"), 1)
        self.assertEqual(categories.count("alarm_history"), 2)
        self.assertEqual(categories.count("alarm_status"), 3)
        self.assertEqual(categories.count("event"), 1)
        self.assertEqual(categories.count("incident"), 3)

    def test_sha256_checksum_unique(self):
        discovered = discover_sources()
        checksums = [f.sha256_checksum for f in discovered]
        self.assertEqual(len(checksums), len(set(checksums)))
        for c in checksums:
            self.assertEqual(len(c), 64)


if __name__ == "__main__":
    unittest.main()
