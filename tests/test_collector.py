import importlib.util
import unittest
from datetime import date
from pathlib import Path


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect = load_module("collect", "scripts/collect.py")


def bucket(day, values):
    return {
        "time": f"{day}T05:00:00.000Z",
        "data": [{"name": key, "value": value} for key, value in values.items()],
    }


class CollectorTests(unittest.TestCase):
    def test_parse_response_builds_expected_metrics(self):
        payload = {
            "data": {
                "spending": [bucket("2026-08-31", {"ZAP": 1200, "ITEM_CREATE": 20})],
                "actions": [bucket("2026-08-31", {"ZAP": 30, "ITEM_CREATE": 4, "BOOST": 2})],
                "spenders": [bucket("2026-08-31", {"ZAP": 9, "total": 12})],
            }
        }
        rows = collect.parse_response(payload, date(2026, 9, 2))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["zap_actions"], 30)
        self.assertEqual(rows[0]["zap_sats"], 1200)
        self.assertEqual(rows[0]["daily_unique_zappers"], 9)
        self.assertEqual(rows[0]["tracked_paid_actions"], 36)
        self.assertEqual(rows[0]["content_items_created"], 4)

    def test_rejects_graphql_errors(self):
        with self.assertRaises(collect.CollectorError):
            collect.parse_response({"errors": [{"message": "nope"}]}, date(2026, 9, 2))

    def test_merge_replaces_same_date(self):
        old = [{"date": "2026-08-31", "zap_actions": 1, "quality_warnings": [], "quality_status": "ok"}]
        new = [{"date": "2026-08-31", "zap_actions": 2, "quality_warnings": [], "quality_status": "ok"}]
        merged = collect.merge_rows(old, new)
        self.assertEqual(merged[0]["zap_actions"], 2)


if __name__ == "__main__":
    unittest.main()

