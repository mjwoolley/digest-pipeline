"""Podcast analytics aggregation from structured access logs.

This module keeps console reads fast by precomputing digest-local JSON artifacts:
- podcast_stats.json
- podcast_downloads.json

The intended source is structured Caddy access logs, but the parser is tolerant of
slightly different JSON layouts as long as request/response fields are present.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

SUBSCRIBER_WINDOW_DAYS = 7
DOWNLOAD_WINDOW_HOURS = 24
MIN_DOWNLOAD_BYTES = 64 * 1024
FEED_STATUSES = {200, 206, 304}
DOWNLOAD_STATUSES = {200, 206}
BOT_HINTS = (
    "bot",
    "crawler",
    "spider",
    "slurp",
    "facebookexternalhit",
    "monitor",
    "uptime",
    "curl/",
    "wget/",
)
APP_HINTS = [
    ("applecoremedia", "Apple Podcasts"),
    ("itunes", "Apple Podcasts"),
    ("apple podcasts", "Apple Podcasts"),
    ("overcast", "Overcast"),
    ("spotify", "Spotify"),
    ("pocket casts", "Pocket Casts"),
    ("pocketcasts", "Pocket Casts"),
    ("castro", "Castro"),
    ("podbean", "Podbean"),
    ("podcast addict", "Podcast Addict"),
    ("antennapod", "AntennaPod"),
    ("downcast", "Downcast"),
    ("rssradio", "RSSRadio"),
    ("googlepodcasts", "Google Podcasts"),
]


@dataclass
class RequestEvent:
    ts: datetime
    host: str
    uri: str
    method: str
    status: int
    remote_ip: str
    user_agent: str
    bytes_written: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return default


def _parse_timestamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s).astimezone(UTC)
        except Exception:
            pass
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except Exception:
            return None
    return None


def _first_header(headers: dict | None, key: str) -> str:
    if not isinstance(headers, dict):
        return ""
    for candidate in (key, key.lower(), key.title(), key.upper()):
        if candidate not in headers:
            continue
        value = headers[candidate]
        if isinstance(value, list):
            return str(value[0]) if value else ""
        return str(value)
    return ""


def _event_from_record(record: dict) -> RequestEvent | None:
    request = record.get("request") or {}
    headers = request.get("headers") or record.get("headers") or {}
    ts = _parse_timestamp(
        record.get("ts")
        or record.get("timestamp")
        or record.get("time")
        or request.get("time")
    )
    if ts is None:
        return None

    host = (
        request.get("host")
        or record.get("host")
        or record.get("server_name")
        or record.get("hostname")
        or ""
    )
    uri = (
        request.get("uri")
        or record.get("uri")
        or record.get("path")
        or request.get("path")
        or ""
    )
    method = (request.get("method") or record.get("method") or "GET").upper()
    status = _coerce_int(record.get("status") or (record.get("resp") or {}).get("status"))
    remote_ip = (
        request.get("remote_ip")
        or request.get("client_ip")
        or record.get("remote_ip")
        or record.get("remote_addr")
        or record.get("client_ip")
        or ""
    )
    user_agent = (
        _first_header(headers, "User-Agent")
        or record.get("user_agent")
        or record.get("user-agent")
        or ""
    )
    bytes_written = _coerce_int(
        record.get("size")
        or record.get("bytes_written")
        or record.get("bytes")
        or (record.get("resp") or {}).get("size")
        or (record.get("response") or {}).get("size")
    )

    if not host or not uri:
        return None
    return RequestEvent(
        ts=ts,
        host=str(host).lower(),
        uri=str(uri),
        method=method,
        status=status,
        remote_ip=str(remote_ip),
        user_agent=str(user_agent),
        bytes_written=bytes_written,
    )


def _iter_json_records(path: Path):
    try:
        with path.open() as f:
            first = f.read(1)
            if not first:
                return
            f.seek(0)
            if first == "[":
                payload = json.load(f)
                if isinstance(payload, list):
                    for item in payload:
                        if isinstance(item, dict):
                            yield item
                return
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
    except Exception:
        return


def _public_hosts(config: dict) -> set[str]:
    hosts = set()
    for value in (
        config.get("subscriptions", {}).get("public_base_url"),
        config.get("podcast", {}).get("pages_base_url"),
    ):
        if not value:
            continue
        parsed = urlparse(value)
        host = parsed.netloc or parsed.path
        host = host.strip().lower()
        if host:
            hosts.add(host)
    return hosts


def _normalize_ua(user_agent: str) -> str:
    ua = (user_agent or "").strip().lower()
    return " ".join(ua.split())


def _app_name(user_agent: str) -> str:
    ua = _normalize_ua(user_agent)
    for hint, name in APP_HINTS:
        if hint in ua:
            return name
    return "Other"


def _is_bot(user_agent: str) -> bool:
    ua = _normalize_ua(user_agent)
    return any(hint in ua for hint in BOT_HINTS)


def _client_key(event: RequestEvent) -> str:
    ua = _normalize_ua(event.user_agent) or "unknown"
    ip = event.remote_ip or "unknown"
    return f"{ip}|{ua}"


def _episode_date_from_uri(uri: str) -> str | None:
    path = uri.split("?", 1)[0]
    if not path.startswith("/podcasts/") or not path.endswith(".mp3"):
        return None
    stem = Path(path).stem
    if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
        return stem
    return None


def discover_log_paths(data_root: Path) -> list[Path]:
    """Find access-log files under ``data_root`` matching known patterns.

    Returns paths in deterministic order, deduplicated by resolved path so
    overlapping glob patterns don't double-count records.
    """
    patterns = [
        "logs/podcast_access*.jsonl",
        "logs/podcast_access*.json",
        "logs/podcast-access*.jsonl",
        "logs/podcast-access*.json",
        "logs/caddy-access*.jsonl",
        "logs/caddy-access*.json",
        "logs/access*.jsonl",
        "logs/access*.json",
        "podcast_stats_input/*.jsonl",
        "podcast_stats_input/*.json",
    ]
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(sorted(data_root.glob(pattern)))
    unique: list[Path] = []
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def compute_podcast_metrics(config: dict, data_root: Path, *, log_paths: list[Path] | None = None,
                            now: datetime | None = None) -> tuple[dict, dict]:
    """Aggregate access logs into ``(stats, downloads)`` dicts.

    ``stats`` summarizes recent feed-fetch activity (estimated subscribers,
    top podcast apps). ``downloads`` is keyed by episode date and counts
    distinct clients per episode. Bots are filtered by user-agent. Multiple
    fetches from the same client within ``DOWNLOAD_WINDOW_HOURS`` count once.
    """
    now = now or _utc_now()
    hosts = _public_hosts(config)
    log_paths = log_paths if log_paths is not None else discover_log_paths(data_root)

    subscriber_cutoff = now - timedelta(days=SUBSCRIBER_WINDOW_DAYS)
    download_cutoff = now - timedelta(days=SUBSCRIBER_WINDOW_DAYS)

    subscriber_keys: set[str] = set()
    subscriber_apps = Counter()
    episode_clients: dict[str, dict[str, datetime]] = {}
    episode_apps: dict[str, Counter] = {}
    coverage_start = None
    coverage_end = None
    records_seen = 0
    matched_records = 0

    for path in log_paths:
        for record in _iter_json_records(path):
            records_seen += 1
            event = _event_from_record(record)
            if event is None:
                continue
            if hosts and event.host not in hosts:
                continue
            coverage_start = event.ts if coverage_start is None else min(coverage_start, event.ts)
            coverage_end = event.ts if coverage_end is None else max(coverage_end, event.ts)
            if _is_bot(event.user_agent):
                continue

            uri_path = event.uri.split("?", 1)[0]
            app = _app_name(event.user_agent)

            if (
                uri_path == "/podcast.xml"
                and event.method != "HEAD"
                and event.status in FEED_STATUSES
                and event.ts >= subscriber_cutoff
            ):
                matched_records += 1
                day_key = event.ts.date().isoformat()
                subscriber_keys.add(f"{day_key}|{_client_key(event)}")
                subscriber_apps[app] += 1
                continue

            episode_date = _episode_date_from_uri(uri_path)
            if not episode_date:
                continue
            if event.method == "HEAD" or event.status not in DOWNLOAD_STATUSES:
                continue
            if event.bytes_written and event.bytes_written < MIN_DOWNLOAD_BYTES and event.status != 206:
                continue
            if event.ts < download_cutoff:
                continue

            matched_records += 1
            client = _client_key(event)
            bucket = episode_clients.setdefault(episode_date, {})
            previous = bucket.get(client)
            if previous and (event.ts - previous) < timedelta(hours=DOWNLOAD_WINDOW_HOURS):
                continue
            bucket[client] = event.ts
            episode_apps.setdefault(episode_date, Counter())[app] += 1

    stats = {
        "estimated_subscribers": len(subscriber_keys),
        "subscriber_window_days": SUBSCRIBER_WINDOW_DAYS,
        "last_computed_at": _iso(now),
        "log_coverage_start": _iso(coverage_start),
        "log_coverage_end": _iso(coverage_end),
        "log_files": [str(path.relative_to(data_root)) if path.is_relative_to(data_root) else str(path)
                      for path in log_paths],
        "records_seen": records_seen,
        "matched_records": matched_records,
        "top_apps": [
            {"app": app, "requests": count}
            for app, count in subscriber_apps.most_common(5)
        ],
    }
    downloads = {
        "window_hours": DOWNLOAD_WINDOW_HOURS,
        "last_computed_at": _iso(now),
        "episodes": {
            episode: {
                "downloads": len(clients),
                "top_apps": [
                    {"app": app, "downloads": count}
                    for app, count in episode_apps.get(episode, Counter()).most_common(5)
                ],
            }
            for episode, clients in sorted(episode_clients.items(), reverse=True)
        },
    }
    return stats, downloads


def write_podcast_metrics(config: dict, data_root: Path, *, log_paths: list[Path] | None = None,
                          now: datetime | None = None) -> tuple[dict, dict]:
    """Compute metrics and persist them to ``podcast_stats.json`` /
    ``podcast_downloads.json`` under ``data_root``. Returns the same
    ``(stats, downloads)`` tuple as ``compute_podcast_metrics``."""
    stats, downloads = compute_podcast_metrics(config, data_root, log_paths=log_paths, now=now)
    (data_root / "podcast_stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    (data_root / "podcast_downloads.json").write_text(json.dumps(downloads, indent=2) + "\n")
    return stats, downloads
