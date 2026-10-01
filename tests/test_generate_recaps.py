import importlib.util
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recaps = load_module("generate_recaps", "scripts/generate_recaps.py")


def daily_row(day, *, warning=False, unique_zappers=25):
    return {
        "date": day.isoformat(),
        "daily_unique_zappers": unique_zappers,
        "tracked_paid_actions": 200,
        "content_items_created": 50,
        "actions_by_type": {"ZAP": 100, "BOOST": 10, "DOWN_ZAP": 5},
        "spending_sats_by_type": {
            "ZAP": 5000,
            "BOOST": 1000,
            "DOWN_ZAP": 500,
            "ITEM_CREATE": 100,
        },
        "quality_status": "warning" if warning else "ok",
        "quality_warnings": ["test warning"] if warning else [],
    }


def reward_row(day, *, anomaly=None):
    return {
        "reward_date": day.isoformat(),
        "reward_distributed_sats": 1000,
        "daily_unique_reward_recipients": 10,
        "quality_status": "ok",
        "quality_warnings": [],
        "anomaly_flags": [anomaly] if anomaly else [],
    }


class RecapGeneratorTests(unittest.TestCase):
    def test_daily_recap_uses_reward_date_and_zaplike_totals(self):
        day = date(2026, 9, 29)
        daily_index = {day: daily_row(day)}
        reward_index = {day: reward_row(day)}

        recap = recaps.build_recap("daily", day, day, daily_index, reward_index)

        self.assertEqual(recap["metrics"]["zaplike_actions"], 115)
        self.assertEqual(recap["metrics"]["zaplike_sats"], 6500)
        self.assertEqual(recap["metrics"]["reward_sats_distributed"], 1000)
        self.assertEqual(recap["metrics"]["tracked_spend_actions"], 200)
        self.assertIn("25 daily unique zappers", recap["post_text"])
        self.assertLessEqual(recap["post_character_count"], 280)

    def test_friday_recap_averages_daily_unique_zappers(self):
        friday = date(2026, 9, 25)
        start = friday - timedelta(days=6)
        days = [start + timedelta(days=offset) for offset in range(7)]
        daily_index = {
            day: daily_row(day, unique_zappers=20 + offset)
            for offset, day in enumerate(days)
        }
        reward_index = {day: reward_row(day) for day in days}

        recap = recaps.build_recap(
            "friday", start, friday, daily_index, reward_index
        )

        self.assertEqual(recap["metrics"]["average_daily_unique_zappers"], 23.0)
        self.assertIn("23 avg. daily unique zappers", recap["post_text"])
        self.assertNotIn("weekly unique", recap["post_text"].lower())
        self.assertLessEqual(recap["post_character_count"], 280)

    def test_latest_friday_waits_until_friday_is_complete(self):
        self.assertEqual(
            recaps.latest_friday(date(2026, 9, 29)), date(2026, 9, 25)
        )
        self.assertEqual(
            recaps.latest_friday(date(2026, 9, 25)), date(2026, 9, 25)
        )

    def test_missing_day_skips_without_building_recap(self):
        friday = date(2026, 9, 25)
        start = friday - timedelta(days=6)
        days = [start + timedelta(days=offset) for offset in range(7)]
        daily_index = {day: daily_row(day) for day in days}
        reward_index = {day: reward_row(day) for day in days if day != friday}

        with self.assertRaises(recaps.RecapSkip):
            recaps.build_recap("friday", start, friday, daily_index, reward_index)

    def test_quality_warning_skips_recap(self):
        day = date(2026, 9, 29)
        daily_index = {day: daily_row(day, warning=True)}
        reward_index = {day: reward_row(day)}

        with self.assertRaises(recaps.RecapSkip):
            recaps.build_recap("daily", day, day, daily_index, reward_index)

    def test_reward_anomaly_marks_review_but_does_not_hide_draft(self):
        day = date(2026, 9, 29)
        daily_index = {day: daily_row(day)}
        reward_index = {day: reward_row(day, anomaly="low_reward_pool")}

        recap = recaps.build_recap("daily", day, day, daily_index, reward_index)

        self.assertEqual(recap["quality_status"], "review")
        self.assertEqual(
            recap["metrics"]["reward_anomaly_flags"], ["low_reward_pool"]
        )

    def test_remove_latest_recap_preserves_archive(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            original_recaps_dir = recaps.RECAPS_DIR
            recaps.RECAPS_DIR = Path(temporary_dir)
            try:
                archive = recaps.RECAPS_DIR / "daily" / "2026-09-29.md"
                archive.parent.mkdir(parents=True)
                archive.write_text("archived", encoding="utf-8")
                for suffix in (".md", ".json"):
                    (recaps.RECAPS_DIR / f"latest_daily{suffix}").write_text(
                        "stale", encoding="utf-8"
                    )

                recaps.remove_latest_recap("latest_daily")

                self.assertFalse((recaps.RECAPS_DIR / "latest_daily.md").exists())
                self.assertFalse((recaps.RECAPS_DIR / "latest_daily.json").exists())
                self.assertEqual(archive.read_text(encoding="utf-8"), "archived")
            finally:
                recaps.RECAPS_DIR = original_recaps_dir

    def test_methodology_reply_fits_in_x_post(self):
        self.assertLessEqual(len(recaps.METHODOLOGY_TEXT), 280)


if __name__ == "__main__":
    unittest.main()
