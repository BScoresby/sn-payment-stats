#!/usr/bin/env python3
"""Summarize archived individual Nostr zap receipt facts."""

from __future__ import annotations

import gzip
import json
import math
import os
import statistics
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOSTR_DIR = PROJECT_ROOT / "data" / "nostr"
EVENTS_DIR = NOSTR_DIR / "events"
DAILY_PATH = NOSTR_DIR / "daily.json"
ROLLING_PATH = NOSTR_DIR / "latest_30_days.json"
DEFINITIONS_PATH = NOSTR_DIR / "metric_definitions.json"
RUN_PATH = NOSTR_DIR / "runs" / "latest.json"

AMOUNT_BUCKETS = [
    ("under_1_sat", None, 1_000),
    ("1_to_10_sats", 1_000, 11_000),
    ("11_to_21_sats", 11_000, 22_000),
    ("22_to_50_sats", 22_000, 51_000),
    ("51_to_100_sats", 51_000, 101_000),
    ("101_to_250_sats", 101_000, 251_000),
    ("251_to_500_sats", 251_000, 501_000),
    ("501_to_750_sats", 501_000, 751_000),
    ("751_to_1000_sats", 751_000, 1_001_000),
    ("1001_to_2500_sats", 1_001_000, 2_501_000),
    ("2501_to_5000_sats", 2_501_000, 5_001_000),
    ("5001_to_7500_sats", 5_001_000, 7_501_000),
    ("7501_to_10000_sats", 7_501_000, 10_001_000),
    ("10001_to_25000_sats", 10_001_000, 25_001_000),
    ("25001_to_50000_sats", 25_001_000, 50_001_000),
    ("50001_to_75000_sats", 50_001_000, 75_001_000),
    ("75001_to_100000_sats", 75_001_000, 100_001_000),
    ("over_100000_sats", 100_001_000, None),
]


class SummaryError(RuntimeError):
    """Raised when the archived observations cannot be summarized safely."""


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
        raise SummaryError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SummaryError(f"Expected a JSON object in {path}")
    return payload


def load_archive(path: Path) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SummaryError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("receipts"), list):
        raise SummaryError(f"Malformed receipt archive: {path}")
    day = payload.get("date_utc")
    if not isinstance(day, str):
        raise SummaryError(f"Receipt archive is missing date_utc: {path}")
    coverage = payload.get("coverage_at_update", {})
    return day, payload["receipts"], coverage if isinstance(coverage, dict) else {}


def percentile(sorted_values: list[int], probability: float) -> float | None:
    """Linear percentile using index (n - 1) * p."""
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def msats_to_sats(value: int | float | None) -> int | float | None:
    if value is None:
        return None
    result = value / 1000
    return int(result) if result.is_integer() else round(result, 3)


