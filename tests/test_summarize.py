import importlib.util
import unittest
from datetime import date, timedelta
from pathlib import Path


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


summarize = load_module("summarize", "scripts/summarize.py")


class SummarizerTests(unittest.TestCase):
    def test_complete_week_metrics(self):
        monday = date(2026, 8, 24)
        rows = []
        for offset in range(7):
            rows.append({
                "date": (monday + timedelta(days=offset)).isoformat(),
                "zap_actions": 100,
                "zap_sats": 5000,
                "daily_unique_zappers": 25,
                "daily_unique_spenders": 40,
                "tracked_paid_actions": 200,
                "content_items_created": 50,
                "quality_status": "ok",
            })
        weeks = summarize.build_weekly(rows)
        self.assertEqual(len(weeks), 1)
        self.assertEqual(weeks[0]["zap_actions"], 700)
        self.assertEqual(weeks[0]["seconds_per_zap"], 864.0)
        self.assertEqual(weeks[0]["average_zap_sats"], 50.0)
        self.assertEqual(weeks[0]["zaps_per_100_content_items"], 200.0)

    def test_incomplete_week_is_omitted(self):
        rows = [{
            "date": "2026-08-24",
            "zap_actions": 1,
            "zap_sats": 1,
            "daily_unique_zappers": 1,
            "daily_unique_spenders": 1,
            "tracked_paid_actions": 1,
            "content_items_created": 1,
            "quality_status": "ok",
        }]
        self.assertEqual(summarize.build_weekly(rows), [])


if __name__ == "__main__":
    unittest.main()

