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


summarize_rewards = load_module("summarize_rewards", "scripts/summarize_rewards.py")


class RewardSummarizerTests(unittest.TestCase):
    def test_complete_reward_week_metrics(self):
        monday = date(2026, 8, 24)
        rows = []
        for offset in range(7):
            reward_day = monday + timedelta(days=offset)
            rows.append({
                "reward_date": reward_day.isoformat(),
                "source_activity_date": (reward_day - timedelta(days=1)).isoformat(),
                "reward_pool_sats": 1000,
                "reward_distributed_sats": 980,
                "daily_unique_reward_recipients": 10,
                "reward_sources_msats": {
                    "BOOST": 200000,
                    "DOWN_ZAP": 300000,
                    "ZAP": 500000,
                },
                "quality_status": "ok",
                "anomaly_flags": [],
            })
        weeks = summarize_rewards.build_weekly(rows)
        self.assertEqual(len(weeks), 1)
        week = weeks[0]
        self.assertEqual(week["reward_pool_sats"], 7000)
        self.assertEqual(week["reward_distributed_sats"], 6860)
        self.assertEqual(week["reward_distribution_gap_sats"], 140)
        self.assertEqual(week["reward_recipient_days"], 70)
        self.assertEqual(week["reward_sats_per_recipient_day"], 98.0)
        self.assertEqual(week["boost_and_downzap_reward_share_pct"], 50.0)
        self.assertEqual(week["source_activity_period"]["start"], "2026-08-23")
        self.assertEqual(week["source_activity_period"]["end"], "2026-08-29")

    def test_incomplete_reward_week_is_omitted(self):
        rows = [{
            "reward_date": "2026-08-24",
            "reward_pool_sats": 1,
            "reward_distributed_sats": 1,
            "daily_unique_reward_recipients": 1,
            "reward_sources_msats": {"ZAP": 1000},
            "quality_status": "ok",
            "anomaly_flags": [],
        }]
        self.assertEqual(summarize_rewards.build_weekly(rows), [])


if __name__ == "__main__":
    unittest.main()
