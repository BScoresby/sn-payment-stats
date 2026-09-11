#!/usr/bin/env python3
"""Build an evidence-based audit of approved and candidate Nostr zap relays."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOSTR_DIR = PROJECT_ROOT / "data" / "nostr"
EVENTS_DIR = NOSTR_DIR / "events"
CONFIG_PATH = NOSTR_DIR / "relay_config.json"
RUN_PATH = NOSTR_DIR / "runs" / "latest.json"
OUTPUT_PATH = NOSTR_DIR / "relay_audit.json"


class AuditError(RuntimeError):
    """Raised when the stored relay evidence cannot be audited."""


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuditError(f"Expected a JSON object in {path}")
    return payload


def load_receipts(events_dir: Path) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for path in sorted(events_dir.glob("*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise AuditError(f"Cannot read {path}: {exc}") from exc
        receipts = payload.get("receipts") if isinstance(payload, dict) else None
        if not isinstance(receipts, list):
            raise AuditError(f"Malformed receipt archive: {path}")
        for receipt in receipts:
            event_id = receipt.get("id") if isinstance(receipt, dict) else None
            if isinstance(event_id, str):
                by_id[event_id] = receipt
    return list(by_id.values())


def build_audit(
    config: dict[str, Any],
    latest_run: dict[str, Any] | None,
    receipts: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    approved_entries = [
        item
        for item in config.get("relays", [])
        if isinstance(item, dict) and item.get("enabled", True)
    ]
    approved_urls = {str(item.get("url", "")).rstrip("/") for item in approved_entries}
    latest_by_url = {
        str(item.get("url", "")).rstrip("/"): item
        for item in (latest_run or {}).get("relays", [])
        if isinstance(item, dict)
    }

    sources_by_id = {
        receipt["id"]: {
            str(url).rstrip("/") for url in receipt.get("observed_on", [])
        }
        for receipt in receipts
    }
    requested_counter: Counter[str] = Counter()
    for receipt in receipts:
        requested_counter.update(
            str(url).rstrip("/") for url in receipt.get("requested_relays", [])
        )

    union_count = len(receipts)
    relay_rows = []
    for entry in approved_entries:
        url = str(entry.get("url", "")).rstrip("/")
        observed = [
            receipt for receipt in receipts if url in sources_by_id.get(receipt["id"], set())
        ]
        unique = [
            receipt
            for receipt in observed
            if sources_by_id.get(receipt["id"], set()) == {url}
        ]
        timestamps = [int(receipt["created_at"]) for receipt in observed]
        latest = latest_by_url.get(url, {})
        if not receipts:
            assessment = "insufficient_data"
        elif unique:
            assessment = "adds_unique_receipts"
        elif observed:
            assessment = "overlap_only_so_far"
        else:
            assessment = "no_receipts_observed"
        relay_rows.append(
            {
                "url": url,
                "label": entry.get("label"),
                "configured_rationale": entry.get("rationale"),
                "latest_query_status": latest.get("status"),
                "latest_query_complete": latest.get("complete"),
                "latest_nip11": latest.get("nip11"),
                "observed_receipts": len(observed),
                "unique_contribution": len(unique),
                "coverage_of_stored_union_pct": (
                    round(len(observed) / union_count * 100, 2) if union_count else None
                ),
                "requested_by_receipts": requested_counter[url],
                "oldest_observed_receipt": (
                    datetime.fromtimestamp(min(timestamps), timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                    if timestamps
                    else None
                ),
                "newest_observed_receipt": (
                    datetime.fromtimestamp(max(timestamps), timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                    if timestamps
                    else None
                ),
                "assessment": assessment,
            }
        )

    candidates = [
        {"url": url, "requested_by_receipts": count, "approved": False}
        for url, count in requested_counter.most_common()
        if url not in approved_urls
    ][:50]
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "method": {
            "coverage": "Fraction of the locally stored, deduplicated valid receipt union observed on this approved relay.",
            "unique_contribution": "Stored valid receipts observed on this approved relay and no other approved relay.",
            "candidate_discovery": "Secure relay URLs listed in embedded kind 9734 relays tags. Candidates are never contacted automatically.",
            "limitations": [
                "This measures coverage relative to the local union, not all Nostr zap receipts.",
                "A relay can retain events that were not requested during a successful collection run.",
                "NIP-11 metadata does not guarantee historical retention."
            ],
        },
        "stored_valid_receipt_union": union_count,
        "approved_relays": relay_rows,
        "unapproved_candidates": candidates,
    }


def main() -> int:
    config = load_json(CONFIG_PATH)
    latest_run = load_json(RUN_PATH) if RUN_PATH.exists() else None
    receipts = load_receipts(EVENTS_DIR)
    generated_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    audit = build_audit(config, latest_run, receipts, generated_at)
    atomic_write_json(OUTPUT_PATH, audit)
    print(
        json.dumps(
            {
                "output": str(OUTPUT_PATH),
                "stored_valid_receipt_union": audit["stored_valid_receipt_union"],
                "approved_relays": len(audit["approved_relays"]),
                "unapproved_candidates": len(audit["unapproved_candidates"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
