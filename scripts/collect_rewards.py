#!/usr/bin/env python3
"""Collect completed Stacker News reward payouts and funding sources."""

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
REWARDS_DAILY_PATH = DATA_DIR / "rewards_daily.json"
RAW_DIR = DATA_DIR / "raw" / "rewards"
USER_AGENT = "SNPaymentStatsCollector/1.1 (+public aggregate research)"

GROWTH_QUERY = """
query RewardGrowth($from: String!, $to: String!) {
  payouts: stackingGrowth(when: "custom", from: $from, to: $to, sub: "all") {
    time
    data { name value }
  }
  recipients: stackerGrowth(when: "custom", from: $from, to: $to, sub: "all") {
    time
    data { name value }
  }
}
""".strip()


class RewardCollectorError(RuntimeError):
    """Raised when reward collection or validation cannot safely continue."""


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


def post_graphql(query: str, variables: dict[str, Any] | None = None, retries: int = 3) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
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
                    raise RewardCollectorError(f"Stacker News returned HTTP {response.status}")
                decoded = json.load(response)
                if not isinstance(decoded, dict):
                    raise RewardCollectorError("GraphQL response was not a JSON object")
                return decoded
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)

    raise RewardCollectorError(
        f"Unable to query Stacker News after {retries} attempts: {last_error}"
    )


def normalize_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RewardCollectorError(f"{field} is not numeric: {value!r}")
    if not math.isfinite(float(value)) or value < 0:
        raise RewardCollectorError(f"{field} is invalid: {value!r}")
    if float(value).is_integer():
        return int(value)
    return float(value)


