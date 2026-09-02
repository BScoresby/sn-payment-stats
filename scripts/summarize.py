#!/usr/bin/env python3
"""Build completed Monday-Sunday summaries from data/daily.json."""

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
DAILY_PATH = DATA_DIR / "daily.json"
WEEKLY_PATH = DATA_DIR / "weekly.json"
LATEST_PATH = DATA_DIR / "latest_week.json"
DEFINITIONS_PATH = DATA_DIR / "metric_definitions.json"


class SummaryError(RuntimeError):
    """Raised when a safe summary cannot be created."""


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


def pct_change(current: float, previous: float) -> float | None:
    return round((current - previous) / previous * 100, 2) if previous else None


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SummaryError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SummaryError(f"Expected a JSON object in {path}")
    return payload


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def aggregate_week(rows: list[dict[str, Any]]) -> dict[str, Any]:
    start = date.fromisoformat(rows[0]["date"])
    end = date.fromisoformat(rows[-1]["date"])
    zap_actions = sum(row["zap_actions"] for row in rows)
    zap_sats = sum(row["zap_sats"] for row in rows)
    tracked_actions = sum(row["tracked_paid_actions"] for row in rows)
    content_items = sum(row["content_items_created"] for row in rows)
    daily_zappers = [row["daily_unique_zappers"] for row in rows]
    daily_spenders = [row["daily_unique_spenders"] for row in rows]
    warning_days = [row["date"] for row in rows if row.get("quality_status") != "ok"]

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": 7},
        "zap_actions": zap_actions,
        "zap_sats": zap_sats,
        "tracked_paid_actions": tracked_actions,
        "content_items_created": content_items,
        "zaps_per_day": ratio(zap_actions, 7),
        "seconds_per_zap": ratio(7 * 24 * 60 * 60, zap_actions),
        "average_zap_sats": ratio(zap_sats, zap_actions),
        "average_daily_unique_zappers": round(statistics.mean(daily_zappers), 2),
        "peak_daily_unique_zappers": max(daily_zappers),
        "average_daily_unique_spenders": round(statistics.mean(daily_spenders), 2),
        "zap_share_of_tracked_paid_actions_pct": ratio(zap_actions * 100, tracked_actions),
        "zaps_per_100_content_items": ratio(zap_actions * 100, content_items),
        "quality_status": "warning" if warning_days else "ok",
        "warning_days": warning_days,
    }


def add_comparisons(weeks: list[dict[str, Any]]) -> None:
    metrics = ("zap_actions", "zap_sats", "tracked_paid_actions", "content_items_created")
    for index, week in enumerate(weeks):
        previous = weeks[index - 1] if index else None
        week["week_over_week_pct"] = {
            metric: pct_change(week[metric], previous[metric]) if previous else None
            for metric in metrics
        }
        trailing = weeks[max(0, index - 4):index]
        week["versus_prior_4_week_average_pct"] = {
            metric: pct_change(
                week[metric], statistics.mean(item[metric] for item in trailing)
            ) if trailing else None
            for metric in metrics
        }
        prior = weeks[:index]
        week["records"] = {
            "zap_actions_all_time_high": bool(prior) and week["zap_actions"] > max(item["zap_actions"] for item in prior),
            "zap_sats_all_time_high": bool(prior) and week["zap_sats"] > max(item["zap_sats"] for item in prior),
        }


def build_weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date = {date.fromisoformat(row["date"]): row for row in rows}
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
    daily = load_json(DAILY_PATH)
    definitions = load_json(DEFINITIONS_PATH)
    rows = daily.get("observations")
    if not isinstance(rows, list):
        raise SummaryError("daily.json is missing its observations array")
    weeks = build_weekly(rows)
    if not weeks:
        raise SummaryError("No complete Monday-Sunday week is available")

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    weekly_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "week_definition": "Monday through Sunday in America/Chicago daily API buckets",
        "week_count": len(weeks),
        "weeks": weeks,
    }
    latest_payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "latest_completed_week": weeks[-1],
        "analysis_context": {
            "prior_completed_weeks_available": len(weeks) - 1,
            "global_caveats": definitions.get("global_caveats", []),
            "suggested_questions": [
                "Which metrics changed most from the previous week?",
                "Did activity change mainly through frequency, value, participation, or content volume?",
                "Is the latest week unusual relative to the prior four completed weeks?",
                "Which accurate statistic best illustrates frequent money-native interaction?"
            ],
        },
    }
    atomic_write_json(WEEKLY_PATH, weekly_payload)
    atomic_write_json(LATEST_PATH, latest_payload)
    print(f"Wrote {len(weeks)} completed weeks; latest ends {weeks[-1]['period']['end']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SummaryError as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)

