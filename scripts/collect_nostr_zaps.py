#!/usr/bin/env python3
"""Collect and validate NIP-57 zap receipts from approved public Nostr relays."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import ipaddress
import json
import os
import statistics
import tempfile
import time as monotonic_time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

try:
    from bolt11 import decode as decode_bolt11
    from nostr_sdk import Event
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
except ImportError as exc:  # pragma: no cover - exercised by the command-line guard
    raise SystemExit(
        "Nostr dependencies are missing. Run: pip install -r requirements-nostr.txt"
    ) from exc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOSTR_DIR = PROJECT_ROOT / "data" / "nostr"
EVENTS_DIR = NOSTR_DIR / "events"
RUNS_DIR = NOSTR_DIR / "runs"
CONFIG_PATH = NOSTR_DIR / "relay_config.json"
KIND_ZAP_REQUEST = 9734
KIND_ZAP_RECEIPT = 9735
USER_AGENT = "SNPaymentStats-NostrCollector/2.0"
COMPLETE_QUERY_STATUSES = {"complete_eose", "complete_but_zero"}


class CollectionError(RuntimeError):
    """Raised when collection cannot safely produce a result."""


@dataclass
class QueryResult:
    status: str
    events: dict[str, dict[str, Any]]
    duration_ms: int
    close_reason: str | None = None
    notices: list[str] = field(default_factory=list)
    auth_challenge: bool = False

    @property
    def complete(self) -> bool:
        return self.status in COMPLETE_QUERY_STATUSES


@dataclass
class RelayResult:
    relay: str
    label: str
    status: str
    events: dict[str, dict[str, Any]]
    windows: list[dict[str, Any]]
    requests: int
    duration_ms: int
    partial_event_count: int = 0
    close_reason: str | None = None
    error: str | None = None
    notices: list[str] = field(default_factory=list)
    nip11: dict[str, Any] | None = None

    @property
    def complete(self) -> bool:
        return self.status == "complete"


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
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CollectionError(f"Expected a JSON object in {path}")
    return value


def load_facts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"Cannot read {path}: {exc}") from exc
    facts = payload.get("receipts") if isinstance(payload, dict) else None
    if not isinstance(facts, list):
        raise CollectionError(f"Malformed receipt archive: {path}")
    return facts


def canonical_relay_url(value: str, require_secure: bool = False) -> str | None:
    """Return a safe canonical relay URL; never resolves or contacts it."""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    allowed_schemes = {"wss"} if require_secure else {"ws", "wss"}
    try:
        hostname_value = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in allowed_schemes or not hostname_value:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    hostname = hostname_value.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".local"):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    ):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port:
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host, path, "", ""))


def normalize_relay_entries(
    config: dict[str, Any], overrides: list[str] | None
) -> list[dict[str, str]]:
    source: list[Any]
    if overrides:
        source = [{"url": value, "label": value} for value in overrides]
    else:
        source = config.get("relays", [])
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in source:
        if isinstance(item, str):
            item = {"url": item, "label": item}
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        url = canonical_relay_url(str(item.get("url", "")))
        if url is None or url in seen:
            raise CollectionError(
                f"Invalid or duplicate configured relay URL: {item.get('url')!r}"
            )
        seen.add(url)
        entries.append(
            {
                "url": url,
                "label": str(item.get("label") or url),
                "rationale": str(item.get("rationale") or ""),
            }
        )
    if not entries:
        raise CollectionError("No approved relays are configured")
    return entries


def tag_values(event: dict[str, Any], name: str) -> list[str]:
    values: list[str] = []
    for tag in event.get("tags", []):
        if (
            isinstance(tag, list)
            and len(tag) >= 2
            and tag[0] == name
            and isinstance(tag[1], str)
        ):
            values.append(tag[1])
    return values


def one_tag(event: dict[str, Any], name: str, required: bool = False) -> str | None:
    values = tag_values(event, name)
    if required and len(values) != 1:
        raise CollectionError(f"expected exactly one {name} tag")
    if len(values) > 1:
        raise CollectionError(f"duplicate {name} tags")
    return values[0] if values else None


def verify_event_json(event: dict[str, Any], expected_kind: int) -> None:
    if event.get("kind") != expected_kind:
        raise CollectionError(f"unexpected event kind {event.get('kind')}")
    try:
        parsed = Event.from_json(json.dumps(event, separators=(",", ":")))
    except Exception as exc:
        raise CollectionError(f"invalid Nostr event encoding: {exc}") from exc
    if not parsed.verify():
        raise CollectionError("invalid Nostr event ID or signature")


def parse_zap_receipt(
    receipt: dict[str, Any],
    relays: list[str],
    invoice_decoder: Callable[[str], Any] = decode_bolt11,
) -> dict[str, Any]:
    """Return a compact fact after NIP-57 structural and cryptographic checks."""
    verify_event_json(receipt, KIND_ZAP_RECEIPT)
    bolt11_text = one_tag(receipt, "bolt11", required=True)
    description = one_tag(receipt, "description", required=True)
    recipient = one_tag(receipt, "p", required=True)
    request_event_id = one_tag(receipt, "e")
    address = one_tag(receipt, "a")
    preimage = one_tag(receipt, "preimage")
    assert bolt11_text is not None and description is not None and recipient is not None

    try:
        request = json.loads(description)
    except json.JSONDecodeError as exc:
        raise CollectionError("description is not a JSON zap request") from exc
    if not isinstance(request, dict):
        raise CollectionError("description is not a JSON object")
    verify_event_json(request, KIND_ZAP_REQUEST)

    request_recipient = one_tag(request, "p", required=True)
    relay_tags = [
        tag
        for tag in request.get("tags", [])
        if isinstance(tag, list) and tag and tag[0] == "relays"
    ]
    if len(relay_tags) != 1 or len(relay_tags[0]) < 2:
        raise CollectionError("zap request must contain one non-empty relays tag")
    requested_relays = sorted(
        {
            canonical
            for raw in relay_tags[0][1:]
            if isinstance(raw, str)
            if (canonical := canonical_relay_url(raw, require_secure=True)) is not None
        }
    )
    if request_recipient != recipient:
        raise CollectionError("receipt p tag does not match zap request")
    request_event = one_tag(request, "e")
    request_address = one_tag(request, "a")
    if request_event != request_event_id or request_address != address:
        raise CollectionError("receipt target does not match zap request")
    sender_tag = one_tag(receipt, "P")
    if sender_tag is not None and sender_tag != request.get("pubkey"):
        raise CollectionError("receipt P tag does not match zap request signer")

    try:
        invoice = invoice_decoder(bolt11_text)
    except Exception as exc:
        raise CollectionError(f"invalid BOLT11 invoice: {exc}") from exc
    amount_msat_raw = getattr(invoice, "amount_msat", None)
    if amount_msat_raw is None:
        raise CollectionError("BOLT11 invoice has no amount")
    amount_msat = int(amount_msat_raw)
    if amount_msat <= 0:
        raise CollectionError("BOLT11 invoice amount is not positive")

    description_hash = getattr(invoice, "description_hash", None)
    actual_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()
    if not isinstance(description_hash, str) or description_hash.lower() != actual_hash:
        raise CollectionError("BOLT11 description hash does not match zap request")

    request_amount = one_tag(request, "amount")
    if request_amount is not None:
        try:
            if int(request_amount) != amount_msat:
                raise CollectionError("BOLT11 amount does not match zap request amount")
        except ValueError as exc:
            raise CollectionError("zap request amount is not an integer") from exc

    payment_hash = getattr(invoice, "payment_hash", None)
    if preimage is not None:
        try:
            computed_payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        except ValueError as exc:
            raise CollectionError("preimage is not valid hexadecimal") from exc
        if not isinstance(payment_hash, str) or computed_payment_hash != payment_hash.lower():
            raise CollectionError("preimage does not match BOLT11 payment hash")

    return {
        "id": receipt["id"],
        "created_at": int(receipt["created_at"]),
        "amount_msat": amount_msat,
        "sender_pubkey": request["pubkey"],
        "recipient_pubkey": recipient,
        "receipt_pubkey": receipt["pubkey"],
        "target_event_id": request_event_id,
        "target_address": address,
        "preimage_present": preimage is not None,
        "observed_on": sorted(set(relays)),
        "requested_relays": requested_relays[:50],
        "requested_relay_count": len(relay_tags[0]) - 1,
    }


def classify_closed(reason: str) -> str:
    prefix = reason.partition(":")[0].strip().lower()
    return {
        "rate-limited": "rate_limited",
        "blocked": "blocked",
        "restricted": "restricted",
        "auth-required": "authentication_required",
        "error": "server_error",
        "unsupported": "unsupported_query",
    }.get(prefix, "server_closed")


async def safe_close_subscription(websocket: Any, subscription_id: str) -> None:
    try:
        await websocket.send(
            json.dumps(["CLOSE", subscription_id], separators=(",", ":"))
        )
    except Exception:
        pass


async def query_window(
    websocket: Any,
    start_ts: int,
    end_ts: int,
    request_limit: int,
    timeout_seconds: int,
) -> QueryResult:
    """Issue one NIP-01 REQ and require an explicit matching EOSE."""
    subscription_id = f"zaps-{uuid.uuid4().hex[:12]}"
    query_filter = {
        "kinds": [KIND_ZAP_RECEIPT],
        "since": start_ts,
        "until": end_ts - 1,
        "limit": request_limit,
    }
    started = monotonic_time.monotonic()
    await websocket.send(
        json.dumps(["REQ", subscription_id, query_filter], separators=(",", ":"))
    )
    events: dict[str, dict[str, Any]] = {}
    notices: list[str] = []
    auth_challenge = False
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                raw = await websocket.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                message = json.loads(raw)
                if not isinstance(message, list) or not message:
                    raise CollectionError("relay sent a malformed protocol message")
                message_type = message[0]
                if (
                    message_type == "EVENT"
                    and len(message) >= 3
                    and message[1] == subscription_id
                ):
                    event = message[2]
                    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                        raise CollectionError("relay sent a malformed EVENT")
                    events[event["id"]] = event
                    if len(events) > request_limit:
                        await safe_close_subscription(websocket, subscription_id)
                        return QueryResult(
                            "relay_exceeded_limit",
                            dict(list(events.items())[:request_limit]),
                            round((monotonic_time.monotonic() - started) * 1000),
                            notices=notices,
                            auth_challenge=auth_challenge,
                        )
                elif (
                    message_type == "EOSE"
                    and len(message) >= 2
                    and message[1] == subscription_id
                ):
                    await safe_close_subscription(websocket, subscription_id)
                    if len(events) >= request_limit:
                        status = "result_limit_reached"
                    elif events:
                        status = "complete_eose"
                    else:
                        status = "complete_but_zero"
                    return QueryResult(
                        status,
                        events,
                        round((monotonic_time.monotonic() - started) * 1000),
                        notices=notices,
                        auth_challenge=auth_challenge,
                    )
                elif (
                    message_type == "CLOSED"
                    and len(message) >= 2
                    and message[1] == subscription_id
                ):
                    reason = str(message[2]) if len(message) >= 3 else ""
                    return QueryResult(
                        classify_closed(reason),
                        events,
                        round((monotonic_time.monotonic() - started) * 1000),
                        close_reason=reason,
                        notices=notices,
                        auth_challenge=auth_challenge,
                    )
                elif message_type == "NOTICE" and len(message) >= 2:
                    notices.append(str(message[1])[:500])
                    notices = notices[-10:]
                elif message_type == "AUTH":
                    auth_challenge = True
    except TimeoutError:
        await safe_close_subscription(websocket, subscription_id)
        status = "authentication_required" if auth_challenge else "query_timeout"
        return QueryResult(
            status,
            events,
            round((monotonic_time.monotonic() - started) * 1000),
            notices=notices,
            auth_challenge=auth_challenge,
        )
    except ConnectionClosed as exc:
        return QueryResult(
            "connection_closed",
            events,
            round((monotonic_time.monotonic() - started) * 1000),
            close_reason=str(exc)[:500],
            notices=notices,
            auth_challenge=auth_challenge,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, CollectionError) as exc:
        await safe_close_subscription(websocket, subscription_id)
        return QueryResult(
            "protocol_error",
            events,
            round((monotonic_time.monotonic() - started) * 1000),
            close_reason=str(exc)[:500],
            notices=notices,
            auth_challenge=auth_challenge,
        )


def make_windows(days: list[date], window_hours: int) -> list[tuple[date, int, int]]:
    if window_hours < 1 or 24 % window_hours:
        raise CollectionError("window_hours must be a positive divisor of 24")
    windows: list[tuple[date, int, int]] = []
    width = window_hours * 3600
    for day in days:
        day_start = int(
            datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp()
        )
        for offset in range(0, 86400, width):
            windows.append((day, day_start + offset, day_start + offset + width))
    return windows


def fetch_nip11(relay_url: str, timeout_seconds: int = 10) -> dict[str, Any]:
    parsed = urlsplit(relay_url)
    http_url = urlunsplit(
        ("https" if parsed.scheme == "wss" else "http", parsed.netloc, parsed.path, "", "")
    )
    request = urllib.request.Request(
        http_url,
        headers={"Accept": "application/nostr+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(512_001)
            if len(raw) > 512_000:
                return {"status": "error", "error": "NIP-11 response exceeds 512 KB"}
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("response is not an object")
            return {
                "status": "ok",
                "name": payload.get("name"),
                "description": payload.get("description"),
                "software": payload.get("software"),
                "version": payload.get("version"),
                "supported_nips": payload.get("supported_nips", []),
                "limitation": payload.get("limitation"),
                "terms_of_service": payload.get("terms_of_service"),
            }
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]}


def classify_connection_exception(exc: Exception) -> tuple[str, str]:
    status_code = None
    if isinstance(exc, InvalidStatus):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 429:
        return "rate_limited", f"HTTP {status_code}"
    if status_code in {401, 407}:
        return "authentication_required", f"HTTP {status_code}"
    if status_code == 403:
        return "blocked", f"HTTP {status_code}"
    return "connection_failed", f"{type(exc).__name__}: {exc}"[:500]


async def collect_from_relay(
    relay_entry: dict[str, str],
    days: list[date],
    query_config: dict[str, Any],
    request_budget: int,
) -> RelayResult:
    relay_url = relay_entry["url"]
    label = relay_entry["label"]
    windows = make_windows(days, int(query_config["window_hours"]))
    nip11 = await asyncio.to_thread(
        fetch_nip11, relay_url, int(query_config["nip11_timeout_seconds"])
    )
    if len(windows) > request_budget:
        return RelayResult(
            relay_url,
            label,
            "request_budget_exceeded",
            {},
            [],
            0,
            0,
            error=f"{len(windows)} windows exceed the budget of {request_budget}",
            nip11=nip11,
        )

    started = monotonic_time.monotonic()
    attempts = int(query_config["connection_attempts"])
    for attempt in range(attempts):
        requests = 0
        window_reports: list[dict[str, Any]] = []
        events: dict[str, dict[str, Any]] = {}
        notices: list[str] = []
        try:
            async with connect(
                relay_url,
                open_timeout=int(query_config["connect_timeout_seconds"]),
                close_timeout=5,
                ping_interval=20,
                max_size=int(query_config["max_message_bytes"]),
                user_agent_header=USER_AGENT,
            ) as websocket:
                for index, (day, start_ts, end_ts) in enumerate(windows):
                    if requests >= request_budget:
                        return RelayResult(
                            relay_url,
                            label,
                            "request_budget_exceeded",
                            {},
                            window_reports,
                            requests,
                            round((monotonic_time.monotonic() - started) * 1000),
                            partial_event_count=len(events),
                            error=f"hard request budget {request_budget} reached",
                            notices=notices,
                            nip11=nip11,
                        )
                    result = await query_window(
                        websocket,
                        start_ts,
                        end_ts,
                        int(query_config["request_limit"]),
                        int(query_config["query_timeout_seconds"]),
                    )
                    requests += 1
                    notices.extend(result.notices)
                    notices = notices[-10:]
                    window_reports.append(
                        {
                            "date_utc": day.isoformat(),
                            "start": iso_z(
                                datetime.fromtimestamp(start_ts, timezone.utc)
                            ),
                            "end_exclusive": iso_z(
                                datetime.fromtimestamp(end_ts, timezone.utc)
                            ),
                            "status": result.status,
                            "event_count": len(result.events),
                            "duration_ms": result.duration_ms,
                            "close_reason": result.close_reason,
                            "auth_challenge": result.auth_challenge,
                        }
                    )
                    if not result.complete:
                        return RelayResult(
                            relay_url,
                            label,
                            result.status,
                            {},
                            window_reports,
                            requests,
                            round((monotonic_time.monotonic() - started) * 1000),
                            partial_event_count=len(events) + len(result.events),
                            close_reason=result.close_reason,
                            notices=notices,
                            nip11=nip11,
                        )
                    events.update(result.events)
                    if index + 1 < len(windows):
                        await asyncio.sleep(float(query_config["request_delay_seconds"]))
                return RelayResult(
                    relay_url,
                    label,
                    "complete",
                    events,
                    window_reports,
                    requests,
                    round((monotonic_time.monotonic() - started) * 1000),
                    notices=notices,
                    nip11=nip11,
                )
        except Exception as exc:
            status, detail = classify_connection_exception(exc)
            if requests or status in {
                "rate_limited",
                "blocked",
                "authentication_required",
            }:
                return RelayResult(
                    relay_url,
                    label,
                    status,
                    {},
                    window_reports,
                    requests,
                    round((monotonic_time.monotonic() - started) * 1000),
                    partial_event_count=len(events),
                    error=detail,
                    notices=notices,
                    nip11=nip11,
                )
            if attempt + 1 < attempts:
                await asyncio.sleep(2**attempt)
            else:
                return RelayResult(
                    relay_url,
                    label,
                    status,
                    {},
                    window_reports,
                    requests,
                    round((monotonic_time.monotonic() - started) * 1000),
                    error=detail,
                    nip11=nip11,
                )
    raise AssertionError("connection attempt loop ended unexpectedly")


def merge_and_write_days(
    facts: list[dict[str, Any]],
    days: list[date],
    collected_at: str,
    complete_relays: list[str],
    configured_relay_count: int,
) -> dict[str, int]:
    new_by_day: dict[str, dict[str, dict[str, Any]]] = {
        day.isoformat(): {} for day in days
    }
    for fact in facts:
        day = datetime.fromtimestamp(fact["created_at"], timezone.utc).date().isoformat()
        if day in new_by_day:
            new_by_day[day][fact["id"]] = fact

    counts: dict[str, int] = {}
    for day in days:
        day_text = day.isoformat()
        path = EVENTS_DIR / f"{day_text}.json.gz"
        merged = {fact["id"]: fact for fact in load_facts(path)}
        for event_id, new_fact in new_by_day[day_text].items():
            if event_id in merged:
                new_fact["observed_on"] = sorted(
                    set(merged[event_id].get("observed_on", []))
                    | set(new_fact.get("observed_on", []))
                )
            merged[event_id] = new_fact
        ordered = sorted(
            merged.values(), key=lambda item: (item["created_at"], item["id"])
        )
        atomic_write_gzip_json(
            path,
            {
                "schema_version": 2,
                "date_utc": day_text,
                "updated_at": collected_at,
                "receipt_count": len(ordered),
                "coverage_at_update": {
                    "complete_relays": complete_relays,
                    "complete_relay_count": len(complete_relays),
                    "configured_relay_count": configured_relay_count,
                },
                "receipts": ordered,
            },
        )
        counts[day_text] = len(ordered)
    return counts


def relay_metrics(
    result: RelayResult,
    valid_by_id: dict[str, dict[str, Any]],
    sources_by_id: dict[str, set[str]],
    valid_union_count: int,
) -> dict[str, Any]:
    valid_ids = set(result.events) & set(valid_by_id)
    unique_ids = {
        event_id
        for event_id in valid_ids
        if sources_by_id.get(event_id) == {result.relay}
    }
    amounts = [valid_by_id[event_id]["amount_msat"] for event_id in valid_ids]
    timestamps = [valid_by_id[event_id]["created_at"] for event_id in valid_ids]
    return {
        "valid_receipts": len(valid_ids),
        "invalid_receipts": len(result.events) - len(valid_ids),
        "unique_contribution": len(unique_ids),
        "coverage_of_valid_union_pct": (
            round(len(valid_ids) / valid_union_count * 100, 2)
            if valid_union_count
            else None
        ),
        "median_zap_sats": (
            round(statistics.median(amounts) / 1000, 3) if amounts else None
        ),
        "oldest_valid_receipt": (
            iso_z(datetime.fromtimestamp(min(timestamps), timezone.utc))
            if timestamps
            else None
        ),
        "newest_valid_receipt": (
            iso_z(datetime.fromtimestamp(max(timestamps), timezone.utc))
            if timestamps
            else None
        ),
    }


def write_run_report(report: dict[str, Any]) -> None:
    stamp = str(report["collected_at"]).replace(":", "-")
    atomic_write_json(RUNS_DIR / "latest.json", report)
    atomic_write_json(RUNS_DIR / f"{stamp}.json", report)


async def run(args: argparse.Namespace) -> int:
    config = load_json(args.config)
    relay_entries = normalize_relay_entries(config, args.relay)
    query_config = config.get("query", {})
    required_settings = {
        "initial_backfill_days",
        "daily_overlap_days",
        "maximum_days_per_run",
        "window_hours",
        "request_limit",
        "daily_request_budget_per_relay",
        "backfill_request_budget_per_relay",
        "request_delay_seconds",
        "query_timeout_seconds",
        "connect_timeout_seconds",
        "nip11_timeout_seconds",
        "connection_attempts",
        "minimum_complete_relays",
        "max_message_bytes",
    }
    if not required_settings.issubset(query_config):
        missing = sorted(required_settings - set(query_config))
        raise CollectionError(f"relay_config.json is missing query settings: {missing}")

    today_utc = (
        date.fromisoformat(args.today)
        if args.today
        else datetime.now(timezone.utc).date()
    )
    existing = any(EVENTS_DIR.glob("*.json.gz"))
    day_count = args.days or int(
        query_config["daily_overlap_days"]
        if existing
        else query_config["initial_backfill_days"]
    )
    maximum_days = int(query_config["maximum_days_per_run"])
    if day_count < 1 or day_count > maximum_days:
        raise CollectionError(f"--days must be between 1 and {maximum_days}")
    days = [today_utc - timedelta(days=offset) for offset in range(day_count, 0, -1)]
    is_backfill = not existing or day_count > int(query_config["daily_overlap_days"])
    request_budget = int(
        query_config[
            "backfill_request_budget_per_relay"
            if is_backfill
            else "daily_request_budget_per_relay"
        ]
    )

    results = await asyncio.gather(
        *(
            collect_from_relay(entry, days, query_config, request_budget)
            for entry in relay_entries
        )
    )
    complete = [result for result in results if result.complete]
    collected_at = iso_z(datetime.now(timezone.utc))

    raw_by_id: dict[str, dict[str, Any]] = {}
    sources_by_id: dict[str, set[str]] = {}
    for result in complete:
        for event_id, event in result.events.items():
            raw_by_id[event_id] = event
            sources_by_id.setdefault(event_id, set()).add(result.relay)

    valid_by_id: dict[str, dict[str, Any]] = {}
    invalid_reasons: Counter[str] = Counter()
    for event_id, event in raw_by_id.items():
        try:
            valid_by_id[event_id] = parse_zap_receipt(
                event, sorted(sources_by_id[event_id])
            )
        except CollectionError as exc:
            invalid_reasons[str(exc)] += 1

    minimum_complete = int(query_config["minimum_complete_relays"])
    coverage_ok = len(complete) >= minimum_complete
    relay_reports = []
    for result in results:
        report = {
            "url": result.relay,
            "label": result.label,
            "status": result.status,
            "complete": result.complete,
            "events_returned": len(result.events),
            "partial_events_discarded": result.partial_event_count,
            "requests": result.requests,
            "request_budget": request_budget,
            "duration_ms": result.duration_ms,
            "close_reason": result.close_reason,
            "error": result.error,
            "notices": result.notices,
            "nip11": result.nip11,
            "windows": result.windows,
        }
        report["qualification"] = relay_metrics(
            result, valid_by_id, sources_by_id, len(valid_by_id)
        )
        relay_reports.append(report)

    run_report = {
        "schema_version": 2,
        "collected_at": collected_at,
        "period": {
            "start": days[0].isoformat(),
            "end": days[-1].isoformat(),
            "days": day_count,
            "day_definition": "completed UTC days",
        },
        "query_policy": {
            "window_hours": int(query_config["window_hours"]),
            "request_limit": int(query_config["request_limit"]),
            "request_delay_seconds": float(query_config["request_delay_seconds"]),
            "request_budget_per_relay": request_budget,
            "explicit_eose_required": True,
        },
        "configured_relay_count": len(relay_entries),
        "complete_relay_count": len(complete),
        "minimum_complete_relays": minimum_complete,
        "coverage_gate_passed": coverage_ok,
        "relays": relay_reports,
        "deduplicated_events_received": len(raw_by_id),
        "valid_receipts_received": len(valid_by_id),
        "invalid_receipts_received": sum(invalid_reasons.values()),
        "invalid_reasons": dict(invalid_reasons.most_common()),
        "stored_receipts_by_day": {},
        "dry_run": args.dry_run,
        "quality_status": (
            "ok"
            if coverage_ok and len(complete) == len(relay_entries)
            else "warning"
        ),
        "quality_warnings": [
            "minimum_relay_coverage_not_met" if not coverage_ok else None,
            "relay_coverage_incomplete"
            if len(complete) < len(relay_entries)
            else None,
            "public_relay_sample_not_ecosystem_complete",
            "lnurl_provider_authorization_not_checked",
        ],
    }
    run_report["quality_warnings"] = [
        warning for warning in run_report["quality_warnings"] if warning
    ]

    if not coverage_ok:
        if not args.dry_run:
            write_run_report(run_report)
        print(json.dumps(run_report, indent=2))
        raise CollectionError(
            f"Only {len(complete)} relays completed every query window; "
            f"at least {minimum_complete} are required. Existing observations were not changed."
        )

    facts = list(valid_by_id.values())
    if not args.dry_run:
        run_report["stored_receipts_by_day"] = merge_and_write_days(
            facts,
            days,
            collected_at,
            sorted(result.relay for result in complete),
            len(relay_entries),
        )
        write_run_report(run_report)
    else:
        run_report["stored_receipts_by_day"] = dict(
            Counter(
                datetime.fromtimestamp(fact["created_at"], timezone.utc)
                .date()
                .isoformat()
                for fact in facts
            )
        )
    print(json.dumps(run_report, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        help="Completed UTC days to refresh (auto: 30 first run, then 3; maximum 30)",
    )
    parser.add_argument("--today", help="Override current UTC date for reproducible testing")
    parser.add_argument(
        "--relay", action="append", help="Approved relay URL; repeat to override config"
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--dry-run", action="store_true", help="Query and validate without writing files"
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
