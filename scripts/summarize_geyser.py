#!/usr/bin/env python3
"""Summarize recorded Geyser Lightning-funded contribution facts."""

from __future__ import annotations

import gzip
import json
import os
import statistics
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GEYSER_DIR = PROJECT_ROOT / "data" / "geyser"
PAYMENTS_DIR = GEYSER_DIR / "payments"
DAILY_PATH = GEYSER_DIR / "daily.json"
ROLLING_PATH = GEYSER_DIR / "latest_30_days.json"
DEFINITIONS_PATH = GEYSER_DIR / "metric_definitions.json"
RUN_PATH = GEYSER_DIR / "runs" / "latest.json"


class SummaryError(RuntimeError):
    """Raised when archived Geyser observations cannot be summarized safely."""


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
    if not isinstance(payload, dict) or not isinstance(payload.get("payments"), list):
        raise SummaryError(f"Malformed Geyser payment archive: {path}")
    day = payload.get("date_utc")
    if not isinstance(day, str):
        raise SummaryError(f"Geyser archive is missing date_utc: {path}")
    coverage = payload.get("coverage_at_update", {})
    return day, payload["payments"], coverage if isinstance(coverage, dict) else {}


def rounded_mean(total: int, count: int) -> float | None:
    return round(total / count, 3) if count else None


def aggregate(payments: list[dict[str, Any]]) -> dict[str, Any]:
    payment_ids: set[str] = set()
    contribution_amounts: dict[str, int] = {}
    project_ids: set[str] = set()
    payment_types: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    total_payment_amount = 0
    total_accounting_paid = 0
    maximum_payment = None
    for payment in payments:
        payment_id = str(payment["payment_id"])
        if payment_id in payment_ids:
            raise SummaryError(f"Duplicate Geyser payment ID {payment_id}")
        payment_ids.add(payment_id)
        contribution_id = str(payment["contribution_id"])
        contribution_amount = int(payment["contribution_amount_sats"])
        existing = contribution_amounts.get(contribution_id)
        if existing is not None and existing != contribution_amount:
            raise SummaryError(
                f"Contribution {contribution_id} has inconsistent amounts"
            )
        contribution_amounts[contribution_id] = contribution_amount
        project_ids.add(str(payment["project_id"]))
        payment_types[str(payment["payment_type"])] += 1
        methods[str(payment.get("method") or "UNKNOWN")] += 1
        payment_amount = int(payment["payment_amount_sats"])
        accounting_paid = int(payment["accounting_amount_paid_sats"])
        total_payment_amount += payment_amount
        total_accounting_paid += accounting_paid
        maximum_payment = (
            payment_amount
            if maximum_payment is None
            else max(maximum_payment, payment_amount)
        )

    amounts = sorted(contribution_amounts.values())
    contribution_total = sum(amounts)
    return {
        "recorded_lightning_contribution_count": len(contribution_amounts),
        "recorded_lightning_payment_count": len(payment_ids),
        "recorded_lightning_contribution_sats": contribution_total,
        "recorded_lightning_payment_sats": total_payment_amount,
        "recorded_lightning_accounting_amount_paid_sats": total_accounting_paid,
        "average_recorded_contribution_sats": rounded_mean(
            contribution_total, len(amounts)
        ),
        "median_recorded_contribution_sats": (
            statistics.median(amounts) if amounts else None
        ),
        "minimum_recorded_contribution_sats": amounts[0] if amounts else None,
        "maximum_recorded_contribution_sats": amounts[-1] if amounts else None,
        "maximum_recorded_payment_sats": maximum_payment,
        "recorded_contributions_over_1m_sats": sum(
            amount > 1_000_000 for amount in amounts
        ),
        "unique_projects_funded": len(project_ids),
        "payments_by_type": dict(sorted(payment_types.items())),
        "payments_by_method": dict(sorted(methods.items())),
    }


