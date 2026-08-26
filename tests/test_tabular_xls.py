"""
tests/test_tabular_xls.py
Tests for reading actual interval telemetry XLS files.
"""
from pathlib import Path
import unittest
from ingestion.config import DATA_DIR
from ingestion.readers.tabular_xls import read_tabular_xls


class TestTabularXls(unittest.TestCase):
    def test_read_tabular_xls_a_block(self):
        file_path = DATA_DIR / "POWERHOUSE_1" / "POWERHOUSE_1.A_BLOCK" / "New Tabular Report for report(2).xls"
        self.assertTrue(file_path.exists())

        records = list(read_tabular_xls(
            path=file_path,
            source_name="POWERHOUSE_1.A_BLOCK",
            source_file_id=1,
            measurement_source_id=10,
            entwine_asset_code="PH-01-A",
            mapping_confidence="strong_inference",
        ))

        self.assertGreater(len(records), 33000)
        first = records[0]
        self.assertEqual(first["source_name"], "POWERHOUSE_1.A_BLOCK")
        self.assertEqual(first["entwine_asset_code"], "PH-01-A")
        self.assertEqual(first["measurement_source_id"], 10)
        self.assertIsNotNone(first["ts"])
        self.assertIn("real_power_kw", first)
        self.assertIn("current_avg_a", first)
        self.assertIn("frequency_hz", first)

    def test_read_tabular_xls_incomer_empty(self):
        file_path = DATA_DIR / "POWERHOUSE_1" / "POWERHOUSE_1.POWERHOUSE_1_INCOMER" / "New Tabular Report for report(2).xls"
        self.assertTrue(file_path.exists())

        records = list(read_tabular_xls(
            path=file_path,
            source_name="POWERHOUSE_1.POWERHOUSE_1_INCOMER",
            source_file_id=2,
            measurement_source_id=11,
        ))

        self.assertEqual(len(records), 0)


if __name__ == "__main__":
    unittest.main()
