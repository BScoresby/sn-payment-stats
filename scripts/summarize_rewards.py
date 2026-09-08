#!/usr/bin/env python3
"""Build completed Monday-Sunday summaries from data/rewards_daily.json."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
REWARDS_DAILY_PATH = DATA_DIR / "rewards_daily.json"
REWARDS_WEEKLY_PATH = DATA_DIR / "rewards_weekly.json"
LATEST_REWARDS_PATH = DATA_DIR / "latest_rewards_week.json"
DEFINITIONS_PATH = DATA_DIR / "metric_definitions.json"


class RewardSummaryError(RuntimeError):
    """Raised when a safe reward summary cannot be created."""


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


def ratio(numerator: float, denominator: float, digits: int = 2) -> float | None:
    return round(numerator / denominator, digits) if denominator else None


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or not previous:
        return None
    return round((current - previous) / previous * 100, 2)


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RewardSummaryError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RewardSummaryError(f"Expected a JSON object in {path}")
    return payload


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def aggregate_week(rows: list[dict[str, Any]]) -> dict[str, Any]:
    start = date.fromisoformat(rows[0]["reward_date"])
    end = date.fromisoformat(rows[-1]["reward_date"])
    reward_pool_sats = sum(row["reward_pool_sats"] for row in rows)
    reward_distributed_sats = sum(row["reward_distributed_sats"] for row in rows)
    recipients = [row["daily_unique_reward_recipients"] for row in rows]
    recipient_days = sum(recipients)
    source_totals: dict[str, int | float] = {}
    for row in rows:
        for source, value in row["reward_sources_msats"].items():
            source_totals[source] = source_totals.get(source, 0) + value
    source_total_msats = sum(source_totals.values())
    source_shares = {
        source: ratio(value * 100, source_total_msats)
        for source, value in sorted(source_totals.items())
    }
    warning_days = [
        row["reward_date"] for row in rows if row.get("quality_status") != "ok"
    ]
    anomaly_days = [
        row["reward_date"] for row in rows if row.get("anomaly_flags")
    ]

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": 7},
        "source_activity_period": {
            "start": (start - timedelta(days=1)).isoformat(),
            "end": (end - timedelta(days=1)).isoformat(),
        },
        "reward_pool_sats": reward_pool_sats,
        "reward_distributed_sats": reward_distributed_sats,
        "reward_distribution_gap_sats": reward_pool_sats - reward_distributed_sats,
        "average_daily_reward_pool_sats": ratio(reward_pool_sats, 7),
        "average_daily_reward_distributed_sats": ratio(reward_distributed_sats, 7),
        "average_daily_unique_reward_recipients": round(statistics.mean(recipients), 2),
        "peak_daily_unique_reward_recipients": max(recipients),
        "reward_recipient_days": recipient_days,
        "reward_sats_per_recipient_day": ratio(reward_distributed_sats, recipient_days),
        "reward_sources_msats": dict(sorted(source_totals.items())),
        "reward_source_shares_pct": source_shares,
        "boost_and_downzap_reward_share_pct": ratio(
            100 * (
                source_totals.get("BOOST", 0) + source_totals.get("DOWN_ZAP", 0)
            ),
            source_total_msats,
        ),
        "quality_status": "warning" if warning_days else "ok",
        "warning_days": warning_days,
        "anomaly_days": anomaly_days,
    }


def add_comparisons(weeks: list[dict[str, Any]]) -> None:
    metrics = (
        "reward_pool_sats",
        "reward_distributed_sats",
        "average_daily_unique_reward_recipients",
        "reward_sats_per_recipient_day",
    )
    for index, week in enumerate(weeks):
        previous = weeks[index - 1] if index else None
        week["week_over_week_pct"] = {
            metric: pct_change(week[metric], previous[metric]) if previous else None
            for metric in metrics
        }
        trailing = weeks[max(0, index - 4):index]
        trailing_averages: dict[str, float | None] = {}
        for metric in metrics:
            values = [item[metric] for item in trailing if item[metric] is not None]
            trailing_averages[metric] = statistics.mean(values) if values else None
        week["versus_prior_4_week_average_pct"] = {
            metric: pct_change(week[metric], trailing_averages[metric])
            for metric in metrics
        }
        prior = weeks[:index]
        week["records"] = {
            "reward_pool_sats_all_time_high": bool(prior) and week["reward_pool_sats"] > max(
                item["reward_pool_sats"] for item in prior
            ),
            "reward_distributed_sats_all_time_high": bool(prior) and week[
                "reward_distributed_sats"
            ] > max(item["reward_distributed_sats"] for item in prior),
        }


def build_weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date = {date.fromisoformat(row["reward_date"]): row for row in rows}
    if not by_date:
        return []
    first_monday = week_start(min(by_date))
    last_day = max(by_date)
    weeks: list[dict[str, Any]] = []
    cursor = first_monday
    while cursor + timedelta(days=6) <= last_day:
        dates = [cursor + timedelta(days=offset) for offset in range(7)]
        if all(day in by_date for day in dates):
            weeks.append(aggregate_week([by_date[day] for day in dates]))
        cursor += timedelta(days=7)
    add_comparisons(weeks)
    return weeks


def main() -> int:
    daily = load_json(REWARDS_DAILY_PATH)
    definitions = load_json(DEFINITIONS_PATH)
    rows = daily.get("observations")
    if not isinstance(rows, list):
        raise RewardSummaryError("rewards_daily.json is missing its observations array")
    weeks = build_weekly(rows)
    if not weeks:
        raise RewardSummaryError("No complete Monday-Sunday reward week is available")

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    weekly_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "week_definition": "Monday through Sunday reward dates in America/Chicago",
        "date_semantics": (
            "Each reward week contains payouts made during the period; the funding "
            "activity occurred one day earlier, as shown in source_activity_period"
        ),
        "week_count": len(weeks),
        "weeks": weeks,
    }
    latest_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "latest_completed_reward_week": weeks[-1],
        "analysis_context": {
            "prior_completed_weeks_available": len(weeks) - 1,
            "reward_caveats": definitions.get("reward_caveats", []),
            "suggested_questions": [
                "Were the reward pool and the amount distributed unusually large this week?",
                "Which paid-action types supplied most of the reward value?",
                "Did rewards grow through more recipients or more sats per recipient-day?",
                "Were rewards unusually concentrated in boosts or downzaps?",
            ],
        },
    }
    atomic_write_json(REWARDS_WEEKLY_PATH, weekly_payload)
    atomic_write_json(LATEST_REWARDS_PATH, latest_payload)
    print(
        f"Wrote {len(weeks)} completed reward weeks; "
        f"latest ends {weeks[-1]['period']['end']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RewardSummaryError as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
