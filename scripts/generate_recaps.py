#!/usr/bin/env python3
"""Generate deterministic, X-ready daily and Friday recap drafts."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DAILY_PATH = DATA_DIR / "daily.json"
REWARDS_PATH = DATA_DIR / "rewards_daily.json"
RECAPS_DIR = DATA_DIR / "recaps"
DAILY_ARCHIVE_DIR = RECAPS_DIR / "daily"
FRIDAY_ARCHIVE_DIR = RECAPS_DIR / "friday"
MAX_POST_CHARACTERS = 280

METHODOLOGY_TEXT = (
    "Methodology: SN public aggregates, America/Chicago dates. “Zaps” = ZAP + "
    "BOOST + DOWN_ZAP. Sats may include Cowboy Credits; not confirmed Lightning "
    "payments. “Daily unique zappers” covers ZAP users only and is not "
    "period-distinct. Rewards are custodial SN payouts."
)


class RecapError(RuntimeError):
    """Raised when recap inputs or output are invalid."""


class RecapSkip(RuntimeError):
    """Raised when a recap should be skipped without failing collection."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RecapError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecapError(f"Expected a JSON object in {path}")
    return payload


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text.rstrip())
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


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=False))


def observations(payload: dict[str, Any], source_name: str) -> list[dict[str, Any]]:
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise RecapError(f"{source_name} is missing its observations array")
    if not all(isinstance(row, dict) for row in rows):
        raise RecapError(f"{source_name} contains a non-object observation")
    return rows


def index_rows(
    rows: list[dict[str, Any]], key: str, source_name: str
) -> dict[date, dict[str, Any]]:
    indexed: dict[date, dict[str, Any]] = {}
    for row in rows:
        raw_day = row.get(key)
        if not isinstance(raw_day, str):
            raise RecapError(f"{source_name} observation is missing {key}")
        try:
            day = date.fromisoformat(raw_day)
        except ValueError as exc:
            raise RecapError(f"Invalid {key} in {source_name}: {raw_day}") from exc
        if day in indexed:
            raise RecapError(f"Duplicate {key} in {source_name}: {raw_day}")
        indexed[day] = row
    return indexed


def expected_days(start: date, end: date) -> list[date]:
    if end < start:
        raise RecapError("Recap period ends before it starts")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def select_complete_rows(
    indexed: dict[date, dict[str, Any]],
    start: date,
    end: date,
    source_name: str,
) -> list[dict[str, Any]]:
    days = expected_days(start, end)
    missing = [day.isoformat() for day in days if day not in indexed]
    if missing:
        raise RecapSkip(f"{source_name} is missing dates: {', '.join(missing)}")
    selected = [indexed[day] for day in days]
    unsafe = [
        day.isoformat()
        for day, row in zip(days, selected)
        if row.get("quality_status") != "ok" or row.get("quality_warnings")
    ]
    if unsafe:
        raise RecapSkip(f"{source_name} has quality warnings for: {', '.join(unsafe)}")
    return selected


def require_nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecapError(f"{label} must be numeric")
    if value < 0:
        raise RecapError(f"{label} cannot be negative")
    return float(value)


def nested_metric(row: dict[str, Any], group: str, metric: str) -> float:
    values = row.get(group)
    if not isinstance(values, dict):
        raise RecapError(f"Observation is missing {group}")
    return require_nonnegative_number(values.get(metric), f"{group}.{metric}")


def top_level_metric(row: dict[str, Any], metric: str) -> float:
    return require_nonnegative_number(row.get(metric), metric)


