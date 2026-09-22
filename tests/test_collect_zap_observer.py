import importlib.util
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collect = load_module("collect_zap_observer", "scripts/collect_zap_observer.py")


def response(show=None, start=date(2026, 8, 20), days=30, sparse=False):
    hourly = []
    for day_offset in range(days):
        day = start.fromordinal(start.toordinal() + day_offset)
        midnight = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        for hour_offset in range(24):
            if sparse and hour_offset not in {0, 12}:
                continue
            hourly.append({
                "hour": midnight + hour_offset * 3600,
                "count": 1,
                "sats": 21,
            })
    payload = {
        "range": "30d",
        "since": hourly[0]["hour"],
        "count": sum(item["count"] for item in hourly),
        "sats": sum(item["sats"] for item in hourly),
        "hourly": hourly,
    }
    if show is not None:
        payload["show"] = show
    return payload


class ZapObserverCollectorTests(unittest.TestCase):
    def test_validation_accepts_sparse_category_series(self):
        payload = response("tracks", sparse=True)
        collect.validate_stats(payload, "tracks")

    def test_validation_rejects_total_mismatch(self):
        payload = response()
        payload["count"] += 1
        with self.assertRaisesRegex(collect.CollectionError, "hourly series"):
            collect.validate_stats(payload, None)

    def test_validation_rejects_duplicate_hour(self):
        payload = response()
        payload["hourly"].append(dict(payload["hourly"][0]))
        payload["count"] += 1
        payload["sats"] += 21
        with self.assertRaisesRegex(collect.CollectionError, "duplicate"):
            collect.validate_stats(payload, None)

    def test_build_daily_rows_uses_only_complete_past_days(self):
        responses = {"all": response()}
        for name in collect.SHOW_FILTERS:
            responses[name] = response(name, sparse=True)
        rows, warnings = collect.build_daily_rows(responses, date(2026, 9, 19))
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["date_utc"], "2026-08-20")
        self.assertEqual(rows[-1]["date_utc"], "2026-09-18")
        self.assertEqual(rows[0]["zap_receipt_count"], 24)
        self.assertEqual(rows[0]["track_zap_receipt_count"], 2)
        self.assertEqual(rows[0]["total_zap_sats"], 504)
        self.assertEqual(warnings, [])

    def test_partial_oldest_day_is_excluded_and_reported(self):
        responses = {"all": response()}
        responses["all"]["hourly"] = responses["all"]["hourly"][12:]
        responses["all"]["count"] -= 12
        responses["all"]["sats"] -= 12 * 21
        for name in collect.SHOW_FILTERS:
            responses[name] = response(name, sparse=True)
        rows, warnings = collect.build_daily_rows(responses, date(2026, 9, 19))
        self.assertEqual(len(rows), 29)
        self.assertEqual(rows[0]["date_utc"], "2026-08-21")
        self.assertIn("2026-08-20", warnings[0])

    def test_merge_preserves_history_and_replaces_refreshed_day(self):
        existing = {
            "observations": [
                {"date_utc": "2026-08-01", "zap_receipt_count": 1},
                {"date_utc": "2026-08-20", "zap_receipt_count": 2},
            ]
        }
        new = [{"date_utc": "2026-08-20", "zap_receipt_count": 3}]
        merged = collect.merge_daily(existing, new, "2026-09-20T00:00:00Z")
        self.assertEqual(merged["observation_count"], 2)
        self.assertEqual(merged["observations"][1]["zap_receipt_count"], 3)

    def test_rolling_summary_reports_missing_days(self):
        rows, _ = collect.build_daily_rows(
            {"all": response(), **{
                name: response(name, sparse=True) for name in collect.SHOW_FILTERS
            }},
            date(2026, 9, 19),
        )
        daily = collect.merge_daily(None, rows[1:], "2026-09-19T01:00:00Z")
        rolling = collect.build_rolling(daily, "2026-09-19T01:00:00Z")
        self.assertEqual(rolling["period"]["days_present"], 29)
        self.assertEqual(rolling["period"]["missing_days"], ["2026-08-20"])
        self.assertEqual(rolling["collection_status"]["quality_status"], "warning")


if __name__ == "__main__":
    unittest.main()