def main() -> int:
    definitions = load_json(DEFINITIONS_PATH)
    run_report = load_json(RUN_PATH) if RUN_PATH.exists() else None
    archives: dict[date, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    seen_payment_ids: set[str] = set()
    for path in sorted(PAYMENTS_DIR.glob("*.json.gz")):
        day_text, payments, coverage = load_archive(path)
        day = date.fromisoformat(day_text)
        unique: list[dict[str, Any]] = []
        for payment in payments:
            payment_id = str(payment.get("payment_id"))
            if not payment_id or payment_id == "None":
                raise SummaryError(f"Payment without an ID in {path}")
            if payment_id in seen_payment_ids:
                raise SummaryError(
                    f"Payment {payment_id} appears in more than one daily archive"
                )
            seen_payment_ids.add(payment_id)
            unique.append(payment)
        archives[day] = (unique, coverage)
    if not archives:
        raise SummaryError("No Geyser payment archives are available")

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    daily_rows = [
        {
            "date_utc": day.isoformat(),
            **aggregate(payments),
            "coverage_at_update": coverage,
        }
        for day, (payments, coverage) in sorted(archives.items())
    ]
    daily_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "day_definition": "UTC calendar day, bucketed by payment paidAt",
        "observation_days": len(daily_rows),
        "observations": daily_rows,
        "global_caveats": definitions.get("global_caveats", []),
    }

    latest_day = max(archives)
    rolling_start = latest_day - timedelta(days=29)
    expected_days = [rolling_start + timedelta(days=offset) for offset in range(30)]
    missing_days = [day.isoformat() for day in expected_days if day not in archives]
    rolling_payments = [
        payment
        for day in expected_days
        for payment in archives.get(day, ([], {}))[0]
    ]
    incomplete_days = [
        day.isoformat()
        for day in expected_days
        if day in archives and archives[day][1].get("status") != "complete"
    ]
    quality_warnings: list[str] = []
    if missing_days:
        quality_warnings.append(
            f"The rolling period is missing {len(missing_days)} UTC day(s)."
        )
    if incomplete_days:
        quality_warnings.append(
            f"{len(incomplete_days)} UTC day(s) have incomplete API coverage."
        )
    if run_report and run_report.get("status") != "complete":
        quality_warnings.append("The latest Geyser collection run failed.")

    rolling_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "period": {
            "start": rolling_start.isoformat(),
            "end": latest_day.isoformat(),
            "days": 30,
            "missing_days": missing_days,
            "incomplete_api_coverage_days": incomplete_days,
        },
        "metrics": aggregate(rolling_payments),
        "collection_status": {
            "latest_run": run_report,
            "quality_status": "warning" if quality_warnings else "ok",
            "quality_warnings": quality_warnings,
            "coverage_scope": "Geyser-recorded contributions only; direct-to-creator payments are unobserved.",
        },
        "methodology": {
            "recommended_zap_like_event_metric": "recorded_lightning_contribution_count",
            "deduplication": {
                "payments": "Geyser payment ID",
                "contributions": "Geyser contribution ID",
            },
            "included_payment_types": [
                "LIGHTNING",
                "LIGHTNING_PODCAST_KEYSEND",
                "LIGHTNING_TO_RSK_SWAP",
            ],
            "excluded_payment_types": [
                "ON_CHAIN",
                "ON_CHAIN_TO_LIGHTNING_SWAP",
                "FIAT_TO_LIGHTNING_SWAP",
                "FIAT",
                "ON_CHAIN_TO_RSK_SWAP",
                "RSK_TO_LIGHTNING_SWAP",
                "RSK_TO_ON_CHAIN_SWAP",
                "RSK_NATIVE_TRANSFER",
                "RSK_AON_CLAIM",
            ],
            "privacy": "The collector does not request or retain funder identities, comments, invoice strings, payment hashes, or preimages.",
        },
        "publication_caveats": definitions.get("global_caveats", []),
    }
    atomic_write_json(DAILY_PATH, daily_payload)
    atomic_write_json(ROLLING_PATH, rolling_payload)
    print(
        json.dumps(
            {
                "daily_path": str(DAILY_PATH),
                "rolling_path": str(ROLLING_PATH),
                "latest_day": latest_day.isoformat(),
                "rolling_contributions": rolling_payload["metrics"][
                    "recorded_lightning_contribution_count"
                ],
                "missing_days": len(missing_days),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