def aggregate_metrics(
    daily_rows: list[dict[str, Any]], reward_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    if not daily_rows:
        raise RecapError("Cannot aggregate an empty daily period")
    if len(reward_rows) != len(daily_rows):
        raise RecapError("Daily and reward periods must contain the same number of days")

    standard_zap_actions = sum(
        nested_metric(row, "actions_by_type", "ZAP") for row in daily_rows
    )
    boost_actions = sum(
        nested_metric(row, "actions_by_type", "BOOST") for row in daily_rows
    )
    downzap_actions = sum(
        nested_metric(row, "actions_by_type", "DOWN_ZAP") for row in daily_rows
    )
    standard_zap_sats = sum(
        nested_metric(row, "spending_sats_by_type", "ZAP") for row in daily_rows
    )
    boost_sats = sum(
        nested_metric(row, "spending_sats_by_type", "BOOST") for row in daily_rows
    )
    downzap_sats = sum(
        nested_metric(row, "spending_sats_by_type", "DOWN_ZAP") for row in daily_rows
    )
    daily_unique_zappers = [
        top_level_metric(row, "daily_unique_zappers") for row in daily_rows
    ]
    tracked_spending_sats = 0.0
    for row in daily_rows:
        spending = row.get("spending_sats_by_type")
        if not isinstance(spending, dict):
            raise RecapError("Observation is missing spending_sats_by_type")
        tracked_spending_sats += sum(
            require_nonnegative_number(value, f"spending_sats_by_type.{name}")
            for name, value in spending.items()
        )

    zaplike_actions = standard_zap_actions + boost_actions + downzap_actions
    zaplike_sats = standard_zap_sats + boost_sats + downzap_sats
    seconds_in_period = len(daily_rows) * 24 * 60 * 60
    reward_anomalies = sorted(
        {
            flag
            for row in reward_rows
            for flag in row.get("anomaly_flags", [])
            if isinstance(flag, str)
        }
    )

    return {
        "zaplike_actions": int(zaplike_actions),
        "zaplike_sats": int(zaplike_sats),
        "standard_zap_actions": int(standard_zap_actions),
        "standard_zap_sats": int(standard_zap_sats),
        "boost_actions": int(boost_actions),
        "boost_sats": int(boost_sats),
        "downzap_actions": int(downzap_actions),
        "downzap_sats": int(downzap_sats),
        "tracked_spend_actions": int(
            sum(top_level_metric(row, "tracked_paid_actions") for row in daily_rows)
        ),
        "tracked_spending_sats": int(tracked_spending_sats),
        "paid_item_creation_actions": int(
            sum(top_level_metric(row, "content_items_created") for row in daily_rows)
        ),
        "average_daily_unique_zappers": round(
            statistics.mean(daily_unique_zappers), 2
        ),
        "peak_daily_unique_zappers": int(max(daily_unique_zappers)),
        "reward_sats_distributed": int(
            sum(
                top_level_metric(row, "reward_distributed_sats")
                for row in reward_rows
            )
        ),
        "reward_recipient_days": int(
            sum(
                top_level_metric(row, "daily_unique_reward_recipients")
                for row in reward_rows
            )
        ),
        "seconds_per_zap": round(seconds_in_period / zaplike_actions, 2)
        if zaplike_actions
        else None,
        "reward_anomaly_flags": reward_anomalies,
    }


def compact_number(value: int) -> str:
    if value < 1_000_000:
        return f"{value:,}"
    rendered = f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".")
    return f"{rendered}M"


def day_label(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}"


def period_label(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.strftime('%B')} {start.day}–{end.day}"
    return f"{day_label(start)}–{day_label(end)}"


def cadence_line(seconds_per_zap: float | None) -> str:
    if seconds_per_zap is None:
        return "No zap activity recorded."
    rounded = max(1, round(seconds_per_zap))
    unit = "second" if rounded == 1 else "seconds"
    return f"One zap every {rounded} {unit}."


def validate_post_text(text: str) -> None:
    if len(text) > MAX_POST_CHARACTERS:
        raise RecapError(
            f"Recap is {len(text)} characters; limit is {MAX_POST_CHARACTERS}"
        )
    if "weekly unique" in text.lower():
        raise RecapError("Recap must not claim weekly distinct zappers")


def render_daily_post(day: date, metrics: dict[str, Any]) -> str:
    text = (
        "⚡ Stacker News Daily Recap ⚡\n"
        f"{day_label(day)}\n\n"
        f"⚡ {metrics['zaplike_actions']:,} zaps*\n"
        f"💰 {compact_number(metrics['zaplike_sats'])} sats zapped\n"
        f"💸 {metrics['tracked_spend_actions']:,} tracked spend actions\n"
        f"📝 {metrics['paid_item_creation_actions']:,} paid item creations\n"
        f"👥 {round(metrics['average_daily_unique_zappers']):,} daily unique zappers\n"
        f"🎁 {compact_number(metrics['reward_sats_distributed'])} reward sats distributed\n\n"
        f"{cadence_line(metrics['seconds_per_zap'])}\n\n"
        "*Includes boosts & downzaps."
    )
    validate_post_text(text)
    return text


