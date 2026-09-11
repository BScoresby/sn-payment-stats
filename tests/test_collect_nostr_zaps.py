import hashlib
import importlib.util
import json
import sys
import asyncio
import unittest
from pathlib import Path


DEPENDENCIES_AVAILABLE = bool(
    importlib.util.find_spec("nostr_sdk")
    and importlib.util.find_spec("bolt11")
    and importlib.util.find_spec("websockets")
)


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if DEPENDENCIES_AVAILABLE:
    collect = load_module("collect_nostr_zaps", "scripts/collect_nostr_zaps.py")
    from nostr_sdk import EventBuilder, Keys, Kind, Tag, Timestamp


@unittest.skipUnless(DEPENDENCIES_AVAILABLE, "Nostr optional dependencies are not installed")
class NostrCollectorTests(unittest.TestCase):
    def build_receipt(self, amount_msat=21000):
        sender = Keys.generate()
        recipient = Keys.generate().public_key().to_hex()
        request = (
            EventBuilder(Kind(9734), "")
            .tags([
                Tag.parse(["p", recipient]),
                Tag.parse(["amount", str(amount_msat)]),
                Tag.parse(["relays", "wss://one.example"]),
            ])
            .custom_created_at(Timestamp.from_secs(1789000000))
            .finalize(sender)
        )
        description = request.as_json()
        description_hash = hashlib.sha256(description.encode()).hexdigest()
        receipt = (
            EventBuilder(Kind(9735), "")
            .tags([
                Tag.parse(["p", recipient]),
                Tag.parse(["bolt11", "lnbc-fake-for-test"]),
                Tag.parse(["description", description]),
            ])
            .custom_created_at(Timestamp.from_secs(1789000010))
            .finalize(Keys.generate())
        )

        class Invoice:
            pass

        invoice = Invoice()
        invoice.amount_msat = amount_msat
        invoice.description_hash = description_hash
        invoice.payment_hash = None
        return json.loads(receipt.as_json()), invoice, sender.public_key().to_hex(), recipient

    def test_valid_receipt_extracts_compact_fact(self):
        receipt, invoice, sender, recipient = self.build_receipt()
        fact = collect.parse_zap_receipt(
            receipt, ["wss://one.example"], invoice_decoder=lambda _: invoice
        )
        self.assertEqual(fact["amount_msat"], 21000)
        self.assertEqual(fact["sender_pubkey"], sender)
        self.assertEqual(fact["recipient_pubkey"], recipient)
        self.assertEqual(fact["observed_on"], ["wss://one.example"])
        self.assertEqual(fact["requested_relays"], ["wss://one.example"])

    def test_description_hash_mismatch_is_rejected(self):
        receipt, invoice, _, _ = self.build_receipt()
        invoice.description_hash = "00" * 32
        with self.assertRaisesRegex(collect.CollectionError, "description hash"):
            collect.parse_zap_receipt(receipt, [], invoice_decoder=lambda _: invoice)

    def test_invoice_amount_mismatch_is_rejected(self):
        receipt, invoice, _, _ = self.build_receipt()
        invoice.amount_msat += 1000
        with self.assertRaisesRegex(collect.CollectionError, "amount does not match"):
            collect.parse_zap_receipt(receipt, [], invoice_decoder=lambda _: invoice)

    def test_fixed_windows_have_predictable_request_count(self):
        days = [
            collect.date(2026, 9, 8),
            collect.date(2026, 9, 9),
            collect.date(2026, 9, 10),
        ]
        self.assertEqual(len(collect.make_windows(days, 6)), 12)

    def test_candidate_url_safety(self):
        self.assertEqual(
            collect.canonical_relay_url("wss://Relay.Example/", require_secure=True),
            "wss://relay.example",
        )
        self.assertIsNone(
            collect.canonical_relay_url("ws://relay.example", require_secure=True)
        )
        self.assertIsNone(
            collect.canonical_relay_url("wss://127.0.0.1", require_secure=True)
        )


class FakeWebSocket:
    def __init__(self, response_factory=None, delay=None, responses=None):
        self.response_factory = response_factory
        self.delay = delay
        self.responses = list(responses or [])
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if self.delay is not None:
            await asyncio.sleep(self.delay)
        subscription_id = self.sent[0][1]
        if self.responses:
            response = self.responses.pop(0)
            return json.dumps(response(subscription_id))
        return json.dumps(self.response_factory(subscription_id))


@unittest.skipUnless(DEPENDENCIES_AVAILABLE, "Nostr optional dependencies are not installed")
class RelayProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_eose_allows_a_real_zero(self):
        websocket = FakeWebSocket(lambda subscription_id: ["EOSE", subscription_id])
        result = await collect.query_window(websocket, 1, 2, 100, 1)
        self.assertEqual(result.status, "complete_but_zero")
        self.assertTrue(result.complete)
        self.assertEqual(websocket.sent[-1][0], "CLOSE")

    async def test_closed_rate_limit_is_not_a_zero(self):
        websocket = FakeWebSocket(
            lambda subscription_id: [
                "CLOSED",
                subscription_id,
                "rate-limited: slow down",
            ]
        )
        result = await collect.query_window(websocket, 1, 2, 100, 1)
        self.assertEqual(result.status, "rate_limited")
        self.assertFalse(result.complete)

    async def test_silent_relay_times_out_instead_of_returning_zero(self):
        websocket = FakeWebSocket(lambda _: [], delay=0.05)
        result = await collect.query_window(websocket, 1, 2, 100, 0.01)
        self.assertEqual(result.status, "query_timeout")
        self.assertFalse(result.complete)

    async def test_exact_result_limit_is_treated_as_possible_truncation(self):
        websocket = FakeWebSocket(
            responses=[
                lambda subscription_id: [
                    "EVENT",
                    subscription_id,
                    {"id": "a"},
                ],
                lambda subscription_id: [
                    "EVENT",
                    subscription_id,
                    {"id": "b"},
                ],
                lambda subscription_id: ["EOSE", subscription_id],
            ]
        )
        result = await collect.query_window(websocket, 1, 2, 2, 1)
        self.assertEqual(result.status, "result_limit_reached")
        self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
