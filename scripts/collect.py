#!/usr/bin/env python3
"""Collect daily Stacker News payment statistics from the public GraphQL API."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

API_URL = "https://stacker.news/api/graphql"
BUCKET_TIMEZONE = ZoneInfo("America/Chicago")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DAILY_PATH = DATA_DIR / "daily.json"
RAW_DIR = DATA_DIR / "raw"
USER_AGENT = "SNPaymentStatsCollector/1.0 (+public aggregate research)"

QUERY = """
query PaymentGrowth($from: String!, $to: String!) {
  spending: spendingGrowth(when: "custom", from: $from, to: $to, sub: "all") {
    time
    data { name value }
  }
  actions: itemGrowth(when: "custom", from: $from, to: $to, sub: "all") {
    time
    data { name value }
  }
  spenders: spenderGrowth(when: "custom", from: $from, to: $to, sub: "all") {
    time
    data { name value }
  }
}
""".strip()


class CollectorError(RuntimeError):
    """Raised when collection or validation cannot safely continue."""


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def post_graphql(from_ms: int, to_ms: int, retries: int = 3) -> dict[str, Any]:
    body = json.dumps(
        {"query": QUERY, "variables": {"from": str(from_ms), "to": str(to_ms)}}
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status != 200:
                    raise CollectorError(f"Stacker News returned HTTP {response.status}")
                decoded = json.load(response)
                if not isinstance(decoded, dict):
                    raise CollectorError("GraphQL response was not a JSON object")
                return decoded
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)

    raise CollectorError(f"Unable to query Stacker News after {retries} attempts: {last_error}")


def bucket_date(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.astimezone(BUCKET_TIMEZONE).date().isoformat()


def normalize_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CollectorError(f"{field} is not numeric: {value!r}")
    if not math.isfinite(float(value)) or value < 0:
        raise CollectorError(f"{field} is invalid: {value!r}")
    if float(value).is_integer():
        return int(value)
    return float(value)


def series_to_map(series: Any, label: str) -> dict[str, dict[str, int | float]]:
    if not isinstance(series, list):
        raise CollectorError(f"Missing or invalid {label} series")

    result: dict[str, dict[str, int | float]] = {}
    for bucket in series:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("time"), str):
            raise CollectorError(f"Malformed bucket in {label}")
        day = bucket_date(bucket["time"])
        values: dict[str, int | float] = {}
        if not isinstance(bucket.get("data"), list):
            raise CollectorError(f"Malformed data array in {label} for {day}")
        for entry in bucket["data"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise CollectorError(f"Malformed name/value entry in {label} for {day}")
            values[entry["name"]] = normalize_number(
                entry.get("value"), f"{label}.{day}.{entry['name']}"
            )
        if day in result:
            raise CollectorError(f"Duplicate {label} bucket for {day}")
        result[day] = values
    return result


def parse_response(payload: dict[str, Any], today_local: date) -> list[dict[str, Any]]:
    errors = payload.get("errors")
    if errors:
        raise CollectorError(f"GraphQL returned errors: {json.dumps(errors)}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CollectorError("GraphQL response is missing data")

    spending = series_to_map(data.get("spending"), "spending")
    actions = series_to_map(data.get("actions"), "actions")
    spenders = series_to_map(data.get("spenders"), "spenders")
    dates = set(spending) | set(actions) | set(spenders)
    if not dates:
        raise CollectorError("The API returned no time buckets")

    rows: list[dict[str, Any]] = []
    for day in sorted(dates):
        if date.fromisoformat(day) >= today_local:
            continue
        if day not in spending or day not in actions or day not in spenders:
            raise CollectorError(f"The API returned incomplete series for {day}")

        action_values = actions[day]
        spending_values = spending[day]
        spender_values = spenders[day]
        zap_actions = normalize_number(action_values.get("ZAP", 0), f"{day}.zap_actions")
        zap_sats = normalize_number(spending_values.get("ZAP", 0), f"{day}.zap_sats")
        unique_zappers = normalize_number(
            spender_values.get("ZAP", 0), f"{day}.daily_unique_zappers"
        )
        unique_spenders = normalize_number(
            spender_values.get("total", 0), f"{day}.daily_unique_spenders"
        )
        tracked_actions = sum(action_values.values())
        warnings: list[str] = []
        if zap_actions == 0:
            warnings.append("zero_zap_actions")
        if unique_zappers > zap_actions:
            warnings.append("unique_zappers_exceed_zap_actions")
        if unique_spenders < unique_zappers:
            warnings.append("unique_spenders_below_unique_zappers")

        rows.append(
            {
                "date": day,
                "zap_actions": zap_actions,
                "zap_sats": zap_sats,
                "daily_unique_zappers": unique_zappers,
                "daily_unique_spenders": unique_spenders,
                "tracked_paid_actions": normalize_number(tracked_actions, f"{day}.tracked_paid_actions"),
                "content_items_created": normalize_number(
                    action_values.get("ITEM_CREATE", 0), f"{day}.content_items_created"
                ),
                "actions_by_type": dict(sorted(action_values.items())),
                "spending_sats_by_type": dict(sorted(spending_values.items())),
                "daily_spenders_by_type": dict(sorted(spender_values.items())),
                "quality_status": "warning" if warnings else "ok",
                "quality_warnings": warnings,
            }
        )
    if not rows:
        raise CollectorError("The API returned no completed daily buckets")
    return rows


def add_anomaly_warnings(rows: list[dict[str, Any]]) -> None:
    prior: list[float] = []
    for row in rows:
        current = float(row["zap_actions"])
        baseline = statistics.median(prior[-14:]) if prior else 0
        if len(prior) >= 7 and baseline > 0 and (current > baseline * 10 or current < baseline / 10):
            row["quality_warnings"].append("zap_actions_far_from_14_day_median")
            row["quality_status"] = "warning"
        prior.append(current)


def load_existing() -> dict[str, Any]:
    if not DAILY_PATH.exists():
        return {"schema_version": 1, "observations": []}
    try:
        with DAILY_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError(f"Cannot read {DAILY_PATH}: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("observations"), list):
        raise CollectorError(f"Unsupported or malformed dataset: {DAILY_PATH}")
    return payload


def merge_rows(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {row["date"]: row for row in existing}
    merged.update({row["date"]: row for row in incoming})
    rows = [merged[key] for key in sorted(merged)]
    add_anomaly_warnings(rows)
    return rows


def window(days: int, now: datetime) -> tuple[datetime, datetime]:
    today = now.astimezone(BUCKET_TIMEZONE).date()
    start_day = today - timedelta(days=days)
    start = datetime.combine(start_day, datetime_time.min, tzinfo=BUCKET_TIMEZONE)
    end = datetime.combine(today, datetime_time.min, tzinfo=BUCKET_TIMEZONE)
    return start, end


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, help="completed days to request (7-119)")
    parser.add_argument("--bootstrap-days", type=int, default=90)
    parser.add_argument("--refresh-days", type=int, default=35)
    parser.add_argument("--dry-run", action="store_true", help="query and validate without writing files")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    existing = load_existing()
    chosen_days = args.days if args.days is not None else (
        args.bootstrap_days if not existing["observations"] else args.refresh_days
    )
    if not 7 <= chosen_days <= 119:
        raise CollectorError("The lookback must be between 7 and 119 days to obtain daily API buckets")

    collected_at = datetime.now(timezone.utc)
    from_dt, to_dt = window(chosen_days, collected_at)
    from_ms = int(from_dt.timestamp() * 1000)
    to_ms = int(to_dt.timestamp() * 1000)
    print(f"Requesting {chosen_days} completed days: {from_dt.date()} through {(to_dt.date() - timedelta(days=1))}")
    response = post_graphql(from_ms, to_ms)
    incoming = parse_response(response, to_dt.date())
    merged = merge_rows(existing["observations"], incoming)

    if args.dry_run:
        print(f"Validated {len(incoming)} daily buckets; no files changed")
        return 0

    archive = {
        "collected_at": iso_z(collected_at),
        "source": API_URL,
        "request": {
            "from": iso_z(from_dt),
            "to_exclusive": iso_z(to_dt),
            "days": chosen_days,
        },
        "response": response,
    }
    daily = {
        "schema_version": 1,
        "source": API_URL,
        "bucket_timezone": "America/Chicago",
        "updated_at": iso_z(collected_at),
        "observation_count": len(merged),
        "observations": merged,
    }
    history_name = (
        f"{collected_at.date().isoformat()}_"
        f"{from_dt.date().isoformat()}_"
        f"{to_dt.date().isoformat()}-exclusive.json"
    )
    history_path = RAW_DIR / "history" / history_name
    atomic_write_json(RAW_DIR / "latest_response.json", archive)
    atomic_write_json(history_path, archive)
    atomic_write_json(DAILY_PATH, daily)
    print(f"Updated {DAILY_PATH.relative_to(PROJECT_ROOT)} with {len(merged)} total days")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CollectorError as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