def bucket_date(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.astimezone(BUCKET_TIMEZONE).date().isoformat()


def series_to_map(series: Any, label: str) -> dict[str, dict[str, int | float]]:
    if not isinstance(series, list):
        raise RewardCollectorError(f"Missing or invalid {label} series")
    result: dict[str, dict[str, int | float]] = {}
    for bucket in series:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("time"), str):
            raise RewardCollectorError(f"Malformed bucket in {label}")
        day = bucket_date(bucket["time"])
        values: dict[str, int | float] = {}
        if not isinstance(bucket.get("data"), list):
            raise RewardCollectorError(f"Malformed data array in {label} for {day}")
        for entry in bucket["data"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise RewardCollectorError(f"Malformed name/value entry in {label} for {day}")
            values[entry["name"]] = normalize_number(
                entry.get("value"), f"{label}.{day}.{entry['name']}"
            )
        result[day] = values
    return result


def parse_growth_response(payload: dict[str, Any]) -> dict[str, dict[str, int | float]]:
    if payload.get("errors"):
        raise RewardCollectorError(f"Reward growth query returned errors: {json.dumps(payload['errors'])}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RewardCollectorError("Reward growth query is missing data")
    payout_map = series_to_map(data.get("payouts"), "reward_payouts")
    recipient_map = series_to_map(data.get("recipients"), "reward_recipients")
    result: dict[str, dict[str, int | float]] = {}
    for day in sorted(set(payout_map) | set(recipient_map)):
        result[day] = {
            "reward_distributed_sats": payout_map.get(day, {}).get("REWARD", 0),
            "daily_unique_reward_recipients": recipient_map.get(day, {}).get("REWARD", 0),
        }
    return result


def reward_detail_query(days: list[date]) -> tuple[str, dict[str, str]]:
    aliases: dict[str, str] = {}
    fields: list[str] = []
    for index, day in enumerate(days):
        alias = f"reward_{index}"
        day_text = day.isoformat()
        aliases[alias] = day_text
        fields.append(
            f'{alias}: rewards(when: ["{day_text}"]) '
            "{ total time sources { name value } }"
        )
    return "query RewardHistory {\n  " + "\n  ".join(fields) + "\n}", aliases


def parse_detail_response(
    payload: dict[str, Any], aliases: dict[str, str]
) -> dict[str, dict[str, Any]]:
    if payload.get("errors"):
        raise RewardCollectorError(f"Reward detail query returned errors: {json.dumps(payload['errors'])}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RewardCollectorError("Reward detail query is missing data")

    details: dict[str, dict[str, Any]] = {}
    for alias, requested_day in aliases.items():
        rewards = data.get(alias)
        if not isinstance(rewards, list) or len(rewards) != 1 or not isinstance(rewards[0], dict):
            raise RewardCollectorError(f"Malformed reward detail for {requested_day}")
        reward = rewards[0]
        total = normalize_number(reward.get("total"), f"{requested_day}.reward_total_sats")
        timestamp = reward.get("time")
        if not isinstance(timestamp, str):
            raise RewardCollectorError(f"Missing reward timestamp for {requested_day}")
        sources_raw = reward.get("sources")
        if not isinstance(sources_raw, list):
            raise RewardCollectorError(f"Missing reward sources for {requested_day}")
        sources: dict[str, int | float] = {}
        for source in sources_raw:
            if not isinstance(source, dict) or not isinstance(source.get("name"), str):
                raise RewardCollectorError(f"Malformed reward source for {requested_day}")
            sources[source["name"]] = normalize_number(
                source.get("value"), f"{requested_day}.sources.{source['name']}"
            )
        details[requested_day] = {
            "reward_pool_sats": total,
            "reward_time": timestamp,
            "reward_sources_msats": dict(sorted(sources.items())),
        }
    return details


def build_rows(
    requested_days: list[date],
    growth: dict[str, dict[str, int | float]],
    details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for reward_day in requested_days:
        day = reward_day.isoformat()
        detail = details.get(day)
        if detail is None:
            raise RewardCollectorError(f"Missing reward detail for {day}")
        growth_values = growth.get(day, {})
        reward_pool_sats = detail["reward_pool_sats"]
        recipients = normalize_number(
            growth_values.get("daily_unique_reward_recipients", 0),
            f"{day}.daily_unique_reward_recipients",
        )
        distributed_sats = normalize_number(
            growth_values.get("reward_distributed_sats", 0),
            f"{day}.reward_distributed_sats",
        )
        source_total_msats = sum(detail["reward_sources_msats"].values())
        warnings: list[str] = []
        if distributed_sats > 0 and recipients == 0:
            warnings.append("positive_rewards_with_zero_recipients")
        if reward_pool_sats > 0 and not detail["reward_sources_msats"]:
            warnings.append("positive_rewards_with_no_sources")
        if abs(float(reward_pool_sats) - float(source_total_msats) / 1000) > 1:
            warnings.append("reward_total_disagrees_with_sources")
        if float(distributed_sats) > float(reward_pool_sats) + 1:
            warnings.append("distributed_rewards_exceed_reward_pool")
        if reward_pool_sats > 0 and bucket_date(detail["reward_time"]) != day:
            warnings.append("reward_timestamp_date_mismatch")

        rows.append(
            {
                "reward_date": day,
                "source_activity_date": (reward_day - timedelta(days=1)).isoformat(),
                "reward_pool_sats": reward_pool_sats,
                "reward_distributed_sats": distributed_sats,
                "reward_distribution_gap_sats": normalize_number(
                    max(float(reward_pool_sats) - float(distributed_sats), 0),
                    f"{day}.reward_distribution_gap_sats",
                ),
                "daily_unique_reward_recipients": recipients,
                "reward_sources_msats": detail["reward_sources_msats"],
                "reward_source_total_msats": normalize_number(
                    source_total_msats, f"{day}.reward_source_total_msats"
                ),
                "quality_status": "warning" if warnings else "ok",
                "quality_warnings": warnings,
                "anomaly_flags": [],
            }
        )
    return rows


def add_anomaly_flags(rows: list[dict[str, Any]]) -> None:
    prior: list[float] = []
    for row in rows:
        row["anomaly_flags"] = []
        current = float(row["reward_pool_sats"])
        baseline = statistics.median(prior[-14:]) if prior else 0
        if len(prior) >= 7 and baseline > 0 and current > baseline * 10:
            row["anomaly_flags"].append("reward_pool_above_10x_14_day_median")
        if len(prior) >= 7 and baseline > 0 and current < baseline / 10:
            row["anomaly_flags"].append("reward_pool_below_one_tenth_14_day_median")
        prior.append(current)


def load_existing() -> dict[str, Any]:
    if not REWARDS_DAILY_PATH.exists():
        return {"schema_version": 1, "observations": []}
    try:
        with REWARDS_DAILY_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RewardCollectorError(f"Cannot read {REWARDS_DAILY_PATH}: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("observations"), list):
        raise RewardCollectorError(f"Unsupported or malformed dataset: {REWARDS_DAILY_PATH}")
    return payload


def merge_rows(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {row["reward_date"]: row for row in existing}
    merged.update({row["reward_date"]: row for row in incoming})
    rows = [merged[key] for key in sorted(merged)]
    add_anomaly_flags(rows)
    return rows


def date_window(days: int, now: datetime) -> list[date]:
    today = now.astimezone(BUCKET_TIMEZONE).date()
    return [today - timedelta(days=offset) for offset in reversed(range(days))]


def growth_window(requested_days: list[date]) -> tuple[datetime, datetime]:
    # The growth API needs a range of at least seven days to return daily buckets.
    end_day = requested_days[-1] + timedelta(days=1)
    start_day = min(requested_days[0], end_day - timedelta(days=7))
    return (
        datetime.combine(start_day, datetime_time.min, tzinfo=BUCKET_TIMEZONE),
        datetime.combine(end_day, datetime_time.min, tzinfo=BUCKET_TIMEZONE),
    )


def batched(values: list[date], size: int) -> list[list[date]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, help="reward dates to request (1-119)")
    parser.add_argument("--bootstrap-days", type=int, default=90)
    parser.add_argument("--refresh-days", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=7)
    parser.add_argument("--batch-pause", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true", help="query and validate without writing files")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    existing = load_existing()
    chosen_days = args.days if args.days is not None else (
        args.bootstrap_days if not existing["observations"] else args.refresh_days
    )
    if not 1 <= chosen_days <= 119:
        raise RewardCollectorError("The reward lookback must be between 1 and 119 days")
    if not 1 <= args.batch_size <= 10:
        raise RewardCollectorError("The reward detail batch size must be between 1 and 10")
    if args.batch_pause < 0:
        raise RewardCollectorError("The reward detail batch pause cannot be negative")

    collected_at = datetime.now(timezone.utc)
    requested_days = date_window(chosen_days, collected_at)
    from_dt, to_dt = growth_window(requested_days)
    print(
        f"Requesting {chosen_days} reward dates: "
        f"{requested_days[0]} through {requested_days[-1]}"
    )

    growth_response = post_graphql(
        GROWTH_QUERY,
        {"from": str(int(from_dt.timestamp() * 1000)), "to": str(int(to_dt.timestamp() * 1000))},
    )
    growth = parse_growth_response(growth_response)

    detail_responses: list[dict[str, Any]] = []
    details: dict[str, dict[str, Any]] = {}
    batches = batched(requested_days, args.batch_size)
    for index, days in enumerate(batches):
        query, aliases = reward_detail_query(days)
        response = post_graphql(query)
        details.update(parse_detail_response(response, aliases))
        detail_responses.append(
            {"dates": [day.isoformat() for day in days], "response": response}
        )
        if index + 1 < len(batches) and args.batch_pause:
            time.sleep(args.batch_pause)

    incoming = build_rows(requested_days, growth, details)
    merged = merge_rows(existing["observations"], incoming)

    if args.dry_run:
        print(f"Validated {len(incoming)} reward dates; no files changed")
        return 0

    archive = {
        "collected_at": iso_z(collected_at),
        "source": API_URL,
        "request": {
            "first_reward_date": requested_days[0].isoformat(),
            "last_reward_date": requested_days[-1].isoformat(),
            "days": chosen_days,
            "detail_batch_size": args.batch_size,
        },
        "growth_response": growth_response,
        "detail_responses": detail_responses,
    }
    daily = {
        "schema_version": 1,
        "source": API_URL,
        "bucket_timezone": "America/Chicago",
        "date_semantics": (
            "reward_date is the payout date; source_activity_date is the preceding "
            "America/Chicago day whose activity funded that payout"
        ),
        "updated_at": iso_z(collected_at),
        "observation_count": len(merged),
        "observations": merged,
    }
    history_name = (
        f"{collected_at.date().isoformat()}_"
        f"{requested_days[0].isoformat()}_"
        f"{requested_days[-1].isoformat()}-inclusive.json"
    )
    atomic_write_json(RAW_DIR / "latest_response.json", archive)
    atomic_write_json(RAW_DIR / "history" / history_name, archive)
    atomic_write_json(REWARDS_DAILY_PATH, daily)
    print(
        f"Updated {REWARDS_DAILY_PATH.relative_to(PROJECT_ROOT)} "
        f"with {len(merged)} total reward dates"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RewardCollectorError as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
