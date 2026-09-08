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


collect_rewards = load_module("collect_rewards", "scripts/collect_rewards.py")


def bucket(day, values):
    return {
        "time": f"{day}T05:00:00.000Z",
        "data": [{"name": key, "value": value} for key, value in values.items()],
    }


class RewardCollectorTests(unittest.TestCase):
    def test_builds_reward_row_and_allows_one_sat_crosscheck_difference(self):
        growth_payload = {
            "data": {
                "payouts": [bucket("2026-09-07", {"REWARD": 922640})],
                "recipients": [bucket("2026-09-07", {"REWARD": 69})],
            }
        }
        growth = collect_rewards.parse_growth_response(growth_payload)
        detail_payload = {
            "data": {
                "reward_0": [{
                    "time": "2026-09-07T05:00:00.000Z",
                    "total": 922641,
                    "sources": [
                        {"name": "DOWN_ZAP", "value": 633418000},
                        {"name": "BOOST", "value": 289223000},
                    ],
                }]
            }
        }
        details = collect_rewards.parse_detail_response(
            detail_payload, {"reward_0": "2026-09-07"}
        )
        rows = collect_rewards.build_rows([date(2026, 9, 7)], growth, details)
        self.assertEqual(rows[0]["source_activity_date"], "2026-09-06")
        self.assertEqual(rows[0]["reward_pool_sats"], 922641)
        self.assertEqual(rows[0]["reward_distributed_sats"], 922640)
        self.assertEqual(rows[0]["reward_distribution_gap_sats"], 1)
        self.assertEqual(rows[0]["daily_unique_reward_recipients"], 69)
        self.assertEqual(rows[0]["reward_source_total_msats"], 922641000)
        self.assertEqual(rows[0]["quality_status"], "ok")

    def test_flags_material_total_mismatch(self):
        growth = {
            "2026-09-07": {
                "reward_distributed_sats": 1100,
                "daily_unique_reward_recipients": 10,
            }
        }
        details = {
            "2026-09-07": {
                "reward_pool_sats": 1000,
                "reward_time": "2026-09-07T05:00:00.000Z",
                "reward_sources_msats": {"DOWN_ZAP": 500000},
            }
        }
        row = collect_rewards.build_rows([date(2026, 9, 7)], growth, details)[0]
        self.assertEqual(row["quality_status"], "warning")
        self.assertIn("reward_total_disagrees_with_sources", row["quality_warnings"])
        self.assertIn("distributed_rewards_exceed_reward_pool", row["quality_warnings"])

    def test_merge_replaces_same_reward_date(self):
        old = [{"reward_date": "2026-09-07", "reward_pool_sats": 1}]
        new = [{"reward_date": "2026-09-07", "reward_pool_sats": 2}]
        merged = collect_rewards.merge_rows(old, new)
        self.assertEqual(merged[0]["reward_pool_sats"], 2)


if __name__ == "__main__":
    unittest.main()