def render_friday_post(start: date, end: date, metrics: dict[str, Any]) -> str:
    text = (
        "⚡ Stacker News Friday Recap ⚡\n"
        f"{period_label(start, end)}\n\n"
        f"⚡ {metrics['zaplike_actions']:,} zaps*\n"
        f"💰 {compact_number(metrics['zaplike_sats'])} sats zapped\n"
        f"💸 {metrics['tracked_spend_actions']:,} tracked spend actions\n"
        f"📝 {metrics['paid_item_creation_actions']:,} paid item creations\n"
        f"👥 {round(metrics['average_daily_unique_zappers']):,} avg. daily unique zappers\n"
        f"🎁 {compact_number(metrics['reward_sats_distributed'])} reward sats distributed\n\n"
        f"{cadence_line(metrics['seconds_per_zap'])}\n\n"
        "*Includes boosts & downzaps."
    )
    validate_post_text(text)
    return text


def build_recap(
    recap_type: str,
    start: date,
    end: date,
    daily_index: dict[date, dict[str, Any]],
    reward_index: dict[date, dict[str, Any]],
) -> dict[str, Any]:
    daily_rows = select_complete_rows(daily_index, start, end, "daily.json")
    reward_rows = select_complete_rows(
        reward_index, start, end, "rewards_daily.json"
    )
    metrics = aggregate_metrics(daily_rows, reward_rows)
    if recap_type == "daily":
        post_text = render_daily_post(end, metrics)
    elif recap_type == "friday":
        post_text = render_friday_post(start, end, metrics)
    else:
        raise RecapError(f"Unsupported recap type: {recap_type}")

    return {
        "schema_version": 1,
        "recap_type": recap_type,
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": len(daily_rows),
            "timezone": "America/Chicago",
        },
        "metrics": metrics,
        "post_text": post_text,
        "post_character_count": len(post_text),
        "quality_status": "review" if metrics["reward_anomaly_flags"] else "ok",
        "caveats": [
            "Zaps include ZAP, BOOST, and DOWN_ZAP action groups.",
            "Sats-denominated aggregates may include Cowboy Credits and do not prove Lightning settlement.",
            "Daily unique zappers covers ZAP users only and must not be described as period-level distinct users.",
            "Reward figures are custodial Stacker News payouts selected by reward_date.",
        ],
    }


def latest_friday(day: date) -> date:
    return day - timedelta(days=(day.weekday() - 4) % 7)


def write_recap(recap: dict[str, Any], archive_dir: Path, latest_stem: str) -> None:
    end = recap["period"]["end"]
    atomic_write_text(archive_dir / f"{end}.md", recap["post_text"])
    atomic_write_json(archive_dir / f"{end}.json", recap)
    atomic_write_text(RECAPS_DIR / f"{latest_stem}.md", recap["post_text"])
    atomic_write_json(RECAPS_DIR / f"{latest_stem}.json", recap)


def remove_latest_recap(latest_stem: str) -> None:
    for suffix in (".md", ".json"):
        (RECAPS_DIR / f"{latest_stem}{suffix}").unlink(missing_ok=True)


def main() -> int:
    daily_payload = load_json(DAILY_PATH)
    rewards_payload = load_json(REWARDS_PATH)
    daily_index = index_rows(
        observations(daily_payload, "daily.json"), "date", "daily.json"
    )
    reward_index = index_rows(
        observations(rewards_payload, "rewards_daily.json"),
        "reward_date",
        "rewards_daily.json",
    )
    if not daily_index:
        raise RecapError("daily.json has no observations")

    newest_day = max(daily_index)
    outcomes: list[str] = []

    try:
        daily_recap = build_recap(
            "daily", newest_day, newest_day, daily_index, reward_index
        )
    except RecapSkip as exc:
        remove_latest_recap("latest_daily")
        outcomes.append(f"Skipped daily recap: {exc}")
    else:
        write_recap(daily_recap, DAILY_ARCHIVE_DIR, "latest_daily")
        outcomes.append(f"Wrote daily recap for {newest_day.isoformat()}")

    friday_end = latest_friday(newest_day)
    friday_start = friday_end - timedelta(days=6)
    try:
        friday_recap = build_recap(
            "friday", friday_start, friday_end, daily_index, reward_index
        )
    except RecapSkip as exc:
        remove_latest_recap("latest_friday")
        outcomes.append(f"Skipped Friday recap: {exc}")
    else:
        write_recap(friday_recap, FRIDAY_ARCHIVE_DIR, "latest_friday")
        outcomes.append(
            f"Wrote Friday recap for {friday_start.isoformat()} through "
            f"{friday_end.isoformat()}"
        )

    atomic_write_text(RECAPS_DIR / "methodology.md", METHODOLOGY_TEXT)
    for outcome in outcomes:
        print(outcome)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecapError as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
