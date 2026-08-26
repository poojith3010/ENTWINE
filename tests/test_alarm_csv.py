"""
tests/test_alarm_csv.py
Tests for reading alarms, status, events, and incidents CSV files.
"""
from pathlib import Path
import unittest
from ingestion.config import DATA_DIR
from ingestion.readers.alarm_csv import read_alarm_csv


class TestAlarmCsv(unittest.TestCase):
    def test_read_alarm_history(self):
        files = list((DATA_DIR / "Alarms").glob("Alarm_History_*.csv"))
        self.assertGreater(len(files), 0)
        test_file = files[0]

        source_map = {
            "POWERHOUSE_1.MAIN_VCB": {
                "measurement_source_id": 5,
                "entwine_asset_code": "PH-01-MAIN-VCB",
                "confidence": "strong_inference",
            }
        }

        records = list(read_alarm_csv(
            path=test_file,
            source_category="alarm_history",
            source_file_id=1,
            source_map=source_map,
        ))

        self.assertGreater(len(records), 0)
        first = records[0]
        self.assertEqual(first["event_class"], "alarm_history")
        self.assertIn("row_fingerprint", first)
        self.assertEqual(len(first["row_fingerprint"]), 64)
        self.assertIsNotNone(first["start_time_ist"])

    def test_read_event_history(self):
        files = list((DATA_DIR / "Alarms").glob("Event_History_*.csv"))
        self.assertGreater(len(files), 0)
        test_file = files[0]

        records = list(read_alarm_csv(
            path=test_file,
            source_category="event",
            source_file_id=2,
            source_map={},
        ))

        self.assertGreater(len(records), 0)
        first = records[0]
        self.assertEqual(first["event_class"], "event")
        self.assertIn("condition_text", first)
        self.assertIn("row_fingerprint", first)


if __name__ == "__main__":
    unittest.main()
