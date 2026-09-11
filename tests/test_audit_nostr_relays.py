import importlib.util
import unittest
from pathlib import Path


def load_module(name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load_module("audit_nostr_relays", "scripts/audit_nostr_relays.py")


class RelayAuditTests(unittest.TestCase):
    def test_unique_contribution_and_candidates(self):
        config = {
            "relays": [
                {"url": "wss://one.example", "label": "One", "enabled": True},
                {"url": "wss://two.example", "label": "Two", "enabled": True},
            ]
        }
        receipts = [
            {
                "id": "a",
                "created_at": 1789000000,
                "observed_on": ["wss://one.example"],
                "requested_relays": ["wss://candidate.example"],
            },
            {
                "id": "b",
                "created_at": 1789000100,
                "observed_on": ["wss://one.example", "wss://two.example"],
                "requested_relays": ["wss://candidate.example"],
            },
        ]
        result = audit.build_audit(config, None, receipts, "2026-09-11T00:00:00Z")
        one = result["approved_relays"][0]
        self.assertEqual(one["observed_receipts"], 2)
        self.assertEqual(one["unique_contribution"], 1)
        self.assertEqual(one["coverage_of_stored_union_pct"], 100.0)
        self.assertEqual(
            result["unapproved_candidates"][0],
            {
                "url": "wss://candidate.example",
                "requested_by_receipts": 2,
                "approved": False,
            },
        )
