#!/usr/bin/env python3
"""Collect load-safe hourly Nostr zap aggregates from zap.observer."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "nostr" / "zap_observer"
DAILY_PATH = DATA_DIR / "daily.json"
ROLLING_PATH = DATA_DIR / "latest_30_days.json"
DEFINITIONS_PATH = DATA_DIR / "metric_definitions.json"
RUN_PATH = DATA_DIR / "runs" / "latest.json"
RAW_DIR = DATA_DIR / "raw"

API_ORIGIN = "https://zap.observer"
STATS_PATH = "/api/stats"
RANGE = "30d"
SHOW_FILTERS = ("notes", "episodes", "tracks", "streams")
REQUEST_BUDGET = 5
MAX_RESPONSE_BYTES = 2_000_000
TIMEOUT_SECONDS = 30
USER_AGENT = (
    "SNPaymentStats-ZapObserverCollector/1.0 "
    "(+https://github.com/BScoresby/sn-payment-stats)"
)

GLOBAL_CAVEATS = [
    "These figures describe NIP-57 zap receipts accepted by zap.observer from its public-relay coverage, not every zap on Nostr.",
    "A NIP-57 zap receipt is not independent proof that its Lightning invoice was real or paid.",
    "zap.observer excludes self-zaps, so this series omits the closest Nostr analogue to a Stacker News boost.",
    "The monitored relay set, rejection counts, exact sats cap, and complete validation implementation are not exposed by the API.",
    "The category filters are provider-defined and are not assumed to be mutually exclusive or collectively exhaustive.",
]


class CollectionError(RuntimeError):
    """Raised when provider data cannot safely update observations."""


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


def stats_url(show: str | None = None) -> str:
    query = {"range": RANGE}
    if show is not None:
        if show not in SHOW_FILTERS:
            raise CollectionError(f"Unsupported zap.observer category: {show}")
        query["show"] = show
    return f"{API_ORIGIN}{STATS_PATH}?{urllib.parse.urlencode(query)}"


def fetch_json(url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed = urllib.parse.urlsplit(url)
    if f"{parsed.scheme}://{parsed.netloc}" != API_ORIGIN or parsed.path != STATS_PATH:
        raise CollectionError(f"Refusing unexpected API URL: {url}")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read(MAX_RESPONSE_BYTES + 1)
            headers = response.headers
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise CollectionError("zap.observer rate-limited the collector (HTTP 429)") from exc
        if exc.code in {401, 403}:
            raise CollectionError(f"zap.observer denied the request (HTTP {exc.code})") from exc
        raise CollectionError(f"zap.observer returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CollectionError(f"zap.observer request failed: {exc}") from exc
    if status != 200:
        raise CollectionError(f"zap.observer returned unexpected HTTP {status}")
    if len(body) > MAX_RESPONSE_BYTES:
        raise CollectionError("zap.observer response exceeded the safety size limit")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError("zap.observer returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CollectionError("zap.observer response is not a JSON object")
    metadata = {
        "url": url,
        "http_status": status,
        "response_bytes": len(body),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "cache_control": headers.get("Cache-Control"),
        "date": headers.get("Date"),
    }
    return payload, metadata


def is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_stats(payload: dict[str, Any], expected_show: str | None) -> None:
    if payload.get("range") != RANGE:
        raise CollectionError(f"Expected range={RANGE}, received {payload.get('range')!r}")
    actual_show = payload.get("show")
    if actual_show != expected_show:
        raise CollectionError(
            f"Expected show={expected_show!r}, received {actual_show!r}"
        )
    if not is_nonnegative_int(payload.get("since")):
        raise CollectionError("zap.observer response has an invalid since value")
    if not is_nonnegative_int(payload.get("count")):
        raise CollectionError("zap.observer response has an invalid count")
    if not is_nonnegative_int(payload.get("sats")):
        raise CollectionError("zap.observer response has an invalid sats total")
    hourly = payload.get("hourly")
    if not isinstance(hourly, list) or len(hourly) > 24 * 31:
        raise CollectionError("zap.observer response has an invalid hourly series")
    seen_hours: set[int] = set()
    for bucket in hourly:
        if not isinstance(bucket, dict):
            raise CollectionError("zap.observer returned a malformed hourly bucket")
        hour = bucket.get("hour")
        if not is_nonnegative_int(hour) or hour % 3600:
            raise CollectionError("zap.observer returned a misaligned hourly timestamp")
        if hour in seen_hours:
            raise CollectionError("zap.observer returned a duplicate hourly timestamp")
        seen_hours.add(hour)
        if not is_nonnegative_int(bucket.get("count")):
            raise CollectionError("zap.observer returned an invalid hourly count")
        if not is_nonnegative_int(bucket.get("sats")):
            raise CollectionError("zap.observer returned an invalid hourly sats total")
    if sum(bucket["count"] for bucket in hourly) != payload["count"]:
        raise CollectionError("zap.observer count does not equal its hourly series")
    if sum(bucket["sats"] for bucket in hourly) != payload["sats"]:
        raise CollectionError("zap.observer sats do not equal its hourly series")


def group_by_day(payload: dict[str, Any]) -> dict[date, dict[str, int]]:
    grouped: dict[date, dict[str, int]] = {}
    for bucket in payload["hourly"]:
        day = datetime.fromtimestamp(bucket["hour"], timezone.utc).date()
        row = grouped.setdefault(day, {"count": 0, "sats": 0, "hours": 0})
        row["count"] += bucket["count"]
        row["sats"] += bucket["sats"]
        row["hours"] += 1
    return grouped


def build_daily_rows(
    responses: dict[str, dict[str, Any]], today_utc: date
) -> tuple[list[dict[str, Any]], list[str]]:
    if set(responses) != {"all", *SHOW_FILTERS}:
        raise CollectionError("The Zap Observer response set is incomplete")
    total_days = group_by_day(responses["all"])
    completed_days = sorted(
        day for day, values in total_days.items()
        if day < today_utc and values["hours"] == 24
    )
    if len(completed_days) < 28:
        raise CollectionError(
            f"Only {len(completed_days)} complete UTC days were available; expected at least 28"
        )
    warnings: list[str] = []
    incomplete_past_days = sorted(
        day for day, values in total_days.items()
        if day < today_utc and values["hours"] != 24
    )
    if incomplete_past_days:
        warnings.append(
            "Excluded partial UTC day(s): "
            + ", ".join(day.isoformat() for day in incomplete_past_days)
        )
    grouped_categories = {
        name: group_by_day(responses[name]) for name in SHOW_FILTERS
    }
    rows = []
    for day in completed_days:
        total = total_days[day]
        row: dict[str, Any] = {
            "date_utc": day.isoformat(),
            "zap_receipt_count": total["count"],
            "total_zap_sats": total["sats"],
        }
        for name in SHOW_FILTERS:
            singular = name.removesuffix("s")
            values = grouped_categories[name].get(day, {"count": 0, "sats": 0})
            row[f"{singular}_zap_receipt_count"] = values["count"]
            row[f"{singular}_zap_sats"] = values["sats"]
        row["quality_status"] = "ok"
        row["quality_warnings"] = []
        rows.append(row)
    return rows, warnings


def merge_daily(
    existing: dict[str, Any] | None,
    new_rows: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    by_day: dict[str, dict[str, Any]] = {}
    if existing is not None:
        observations = existing.get("observations")
        if not isinstance(observations, list):
            raise CollectionError("Existing Zap Observer daily data is malformed")
        for row in observations:
            if not isinstance(row, dict) or not isinstance(row.get("date_utc"), str):
                raise CollectionError("Existing Zap Observer observation is malformed")
            by_day[row["date_utc"]] = row
    for row in new_rows:
        by_day[row["date_utc"]] = row
    observations = [by_day[key] for key in sorted(by_day)]
    return {
        "schema_version": 1,
        "source": "https://zap.observer/api/stats",
        "source_attribution": "zap.observer",
        "bucket_timezone": "UTC",
        "updated_at": generated_at,
        "observation_count": len(observations),
        "observations": observations,
        "global_caveats": GLOBAL_CAVEATS,
    }


def build_rolling(daily: dict[str, Any], generated_at: str) -> dict[str, Any]:
    observations = daily["observations"]
    if not observations:
        raise CollectionError("No Zap Observer daily observations are available")
    newest = date.fromisoformat(observations[-1]["date_utc"])
    start = newest - timedelta(days=29)
    by_day = {date.fromisoformat(row["date_utc"]): row for row in observations}
    expected = [start + timedelta(days=offset) for offset in range(30)]
    rows = [by_day[day] for day in expected if day in by_day]
    missing = [day.isoformat() for day in expected if day not in by_day]
    numeric_fields = [
        "zap_receipt_count", "total_zap_sats",
        "note_zap_receipt_count", "note_zap_sats",
        "episode_zap_receipt_count", "episode_zap_sats",
        "track_zap_receipt_count", "track_zap_sats",
        "stream_zap_receipt_count", "stream_zap_sats",
    ]
    metrics = {field: sum(row[field] for row in rows) for field in numeric_fields}
    metrics["average_zap_sats"] = (
        round(metrics["total_zap_sats"] / metrics["zap_receipt_count"], 3)
        if metrics["zap_receipt_count"] else None
    )
    warnings = list(GLOBAL_CAVEATS)
    if missing:
        warnings.insert(0, f"The rolling period is missing {len(missing)} UTC day(s).")
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "source": "zap.observer",
        "period": {
            "start": start.isoformat(),
            "end": newest.isoformat(),
            "days": 30,
            "days_present": len(rows),
            "missing_days": missing,
        },
        "metrics": metrics,
        "collection_status": {
            "quality_status": "warning" if missing else "ok",
            "quality_warnings": warnings,
        },
        "methodology": {
            "provider_description": "NIP-57 receipts accepted and hourly aggregated by zap.observer",
            "daily_aggregation": "Sum of 24 provider hourly buckets in each completed UTC day",
            "self_zaps_included": False,
            "categories_additive": False,
            "independent_settlement_proof": False,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and validate without changing repository data.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = datetime.now(timezone.utc)
    generated_at = iso_z(started)
    request_metadata: list[dict[str, Any]] = []
    responses: dict[str, dict[str, Any]] = {}
    try:
        for index, show in enumerate((None, *SHOW_FILTERS)):
            if index >= REQUEST_BUDGET:
                raise CollectionError("Zap Observer request budget exhausted")
            payload, metadata = fetch_json(stats_url(show))
            validate_stats(payload, show)
            key = show or "all"
            responses[key] = payload
            request_metadata.append({"dataset": key, **metadata})
            if index + 1 < REQUEST_BUDGET:
                time.sleep(1)

        rows, warnings = build_daily_rows(responses, started.date())
        existing = load_json(DAILY_PATH) if DAILY_PATH.exists() else None
        daily = merge_daily(existing, rows, generated_at)
        rolling = build_rolling(daily, generated_at)
        completed_at = datetime.now(timezone.utc)
        run_report = {
            "schema_version": 1,
            "provider": "zap.observer",
            "started_at": generated_at,
            "completed_at": iso_z(completed_at),
            "status": "success",
            "request_count": len(request_metadata),
            "request_budget": REQUEST_BUDGET,
            "requests": request_metadata,
            "days_updated": len(rows),
            "first_updated_day": rows[0]["date_utc"],
            "last_updated_day": rows[-1]["date_utc"],
            "quality_warnings": warnings,
        }
        if args.dry_run:
            print(json.dumps({
                "dry_run": True,
                "days_available": len(rows),
                "first_day": rows[0]["date_utc"],
                "last_day": rows[-1]["date_utc"],
                "requests": len(request_metadata),
                "rolling_metrics": rolling["metrics"],
            }, indent=2))
            return 0

        raw_name = started.strftime("%Y-%m-%dT%H-%M-%SZ.json.gz")
        atomic_write_gzip_json(
            RAW_DIR / raw_name,
            {
                "schema_version": 1,
                "fetched_at": generated_at,
                "responses": responses,
                "request_metadata": request_metadata,
            },
        )
        atomic_write_json(DAILY_PATH, daily)
        atomic_write_json(ROLLING_PATH, rolling)
        atomic_write_json(RUN_PATH, run_report)
        print(json.dumps({
            "daily_path": str(DAILY_PATH),
            "rolling_path": str(ROLLING_PATH),
            "days_updated": len(rows),
            "latest_day": rows[-1]["date_utc"],
            "request_count": len(request_metadata),
        }, indent=2))
        return 0
    except CollectionError as exc:
        if not args.dry_run:
            atomic_write_json(
                RUN_PATH,
                {
                    "schema_version": 1,
                    "provider": "zap.observer",
                    "started_at": generated_at,
                    "completed_at": iso_z(datetime.now(timezone.utc)),
                    "status": "failed",
                    "request_count": len(request_metadata),
                    "request_budget": REQUEST_BUDGET,
                    "requests": request_metadata,
                    "error": str(exc),
                },
            )
        raise SystemExit(f"Zap Observer collection failed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
