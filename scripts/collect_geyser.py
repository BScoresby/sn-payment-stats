#!/usr/bin/env python3
"""Collect recorded Geyser contributions funded by payer-origin Lightning."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GEYSER_DIR = PROJECT_ROOT / "data" / "geyser"
PAYMENTS_DIR = GEYSER_DIR / "payments"
RUNS_DIR = GEYSER_DIR / "runs"
CONFIG_PATH = GEYSER_DIR / "config.json"

USER_AGENT = "SNPaymentStats-GeyserCollector/1.0 (+public aggregate research)"

# These types describe a payment initiated over Lightning by the contributor.
# Types such as FIAT_TO_LIGHTNING_SWAP and RSK_TO_LIGHTNING_SWAP are deliberately
# excluded because Lightning is the payout/internal leg, not the contributor's rail.
PAYER_LIGHTNING_TYPES = {
    "LIGHTNING",
    "LIGHTNING_PODCAST_KEYSEND",
    "LIGHTNING_TO_RSK_SWAP",
}

QUERY = """
query GeyserContributions($input: GetContributionsInput) {
  contributionsGet(input: $input) {
    contributions {
      id
      amount
      status
      createdAt
      confirmedAt
      projectId
      payments {
        id
        status
        paymentType
        paymentAmount
        paymentCurrency
        accountingAmountPaid
        paidAt
        method
      }
    }
  }
}
""".strip()


class CollectionError(RuntimeError):
    """Raised when collection cannot safely produce a complete result."""


class RequestBudgetExceeded(CollectionError):
    """Raised before a request would exceed the configured hard budget."""


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


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


def atomic_write_gzip_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        with gzip.open(temporary_name, "wt", encoding="utf-8", compresslevel=9) as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
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
        raise CollectionError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CollectionError(f"Expected a JSON object in {path}")
    return payload


@dataclass
class APIClient:
    api_url: str
    request_budget: int
    request_delay_seconds: float
    timeout_seconds: int
    retries: int
    retry_backoff_seconds: float
    max_response_bytes: int
    requests: int = 0
    retries_used: int = 0
    duration_ms: int = 0

    def _wait(self) -> None:
        if self.requests:
            time.sleep(self.request_delay_seconds)

    def post(self, variables: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": QUERY, "variables": variables}).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if self.requests >= self.request_budget:
                raise RequestBudgetExceeded(
                    f"hard request budget of {self.request_budget} reached"
                )
            self._wait()
            request = urllib.request.Request(
                self.api_url,
                data=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            started = time.monotonic()
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(self.max_response_bytes + 1)
                    if len(raw) > self.max_response_bytes:
                        raise CollectionError(
                            f"Geyser response exceeded {self.max_response_bytes} bytes"
                        )
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise CollectionError("Geyser response was not a JSON object")
                    if payload.get("error"):
                        raise CollectionError(f"Geyser API error: {payload['error']}")
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403, 407, 429}:
                    raise CollectionError(
                        f"Geyser rejected the request with HTTP {exc.code}; stopping without retry"
                    ) from exc
                last_error = exc
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                CollectionError,
            ) as exc:
                last_error = exc
            finally:
                self.duration_ms += round((time.monotonic() - started) * 1000)

            if attempt < self.retries:
                self.retries_used += 1
                time.sleep(self.retry_backoff_seconds * (attempt + 1))

        raise CollectionError(
            f"Unable to query Geyser after {self.retries + 1} attempt(s): {last_error}"
        )


def require_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CollectionError(f"{field} is not an integer: {value!r}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CollectionError(f"{field} is not an integer: {value!r}") from exc
    if result < 0:
        raise CollectionError(f"{field} is negative: {result}")
    return result


def require_positive_id(value: Any, field: str) -> str:
    result = require_nonnegative_int(value, field)
    if result <= 0:
        raise CollectionError(f"{field} is not positive: {result}")
    return str(result)


def require_timestamp_ms(value: Any, field: str) -> int:
    result = require_nonnegative_int(value, field)
    if result < 946_684_800_000:
        raise CollectionError(f"{field} is not a plausible millisecond timestamp: {result}")
    return result


def parse_page(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("errors"):
        raise CollectionError(f"GraphQL returned errors: {json.dumps(payload['errors'])}")
    data = payload.get("data")
    response = data.get("contributionsGet") if isinstance(data, dict) else None
    contributions = response.get("contributions") if isinstance(response, dict) else None
    if not isinstance(contributions, list):
        raise CollectionError("Geyser response is missing contributionsGet.contributions")
    if not all(isinstance(item, dict) for item in contributions):
        raise CollectionError("Geyser returned a malformed contribution")
    return contributions


def fetch_all_contributions(
    client: Any,
    start_ms: int,
    end_ms: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    if start_ms > end_ms:
        raise CollectionError("The requested date range is reversed")
    contributions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        pagination: dict[str, Any] = {"take": page_size}
        if cursor is not None:
            pagination["cursor"] = {"id": cursor}
        variables = {
            "input": {
                "where": {
                    "dateRange": {
                        "startDateTime": start_ms,
                        "endDateTime": end_ms,
                    },
                    "status": "CONFIRMED",
                },
                "orderBy": {"createdAt": "desc"},
                "pagination": pagination,
            }
        }
        page = parse_page(client.post(variables))
        pages += 1
        for contribution in page:
            contribution_id = require_positive_id(
                contribution.get("id"), "contribution.id"
            )
            if contribution_id in seen_ids:
                raise CollectionError(
                    f"Geyser repeated contribution {contribution_id} while paginating"
                )
            seen_ids.add(contribution_id)
            contributions.append(contribution)
        if len(page) < page_size:
            break
        if not page:
            break
        next_cursor = require_positive_id(page[-1].get("id"), "pagination cursor")
        if next_cursor == cursor:
            raise CollectionError("Geyser pagination cursor did not advance")
        cursor = next_cursor
    return contributions, pages


def extract_lightning_facts(
    contributions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen_payment_ids: set[str] = set()
    for contribution in contributions:
        contribution_id = require_positive_id(
            contribution.get("id"), "contribution.id"
        )
        if contribution.get("status") != "CONFIRMED":
            raise CollectionError(
                f"Contribution {contribution_id} is not CONFIRMED"
            )
        contribution_amount = require_nonnegative_int(
            contribution.get("amount"), f"contribution.{contribution_id}.amount"
        )
        created_at = require_timestamp_ms(
            contribution.get("createdAt"), f"contribution.{contribution_id}.createdAt"
        )
        confirmed_at = require_timestamp_ms(
            contribution.get("confirmedAt"),
            f"contribution.{contribution_id}.confirmedAt",
        )
        project_id = require_positive_id(
            contribution.get("projectId"), f"contribution.{contribution_id}.projectId"
        )
        payments = contribution.get("payments")
        if not isinstance(payments, list):
            raise CollectionError(
                f"Contribution {contribution_id} has a malformed payments list"
            )
        for payment in payments:
            if not isinstance(payment, dict):
                raise CollectionError(
                    f"Contribution {contribution_id} has a malformed payment"
                )
            if payment.get("status") != "PAID":
                continue
            payment_type = payment.get("paymentType")
            if payment_type not in PAYER_LIGHTNING_TYPES:
                continue
            payment_id = require_positive_id(
                payment.get("id"), f"contribution.{contribution_id}.payment.id"
            )
            if payment_id in seen_payment_ids:
                raise CollectionError(f"Duplicate paid payment ID {payment_id}")
            seen_payment_ids.add(payment_id)
            if payment.get("paymentCurrency") != "BTCSAT":
                raise CollectionError(
                    f"Payer-Lightning payment {payment_id} is not denominated in BTCSAT"
                )
            paid_at = require_timestamp_ms(
                payment.get("paidAt"), f"payment.{payment_id}.paidAt"
            )
            payment_amount = require_nonnegative_int(
                payment.get("paymentAmount"), f"payment.{payment_id}.paymentAmount"
            )
            accounting_amount_paid = require_nonnegative_int(
                payment.get("accountingAmountPaid"),
                f"payment.{payment_id}.accountingAmountPaid",
            )
            if payment_amount <= 0 or accounting_amount_paid <= 0:
                raise CollectionError(
                    f"Paid Lightning payment {payment_id} has a non-positive amount"
                )
            method = payment.get("method")
            facts.append(
                {
                    "payment_id": payment_id,
                    "contribution_id": contribution_id,
                    "project_id": project_id,
                    "created_at_ms": created_at,
                    "confirmed_at_ms": confirmed_at,
                    "paid_at_ms": paid_at,
                    "payment_type": payment_type,
                    "method": str(method) if method is not None else None,
                    "contribution_amount_sats": contribution_amount,
                    "payment_amount_sats": payment_amount,
                    "accounting_amount_paid_sats": accounting_amount_paid,
                }
            )
    return sorted(facts, key=lambda item: (item["paid_at_ms"], item["payment_id"]))


def completed_days(today_utc: date, count: int) -> list[date]:
    return [today_utc - timedelta(days=offset) for offset in range(count, 0, -1)]


def is_backfill_run(
    existing_archives: bool,
    explicit_days: int | None,
    day_count: int,
    daily_overlap_days: int,
) -> bool:
    return (
        (not existing_archives and explicit_days is None)
        or day_count > daily_overlap_days
    )


def day_start_ms(day: date) -> int:
    return int(
        datetime.combine(day, datetime_time.min, tzinfo=timezone.utc).timestamp()
        * 1000
    )


def replace_day_archives(
    facts: list[dict[str, Any]],
    days: list[date],
    collected_at: str,
    coverage: dict[str, Any],
) -> dict[str, int]:
    by_day: dict[str, list[dict[str, Any]]] = {
        day.isoformat(): [] for day in days
    }
    for fact in facts:
        day_text = datetime.fromtimestamp(
            fact["paid_at_ms"] / 1000, timezone.utc
        ).date().isoformat()
        if day_text in by_day:
            by_day[day_text].append(fact)

    counts: dict[str, int] = {}
    for day in days:
        day_text = day.isoformat()
        day_facts = sorted(
            by_day[day_text], key=lambda item: (item["paid_at_ms"], item["payment_id"])
        )
        atomic_write_gzip_json(
            PAYMENTS_DIR / f"{day_text}.json.gz",
            {
                "schema_version": 1,
                "date_utc": day_text,
                "updated_at": collected_at,
                "recorded_lightning_payment_count": len(day_facts),
                "coverage_at_update": coverage,
                "payments": day_facts,
            },
        )
        counts[day_text] = len(day_facts)
    return counts


def write_run_report(report: dict[str, Any]) -> None:
    stamp = str(report["collected_at"]).replace(":", "-")
    atomic_write_json(RUNS_DIR / "latest.json", report)
    atomic_write_json(RUNS_DIR / f"{stamp}.json", report)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect recorded Geyser contributions funded over Lightning."
    )
    parser.add_argument(
        "--days",
        type=int,
        help="Completed UTC days to refresh (default: configured initial/overlap window)",
    )
    parser.add_argument(
        "--today",
        help="Override today's UTC date for reproducible testing (YYYY-MM-DD)",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and validate without changing files",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_json(args.config)
    query_config = config.get("query")
    if not isinstance(query_config, dict):
        raise CollectionError("config.json is missing the query object")
    required = {
        "initial_backfill_days",
        "daily_overlap_days",
        "maximum_days_per_run",
        "confirmation_lag_buffer_days",
        "page_size",
        "daily_request_budget",
        "backfill_request_budget",
        "request_delay_seconds",
        "request_timeout_seconds",
        "retries",
        "retry_backoff_seconds",
        "max_response_bytes",
    }
    if not required.issubset(query_config):
        raise CollectionError(
            f"config.json is missing query settings: {sorted(required - set(query_config))}"
        )

    today_utc = (
        date.fromisoformat(args.today)
        if args.today
        else datetime.now(timezone.utc).date()
    )
    existing = any(PAYMENTS_DIR.glob("*.json.gz"))
    day_count = args.days or int(
        query_config["daily_overlap_days"]
        if existing
        else query_config["initial_backfill_days"]
    )
    maximum_days = int(query_config["maximum_days_per_run"])
    if day_count < 1 or day_count > maximum_days:
        raise CollectionError(f"--days must be between 1 and {maximum_days}")
    days = completed_days(today_utc, day_count)
    buffer_days = int(query_config["confirmation_lag_buffer_days"])
    query_start = days[0] - timedelta(days=buffer_days)
    query_end_exclusive = today_utc
    start_ms = day_start_ms(query_start)
    end_ms = day_start_ms(query_end_exclusive) - 1
    is_backfill = is_backfill_run(
        existing,
        args.days,
        day_count,
        int(query_config["daily_overlap_days"]),
    )
    budget = int(
        query_config[
            "backfill_request_budget" if is_backfill else "daily_request_budget"
        ]
    )
    client = APIClient(
        api_url=str(config.get("api_url") or ""),
        request_budget=budget,
        request_delay_seconds=float(query_config["request_delay_seconds"]),
        timeout_seconds=int(query_config["request_timeout_seconds"]),
        retries=int(query_config["retries"]),
        retry_backoff_seconds=float(query_config["retry_backoff_seconds"]),
        max_response_bytes=int(query_config["max_response_bytes"]),
    )
    if not client.api_url.startswith("https://"):
        raise CollectionError("Geyser API URL must use HTTPS")

    collected_at = iso_z(datetime.now(timezone.utc))
    report: dict[str, Any] = {
        "schema_version": 1,
        "collected_at": collected_at,
        "status": "failed",
        "period": {
            "start": days[0].isoformat(),
            "end": days[-1].isoformat(),
            "days": day_count,
            "day_definition": "completed UTC days, bucketed by payment paidAt",
        },
        "query_policy": {
            "api_url": client.api_url,
            "query_start": query_start.isoformat(),
            "query_end": days[-1].isoformat(),
            "confirmation_lag_buffer_days": buffer_days,
            "page_size": int(query_config["page_size"]),
            "request_delay_seconds": client.request_delay_seconds,
            "request_budget": budget,
            "retries_per_page": client.retries,
        },
        "requests": 0,
        "retries_used": 0,
        "duration_ms": 0,
        "pages": 0,
        "contributions_returned": 0,
        "recorded_lightning_payments": 0,
        "error": None,
    }
    try:
        contributions, pages = fetch_all_contributions(
            client,
            start_ms,
            end_ms,
            int(query_config["page_size"]),
        )
        facts = extract_lightning_facts(contributions)
        report.update(
            {
                "status": "complete",
                "requests": client.requests,
                "retries_used": client.retries_used,
                "duration_ms": client.duration_ms,
                "pages": pages,
                "contributions_returned": len(contributions),
                "recorded_lightning_payments": len(facts),
            }
        )
        if args.dry_run:
            print(json.dumps(report, indent=2))
            return 0
        coverage = {
            "status": "complete",
            "query_start": query_start.isoformat(),
            "query_end": days[-1].isoformat(),
            "confirmation_lag_buffer_days": buffer_days,
            "pages": pages,
            "requests": client.requests,
            "contributions_returned": len(contributions),
            "direct_payments_included": False,
        }
        day_counts = replace_day_archives(
            facts, days, collected_at, coverage
        )
        report["daily_payment_counts"] = day_counts
        write_run_report(report)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        report.update(
            {
                "requests": client.requests,
                "retries_used": client.retries_used,
                "duration_ms": client.duration_ms,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        if not args.dry_run:
            write_run_report(report)
        print(json.dumps(report, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
