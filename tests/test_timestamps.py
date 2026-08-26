"""
tests/test_timestamps.py
Tests for ingestion/normalize.py timestamp conversions and metric coercion.
"""
from datetime import datetime, timezone
import unittest
from ingestion.normalize import (
    parse_xls_timestamp,
    parse_csv_timestamp,
    coerce_float,
    parse_metric_header,
    is_uuid_row,
)


class TestTimestamps(unittest.TestCase):
    def test_parse_xls_timestamp(self):
        raw = "1/1/2025 12:15:00 AM"
        dt = parse_xls_timestamp(raw)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.utcoffset().total_seconds(), 0)
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.month, 12)
        self.assertEqual(dt.day, 31)
        self.assertEqual(dt.hour, 18)
        self.assertEqual(dt.minute, 45)

    def test_parse_csv_timestamp(self):
        raw = "2026-01-30 17:45:00"
        dt = parse_csv_timestamp(raw)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.utcoffset().total_seconds(), 0)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.day, 30)
        self.assertEqual(dt.hour, 12)
        self.assertEqual(dt.minute, 15)

    def test_coerce_float(self):
        self.assertEqual(coerce_float("123.45"), 123.45)
        self.assertIsNone(coerce_float("-0.001"))
        self.assertIsNone(coerce_float("nan"))
        self.assertIsNone(coerce_float(""))
        self.assertIsNone(coerce_float("N/A"))

    def test_parse_metric_header(self):
        header = "POWERHOUSE_1.A_BLOCK\nCurrent Avg\n(A)"
        col = parse_metric_header(header, "POWERHOUSE_1.A_BLOCK")
        self.assertEqual(col, "current_avg_a")

        header_power = "POWERHOUSE_1.DG_1\nReal Power\n(kW)"
        col_power = parse_metric_header(header_power, "POWERHOUSE_1.DG_1")
        self.assertEqual(col_power, "real_power_kw")

    def test_is_uuid_row(self):
        self.assertTrue(is_uuid_row("ID: 051d6a59-3b7c-42db-9791-57b51280a984"))
        self.assertFalse(is_uuid_row("1/1/2025 12:15:00 AM"))
        self.assertFalse(is_uuid_row("Tabular"))


if __name__ == "__main__":
    unittest.main()