def aggregate(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    amounts = sorted(int(item["amount_msat"]) for item in receipts)
    count = len(amounts)
    total_msats = sum(amounts)
    relay_names = sorted(
        {relay for item in receipts for relay in item.get("observed_on", [])}
    )
    over_one_million = sum(amount > 1_000_000_000 for amount in amounts)
    distribution = {}
    for label, lower, upper in AMOUNT_BUCKETS:
        distribution[label] = sum(
            (lower is None or amount >= lower) and (upper is None or amount < upper)
            for amount in amounts
        )
    frequency = Counter(amounts)
    most_common = sorted(frequency.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "zap_receipt_count": count,
        "total_zap_msats": total_msats,
        "total_zap_sats": msats_to_sats(total_msats),
        "average_zap_sats": msats_to_sats(total_msats / count) if count else None,
        "median_zap_sats": msats_to_sats(statistics.median(amounts)) if count else None,
        "p25_zap_sats": msats_to_sats(percentile(amounts, 0.25)),
        "p75_zap_sats": msats_to_sats(percentile(amounts, 0.75)),
        "p90_zap_sats": msats_to_sats(percentile(amounts, 0.90)),
        "p99_zap_sats": msats_to_sats(percentile(amounts, 0.99)),
        "minimum_zap_sats": msats_to_sats(amounts[0]) if count else None,
        "maximum_zap_sats": msats_to_sats(amounts[-1]) if count else None,
        "unique_senders": len({item["sender_pubkey"] for item in receipts}),
        "unique_recipients": len({item["recipient_pubkey"] for item in receipts}),
        "receipts_over_1m_sats": over_one_million,
        "preimage_present_count": sum(bool(item.get("preimage_present")) for item in receipts),
        "relays_observed": relay_names,
        "amount_distribution": distribution,
        "most_common_amounts": [
            {"amount_sats": msats_to_sats(amount), "receipt_count": occurrences}
            for amount, occurrences in most_common
        ],
    }


def main() -> int:
    definitions = load_json(DEFINITIONS_PATH)
    run_report = load_json(RUN_PATH) if RUN_PATH.exists() else None
    archives: dict[date, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for path in sorted(EVENTS_DIR.glob("*.json.gz")):
        day_text, receipts, coverage = load_archive(path)
        day = date.fromisoformat(day_text)
        unique: list[dict[str, Any]] = []
        for receipt in receipts:
            event_id = receipt.get("id")
            if not isinstance(event_id, str) or event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            unique.append(receipt)
        archives[day] = (unique, coverage)
    if not archives:
        raise SummaryError("No Nostr receipt archives are available")

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    daily_rows = []
    for day, (receipts, coverage) in sorted(archives.items()):
        daily_rows.append(
            {
                "date_utc": day.isoformat(),
                **aggregate(receipts),
                "coverage_at_update": coverage,
            }
        )
    daily_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "day_definition": "UTC calendar day",
        "observation_days": len(daily_rows),
        "observations": daily_rows,
        "global_caveats": definitions.get("global_caveats", []),
    }

    latest_day = max(archives)
    rolling_start = latest_day - timedelta(days=29)
    expected_days = [rolling_start + timedelta(days=offset) for offset in range(30)]
    missing_days = [day.isoformat() for day in expected_days if day not in archives]
    rolling_receipts = [
        receipt
        for day in expected_days
        for receipt in archives.get(day, ([], {}))[0]
    ]
    incomplete_coverage_days = [
        day.isoformat()
        for day in expected_days
        if day in archives
        and archives[day][1].get("complete_relay_count", 0)
        < archives[day][1].get("configured_relay_count", 0)
    ]
    warnings = list(definitions.get("global_caveats", []))
    if missing_days:
        warnings.insert(0, f"The rolling period is missing {len(missing_days)} UTC day(s).")
    if incomplete_coverage_days:
        warnings.insert(
            0,
            f"{len(incomplete_coverage_days)} rolling-period day(s) were last updated with fewer than all configured relays.",
        )
    if run_report and not run_report.get("coverage_gate_passed", False):
        warnings.insert(0, "The latest collection run did not meet minimum relay coverage.")
    elif run_report and run_report.get("complete_relay_count", 0) < run_report.get("configured_relay_count", 0):
        warnings.insert(0, "One or more configured relays failed during the latest collection run.")
    rolling_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "period": {
            "start": rolling_start.isoformat(),
            "end": latest_day.isoformat(),
            "days": 30,
            "missing_days": missing_days,
            "incomplete_relay_coverage_days": incomplete_coverage_days,
        },
        "metrics": aggregate(rolling_receipts),
        "collection_status": {
            "latest_run": run_report,
            "quality_status": "warning" if warnings else "ok",
            "quality_warnings": warnings,
        },
        "methodology": {
            "event_kind": 9735,
            "deduplication_key": "Nostr event ID",
            "median": "Exact median across individual BOLT11 invoice amounts; arithmetic mean of the two central values for an even count.",
            "validation": [
                "receipt event ID and signature",
                "embedded kind 9734 request ID and signature",
                "receipt and request target tags agree",
                "BOLT11 amount is positive",
                "BOLT11 description hash matches the embedded request",
                "request amount matches BOLT11 when present",
                "preimage matches BOLT11 payment hash when present"
            ],
            "not_validated": "LNURL provider authorization and independent Lightning settlement",
        },
    }
    atomic_write_json(DAILY_PATH, daily_payload)
    atomic_write_json(ROLLING_PATH, rolling_payload)
    print(json.dumps({
        "daily_path": str(DAILY_PATH),
        "rolling_path": str(ROLLING_PATH),
        "latest_day": latest_day.isoformat(),
        "rolling_receipts": len(rolling_receipts),
        "median_zap_sats": rolling_payload["metrics"]["median_zap_sats"],
        "missing_days": len(missing_days),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
