"""
tests/test_dedup.py
Tests for deduplication logic across readers and database tables.
"""
from datetime import datetime, timezone
import unittest
from ingestion.readers.alarm_csv import _make_fingerprint


class TestDedup(unittest.TestCase):
    def test_event_fingerprint_deterministic_and_unique(self):
        fp1 = _make_fingerprint("1", "alarm_history", "101", "2026-01-25 10:00:00", "128", "POWERHOUSE_1.MAIN_VCB", "Under Voltage")
        fp2 = _make_fingerprint("1", "alarm_history", "101", "2026-01-25 10:00:00", "128", "POWERHOUSE_1.MAIN_VCB", "Under Voltage")
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)

        fp3 = _make_fingerprint("1", "alarm_history", "102", "2026-01-25 10:00:00", "128", "POWERHOUSE_1.MAIN_VCB", "Under Voltage")
        self.assertNotEqual(fp1, fp3)


if __name__ == "__main__":
    unittest.main()
