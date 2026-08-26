"""
tests/test_mapping.py
Tests for ingestion/mapping.py loading and validation.
"""
import unittest
from pathlib import Path
from ingestion.mapping import all_rows, approved_rows, quarantined_rows, telemetry_source_map


class TestMapping(unittest.TestCase):
    def test_mapping_loads_all_rows(self):
        rows = all_rows()
        self.assertEqual(len(rows), 13)
        sources = [r["source_name"] for r in rows]
        self.assertIn("POWERHOUSE_1", sources)
        self.assertIn("POWERHOUSE_1.A_BLOCK", sources)
        self.assertIn("POWERHOUSE_1.FROM_POWERHOUSE_2", sources)

    def test_mapping_approved_rows(self):
        approved = approved_rows()
        for r in approved:
            code = r.get("entwine_asset_code", "").strip().upper()
            self.assertTrue(code != "TBD" and code != "")
        self.assertEqual(len(approved), 11)

    def test_mapping_quarantined_rows(self):
        quarantined = quarantined_rows()
        self.assertEqual(len(quarantined), 2)
        sources = [r["source_name"] for r in quarantined]
        self.assertIn("POWERHOUSE_1.FROM_POWERHOUSE_2", sources)
        self.assertIn("POWERHOUSE_1.TO_POWERHOUSE_2", sources)

    def test_telemetry_source_map(self):
        smap = telemetry_source_map()
        self.assertIn("POWERHOUSE_1.A_BLOCK", smap)
        self.assertEqual(smap["POWERHOUSE_1.A_BLOCK"]["entwine_asset_code"], "PH-01-A")
        self.assertEqual(smap["POWERHOUSE_1.A_BLOCK"]["asset_type"], "meter_candidate")


if __name__ == "__main__":
    unittest.main()
