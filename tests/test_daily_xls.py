"""
tests/test_daily_xls.py
Tests for reading DAILY ENERGY REPORT workbooks.
"""
from pathlib import Path
import unittest
from ingestion.config import DATA_DIR
from ingestion.readers.daily_xls import read_daily_xls


class TestDailyXls(unittest.TestCase):
    def test_read_daily_xls_source_detection_and_parsing(self):
        file_path = DATA_DIR / "daily energy report" / "DAILY ENERGY REPORT(264).xls"
        self.assertTrue(file_path.exists())

        source_map = {
            "POWERHOUSE_1.A_BLOCK": {
                "measurement_source_id": 1,
                "entwine_asset_code": "PH-01-A",
                "confidence": "strong_inference",
            }
        }

        detected_src, records_gen = read_daily_xls(
            path=file_path,
            source_file_id=1,
            source_map=source_map,
        )

        self.assertEqual(detected_src, "POWERHOUSE_1.A_BLOCK")
        records = list(records_gen)
        self.assertGreater(len(records), 0)
        first = records[0]
        self.assertEqual(first["source_name"], "POWERHOUSE_1.A_BLOCK")
        self.assertEqual(first["entwine_asset_code"], "PH-01-A")
        self.assertEqual(first["measurement_source_id"], 1)
        self.assertIsNotNone(first["report_date"])
        self.assertIn("real_energy_kwh", first)


if __name__ == "__main__":
    unittest.main()
