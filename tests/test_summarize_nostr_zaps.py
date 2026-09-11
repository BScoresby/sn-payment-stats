import importlib.util
import unittest
from pathlib import Path


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


summarize = load_module("summarize_nostr_zaps", "scripts/summarize_nostr_zaps.py")


def fact(event_id, amount_msat, sender="sender", recipient="recipient"):
    return {
        "id": event_id,
        "amount_msat": amount_msat,
        "sender_pubkey": sender,
        "recipient_pubkey": recipient,
        "observed_on": ["wss://relay.example"],
        "preimage_present": False,
    }


class NostrSummaryTests(unittest.TestCase):
    def test_odd_exact_median(self):
        result = summarize.aggregate([
            fact("a", 1000), fact("b", 21000), fact("c", 100000),
        ])
        self.assertEqual(result["median_zap_sats"], 21)
        self.assertEqual(result["zap_receipt_count"], 3)
        self.assertEqual(result["total_zap_sats"], 122)
        self.assertEqual(sum(result["amount_distribution"].values()), 3)

    def test_even_exact_median_uses_two_central_values(self):
        result = summarize.aggregate([
            fact("a", 1000), fact("b", 21000), fact("c", 100000), fact("d", 1000000),
        ])
        self.assertEqual(result["median_zap_sats"], 60.5)

    def test_unique_keys_are_deduplicated(self):
        result = summarize.aggregate([
            fact("a", 1000, "s1", "r1"),
            fact("b", 2000, "s1", "r2"),
        ])
        self.assertEqual(result["unique_senders"], 1)
        self.assertEqual(result["unique_recipients"], 2)


if __name__ == "__main__":
    unittest.main()
