import importlib.util
import sys
import unittest
from pathlib import Path


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


summarize = load_module("summarize_geyser", "scripts/summarize_geyser.py")


def fact(
    payment_id,
    contribution_id,
    contribution_amount,
    paid_amount,
    project_id="1",
    payment_type="LIGHTNING",
    method="STRIKE",
):
    return {
        "payment_id": str(payment_id),
        "contribution_id": str(contribution_id),
        "project_id": str(project_id),
        "contribution_amount_sats": contribution_amount,
        "payment_amount_sats": paid_amount,
        "accounting_amount_paid_sats": paid_amount,
        "payment_type": payment_type,
        "method": method,
    }


class GeyserSummaryTests(unittest.TestCase):
    def test_counts_payments_and_contributions_separately(self):
        result = summarize.aggregate([
            fact("p1", "c1", 100, 105),
            fact("p2", "c1", 100, 10),
            fact("p3", "c2", 300, 315, project_id="2"),
        ])
        self.assertEqual(result["recorded_lightning_payment_count"], 3)
        self.assertEqual(result["recorded_lightning_contribution_count"], 2)
        self.assertEqual(result["recorded_lightning_contribution_sats"], 400)
        self.assertEqual(result["recorded_lightning_payment_sats"], 430)
        self.assertEqual(
            result["recorded_lightning_accounting_amount_paid_sats"], 430
        )
        self.assertEqual(result["median_recorded_contribution_sats"], 200)
        self.assertEqual(result["unique_projects_funded"], 2)

    def test_empty_period_is_a_valid_zero(self):
        result = summarize.aggregate([])
        self.assertEqual(result["recorded_lightning_contribution_count"], 0)
        self.assertEqual(result["recorded_lightning_payment_count"], 0)
        self.assertIsNone(result["median_recorded_contribution_sats"])

    def test_duplicate_payment_id_fails(self):
        with self.assertRaisesRegex(summarize.SummaryError, "Duplicate"):
            summarize.aggregate([
                fact("p1", "c1", 100, 100),
                fact("p1", "c2", 200, 200),
            ])

    def test_inconsistent_contribution_amount_fails(self):
        with self.assertRaisesRegex(summarize.SummaryError, "inconsistent"):
            summarize.aggregate([
                fact("p1", "c1", 100, 100),
                fact("p2", "c1", 200, 200),
            ])


if __name__ == "__main__":
    unittest.main()
