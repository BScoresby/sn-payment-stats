import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collect = load_module("collect_geyser", "scripts/collect_geyser.py")


def contribution(
    contribution_id="10",
    payment_id="20",
    payment_type="LIGHTNING",
    payment_status="PAID",
    currency="BTCSAT",
):
    return {
        "id": contribution_id,
        "amount": 1000,
        "status": "CONFIRMED",
        "createdAt": 1789000000000,
        "confirmedAt": 1789000010000,
        "projectId": "30",
        "payments": [
            {
                "id": payment_id,
                "status": payment_status,
                "paymentType": payment_type,
                "paymentAmount": 1050,
                "paymentCurrency": currency,
                "accountingAmountPaid": 1050,
                "paidAt": 1789000010000,
                "method": "STRIKE",
            }
        ],
    }


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.variables = []

    def post(self, variables):
        self.variables.append(variables)
        page = self.pages.pop(0)
        return {"data": {"contributionsGet": {"contributions": page}}}


class GeyserCollectorTests(unittest.TestCase):
    def test_extracts_paid_payer_lightning_payment(self):
        facts = collect.extract_lightning_facts([contribution()])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["payment_id"], "20")
        self.assertEqual(facts[0]["contribution_amount_sats"], 1000)
        self.assertEqual(facts[0]["accounting_amount_paid_sats"], 1050)

    def test_includes_payer_origin_swap_and_keysend(self):
        rows = [
            contribution("10", "20", "LIGHTNING_TO_RSK_SWAP"),
            contribution("11", "21", "LIGHTNING_PODCAST_KEYSEND"),
        ]
        facts = collect.extract_lightning_facts(rows)
        self.assertEqual({item["payment_type"] for item in facts}, {
            "LIGHTNING_TO_RSK_SWAP",
            "LIGHTNING_PODCAST_KEYSEND",
        })

    def test_excludes_unpaid_and_non_payer_lightning_types(self):
        rows = [
            contribution("10", "20", payment_status="UNPAID"),
            contribution("11", "21", "FIAT_TO_LIGHTNING_SWAP"),
            contribution("12", "22", "ON_CHAIN"),
        ]
        self.assertEqual(collect.extract_lightning_facts(rows), [])

    def test_rejects_non_sat_lightning_payment(self):
        with self.assertRaisesRegex(collect.CollectionError, "BTCSAT"):
            collect.extract_lightning_facts([
                contribution(currency="USDCENT")
            ])

    def test_graphql_errors_are_not_interpreted_as_zero(self):
        with self.assertRaisesRegex(collect.CollectionError, "GraphQL"):
            collect.parse_page({"errors": [{"message": "forbidden"}]})

    def test_pagination_uses_last_contribution_id_and_stops_on_short_page(self):
        first = [contribution(str(number), str(number + 100)) for number in (12, 11)]
        second = [contribution("10", "110")]
        client = FakeClient([first, second])
        rows, pages = collect.fetch_all_contributions(client, 1, 2, page_size=2)
        self.assertEqual(pages, 2)
        self.assertEqual([item["id"] for item in rows], ["12", "11", "10"])
        self.assertNotIn("cursor", client.variables[0]["input"]["pagination"])
        self.assertEqual(
            client.variables[1]["input"]["pagination"]["cursor"]["id"], "11"
        )

    def test_repeated_contribution_across_pages_fails(self):
        client = FakeClient([
            [contribution("10", "20")],
            [contribution("10", "20")],
        ])
        with self.assertRaisesRegex(collect.CollectionError, "repeated contribution"):
            collect.fetch_all_contributions(client, 1, 2, page_size=1)

    def test_completed_days_excludes_today(self):
        self.assertEqual(
            collect.completed_days(date(2026, 9, 14), 3),
            [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)],
        )

    def test_small_explicit_first_run_uses_daily_budget(self):
        self.assertFalse(collect.is_backfill_run(False, 1, 1, 7))
        self.assertTrue(collect.is_backfill_run(False, None, 90, 7))
        self.assertTrue(collect.is_backfill_run(True, 30, 30, 7))


if __name__ == "__main__":
    unittest.main()
