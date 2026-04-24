"""Flask management console API for the digest pipeline.

Read-only dashboard API that serves pipeline run data, source health,
delivery stats, and podcast status across all configured digests.

Run via: digest-pipeline --console [--digests-dir DIR] [--port PORT]
"""
import json
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request

from .config import load_config
from .podcast_stats import discover_log_paths
from .subscribers import load_subscribers

logger = logging.getLogger("digest")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _read_json(path: Path):
    """Read a JSON file, returning None on any error."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_jsonl_tail(path: Path, n: int = 500) -> list[dict]:
    """Read last n lines from a JSONL file."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            lines = deque(f, maxlen=n)
        results = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return results
    except Exception:
        return []


def _load_podcast_stats(data_root: Path) -> tuple[dict, dict]:
    """Load cached podcast stats artifacts."""
    stats = _read_json(data_root / "podcast_stats.json") or {}
    downloads = _read_json(data_root / "podcast_downloads.json") or {}
    return stats, downloads


def _scan_runs(data_root: Path, days: int = 14) -> list[dict]:
    """List recent runs, newest first.

    Reads run.json when available; synthesizes a minimal record from
    work artifacts for older runs that predate structured logging.
    """
    work_dir = data_root / "work"
    if not work_dir.exists():
        return []
    # Discover date dirs (YYYY-MM-DD pattern)
    date_dirs = sorted(
        [d for d in work_dir.iterdir() if d.is_dir() and DATE_RE.match(d.name)],
        key=lambda d: d.name,
        reverse=True,
    )[:days]
    runs = []
    for d in date_dirs:
        run_path = d / "run.json"
        data = _read_json(run_path)
        if data:
            runs.append(data)
        else:
            # Synthesize from artifacts
            has_final = (d / "final-digest.md").exists()
            extracted = _read_json(d / "extracted.json")
            article_count = len(extracted) if isinstance(extracted, list) else 0
            runs.append({
                "date": d.name,
                "digest": "",
                "started_at": None,
                "completed_at": None,
                "status": "success" if has_final else "unknown",
                "error": None,
                "duration_s": None,
                "stages": [],
                "totals": None,
                "_synthetic": True,
                "_article_count": article_count,
            })
    return runs


def _count_sources(config: dict) -> int:
    """Count total configured sources from config."""
    sources = config.get("sources", {})
    count = 0
    count += len(sources.get("twitter", {}).get("accounts", []))
    for key in ("blogs", "research"):
        count += len(sources.get(key, {}))
    count += len(sources.get("newsletters", {}).get("sources", {}))
    if sources.get("github_trending"):
        count += 1
    return count


def _list_sources(config: dict) -> list[dict]:
    """Build flat list of all configured sources with metadata."""
    sources_cfg = config.get("sources", {})
    result = []

    for acct in sources_cfg.get("twitter", {}).get("accounts", []):
        result.append({
            "source_key": f"twitter:{acct}",
            "source_type": "twitter",
            "name": f"@{acct}",
            "url": f"https://x.com/{acct}",
        })

    for key, blog in sources_cfg.get("blogs", {}).items():
        result.append({
            "source_key": f"blog:{key}",
            "source_type": "blog",
            "name": blog.get("name", key),
            "url": blog.get("feed_url") or blog.get("url", ""),
        })

    for key, blog in sources_cfg.get("research", {}).items():
        result.append({
            "source_key": f"research:{key}",
            "source_type": "research",
            "name": blog.get("name", key),
            "url": blog.get("feed_url") or blog.get("url", ""),
        })

    for key, nl in sources_cfg.get("newsletters", {}).get("sources", {}).items():
        result.append({
            "source_key": f"newsletter:{key}",
            "source_type": "newsletter",
            "name": nl.get("name", key),
            "url": "",
        })

    gt = sources_cfg.get("github_trending")
    if gt:
        result.append({
            "source_key": "github_trending:trending",
            "source_type": "github_trending",
            "name": gt.get("name", "GitHub Trending"),
            "url": gt.get("url", ""),
        })

    return result


def _compute_source_metrics(data_root: Path, days: int = 14) -> dict:
    """Compute per-source metrics from pipeline artifacts over recent runs.

    Scans work/{date}/ directories and aggregates extracted, in-digest,
    exclusive, and priority score metrics per source_key.
    """
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    work_dir = data_root / "work"
    if not work_dir.exists():
        return {}

    # Per-source accumulators
    metrics = {}  # source_key -> {extracted, in_digest, exclusive, scores, dates}

    def _ensure(sk):
        if sk not in metrics:
            metrics[sk] = {
                "extracted": 0,
                "in_digest": 0,
                "exclusive": 0,
                "scores": [],
                "dates": set(),
            }

    for day_dir in sorted(work_dir.iterdir()):
        if not day_dir.is_dir() or not DATE_RE.match(day_dir.name):
            continue
        if day_dir.name < cutoff:
            continue

        # Extracted: count by source_key
        extracted = _read_json(day_dir / "extracted.json")
        if extracted:
            for art in extracted:
                sk = art.get("source_key", "")
                if sk:
                    _ensure(sk)
                    metrics[sk]["extracted"] += 1
                    metrics[sk]["dates"].add(day_dir.name)

        # Prioritized (kept): in-digest count + scores
        kept = _read_json(day_dir / "prioritized.json")
        if kept:
            for art in kept:
                score = art.get("_priority_score")
                for sk in art.get("source_keys", []):
                    _ensure(sk)
                    metrics[sk]["in_digest"] += 1
                    if score is not None:
                        metrics[sk]["scores"].append(score)

        # Prioritized (dropped): scores only
        dropped = _read_json(day_dir / "prioritized_dropped.json")
        if dropped:
            for art in dropped:
                score = art.get("_priority_score")
                if score is not None:
                    for sk in art.get("source_keys", []):
                        _ensure(sk)
                        metrics[sk]["scores"].append(score)

        # Deduped: exclusive count (sole source in story)
        deduped = _read_json(day_dir / "deduped.json")
        if deduped:
            for art in deduped:
                skeys = art.get("source_keys", [])
                if len(skeys) == 1:
                    sk = skeys[0]
                    _ensure(sk)
                    metrics[sk]["exclusive"] += 1

    # Finalize: compute derived metrics
    result = {}
    for sk, m in metrics.items():
        extracted = m["extracted"]
        in_digest = m["in_digest"]
        scores = m["scores"]
        result[sk] = {
            "extracted": extracted,
            "in_digest": in_digest,
            "yield_rate": round(in_digest / extracted, 3) if extracted > 0 else None,
            "exclusive": m["exclusive"],
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "days_active": len(m["dates"]),
        }
    return result


def _sanitize_config(config: dict) -> dict:
    """Return config with internal/secret keys removed."""
    sanitized = {}
    for key, val in config.items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict):
            # Strip keys ending in _env (API key environment variable names)
            sanitized[key] = {
                k: (_sanitize_config(v) if isinstance(v, dict) else v)
                for k, v in val.items()
                if not k.endswith("_env")
            }
        else:
            sanitized[key] = val
    return sanitized


def _parse_rss_items(rss_path: Path) -> list[dict]:
    """Parse podcast RSS feed for item metadata."""
    if not rss_path.exists():
        return []
    try:
        tree = ET.parse(rss_path)
        items = []
        for item in tree.findall(".//item"):
            title = item.findtext("title", "")
            pub_date = item.findtext("pubDate", "")
            enc = item.find("enclosure")
            url = enc.get("url", "") if enc is not None else ""
            length = int(enc.get("length", 0)) if enc is not None else 0
            items.append({
                "title": title,
                "pub_date": pub_date,
                "url": url,
                "length": length,
            })
        return items
    except Exception:
        return []


def _scan_podcast_runs(data_root: Path, days: int = 14) -> list[dict]:
    """List recent podcast runs, newest first.

    Reads {date}-run.json when available; synthesizes minimal records
    for older episodes that predate structured logging.
    """
    podcasts_dir = data_root / "podcasts"
    if not podcasts_dir.exists():
        return []

    _, podcast_downloads = _load_podcast_stats(data_root)
    download_lookup = (podcast_downloads.get("episodes") or {}) if isinstance(podcast_downloads, dict) else {}

    # Read actual run.json files
    run_files = sorted(
        [f for f in podcasts_dir.glob("*-run.json") if DATE_RE.match(f.name[:10])],
        key=lambda f: f.name,
        reverse=True,
    )
    runs = []
    known_dates = set()
    for rf in run_files:
        data = _read_json(rf)
        if data:
            date = data.get("date", rf.name[:10])
            data["download_count"] = ((download_lookup.get(date) or {}).get("downloads") or 0)
            data["article_count"] = _extract_article_count(data)
            runs.append(data)
            known_dates.add(date)

    # Synthesize entries for MP3 files with no run.json
    mp3_files = sorted(podcasts_dir.glob("*.mp3"), key=lambda f: f.name, reverse=True)
    for mp3 in mp3_files:
        date = mp3.stem
        if date in known_dates or not DATE_RE.match(date):
            continue
        size = mp3.stat().st_size
        # Estimate duration from file size (128kbps = 16000 bytes/sec)
        est_duration = size / 16000
        runs.append({
            "date": date,
            "pipeline_type": "podcast",
            "status": "success",
            "duration_s": None,
            "stages": [],
            "totals": {"audio_duration_s": est_duration, "mp3_size": size},
            "download_count": ((download_lookup.get(date) or {}).get("downloads") or 0),
            "article_count": 0,
            "_synthetic": True,
        })

    runs.sort(key=lambda r: r.get("date", ""), reverse=True)
    return runs[:days]


def _extract_article_count(run: dict) -> int:
    """Get article count from a run record (real or synthetic)."""
    if run.get("_article_count") is not None:
        return run["_article_count"]
    count = 0
    for stage in run.get("stages", []):
        if stage.get("stage", "").startswith("Extract"):
            m = re.search(r"Articles found: (\d+)", stage.get("detail", ""))
            if m:
                count += int(m.group(1))
    return count


def _get_source_detail(config, source_type, key):
    """Extract a single source's fields + shared settings from the config dict."""
    sources = config.get("sources", {})

    if source_type == "twitter":
        twitter = sources.get("twitter", {})
        accounts = twitter.get("accounts", [])
        if key not in accounts:
            return None
        return {
            "fields": {"account": key},
            "shared_settings": {
                "lookback": twitter.get("lookback"),
                "max_per_account": twitter.get("max_per_account"),
            },
        }

    if source_type in ("blog", "research"):
        cfg_key = "blogs" if source_type == "blog" else "research"
        section = sources.get(cfg_key, {})
        if key not in section:
            return None
        return {"fields": dict(section[key]), "shared_settings": {}}

    if source_type == "newsletter":
        nl = sources.get("newsletters", {})
        nl_sources = nl.get("sources", {})
        if key not in nl_sources:
            return None
        return {
            "fields": dict(nl_sources[key]),
            "shared_settings": {"imap": nl.get("imap")},
        }

    if source_type == "github_trending":
        gt = sources.get("github_trending")
        if not gt:
            return None
        return {"fields": dict(gt), "shared_settings": {}}

    return None


def _validate_source(source_type, key, fields):
    """Returns list of error strings for invalid source data."""
    errors = []

    def _valid_url(u):
        return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

    if source_type == "twitter":
        if not key or not isinstance(key, str):
            errors.append("key must be a non-empty string")

    elif source_type in ("blog", "research"):
        if not fields.get("name"):
            errors.append("name is required")
        strategy = fields.get("strategy", "rss")
        if strategy not in ("rss", "html_scrape"):
            errors.append("strategy must be one of: rss, html_scrape")
        if strategy == "rss":
            if not _valid_url(fields.get("feed_url", "")):
                errors.append("feed_url is required and must be a valid URL for rss strategy")
        if strategy == "html_scrape":
            if not _valid_url(fields.get("url", "")):
                errors.append("url is required and must be a valid URL for html_scrape strategy")
            if not fields.get("scrape_parser"):
                errors.append("scrape_parser is required for html_scrape strategy")

    elif source_type == "newsletter":
        if not fields.get("name"):
            errors.append("name is required")
        if not fields.get("from"):
            errors.append("from is required")
        lookback = fields.get("lookback_days")
        if lookback is not None:
            if not isinstance(lookback, int) or lookback <= 0:
                errors.append("lookback_days must be a positive integer")

    elif source_type == "github_trending":
        if not fields.get("name"):
            errors.append("name is required")
        if not _valid_url(fields.get("url", "")):
            errors.append("url is required and must be a valid URL")
        min_stars = fields.get("min_stars_week")
        if not isinstance(min_stars, int) or min_stars <= 0:
            errors.append("min_stars_week is required and must be a positive integer")
        ai_kw = fields.get("ai_keywords")
        if not isinstance(ai_kw, list) or len(ai_kw) == 0:
            errors.append("ai_keywords is required and must be a non-empty list")

    return errors


def _write_config(cfg_path, config):
    """Atomic config write — re-reads disk, applies sources mutation, writes back."""
    cfg_path = str(cfg_path)
    with open(cfg_path) as f:
        disk_config = json.load(f)

    # Apply only the sources section from the in-memory config
    disk_config["sources"] = config.get("sources", {})

    # Strip internal keys
    for k in ("_data_root", "_pipeline_dir"):
        disk_config.pop(k, None)

    content = json.dumps(disk_config, indent=2) + "\n"
    dir_name = os.path.dirname(cfg_path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".json")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        fd = -1
        os.replace(tmp_path, cfg_path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _clean_source_state(data_root, source_key):
    """Remove source_key from .source_state.json if it exists."""
    state_path = data_root / ".source_state.json"
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text())
        if source_key in state:
            del state[source_key]
            state_path.write_text(json.dumps(state, indent=2) + "\n")
    except Exception:
        pass


def create_app(digests_dir: str = None, config_path: str = None) -> Flask:
    """Flask app factory for the management console.

    Provide either digests_dir (multi-digest) or config_path (single-digest).
    """
    # Build digest registry: {slug: config_path}
    # Configs are reloaded on each request so changes are picked up without restart.
    digest_configs = {}
    if digests_dir:
        digests_path = Path(digests_dir)
        for cfg_file in sorted(digests_path.glob("*/config.json")):
            slug = cfg_file.parent.name
            try:
                # Validate config loads at startup, but don't cache it
                load_config(str(cfg_file))
                digest_configs[slug] = str(cfg_file)
                logger.info(f"[CONSOLE] Loaded digest: {slug}")
            except Exception as e:
                logger.warning(f"[CONSOLE] Failed to load {cfg_file}: {e}")
    elif config_path:
        config = load_config(config_path)
        slug = config["_data_root"].name
        digest_configs[slug] = config_path

    if not digest_configs:
        raise RuntimeError("No digest configs found")

    # Serve built frontend from console/dist/ if it exists
    dist_dir = Path(__file__).resolve().parent.parent / "console" / "dist"
    if dist_dir.is_dir():
        app = Flask(__name__, static_folder=str(dist_dir), static_url_path="")
    else:
        app = Flask(__name__)

    def _get_digest(slug):
        """Load and return digest info from disk, or None."""
        cfg_path = digest_configs.get(slug)
        if not cfg_path:
            return None
        try:
            config = load_config(cfg_path)
            return {"config": config, "data_root": config["_data_root"]}
        except Exception:
            return None

    def _validate_date(date_str):
        """Return True if date_str is a valid YYYY-MM-DD string."""
        return bool(DATE_RE.match(date_str))

    # --- API Routes ---

    @app.route("/api/digests")
    def list_digests():
        """Overview row for every configured digest: name, source/subscriber
        counts, and a summary of the last run."""
        result = []
        for slug in sorted(digest_configs):
            info = _get_digest(slug)
            if not info:
                continue
            cfg = info["config"]
            data_root = info["data_root"]
            runs = _scan_runs(data_root, days=1)
            last_run = runs[0] if runs else None

            subscribers = load_subscribers(data_root)
            podcast_stats, _ = _load_podcast_stats(data_root)

            result.append({
                "slug": slug,
                "name": cfg.get("digest", {}).get("name", slug),
                "emoji": cfg.get("digest", {}).get("emoji", ""),
                "source_count": _count_sources(cfg),
                "subscriber_count": len(subscribers),
                "email_subscriber_count": len(subscribers),
                "estimated_podcast_subscribers": podcast_stats.get("estimated_subscribers", 0),
                "last_run": {
                    "date": last_run["date"],
                    "status": last_run["status"],
                    "duration_s": last_run.get("duration_s"),
                    "cost": (last_run.get("totals") or {}).get("cost"),
                    "article_count": _extract_article_count(last_run),
                } if last_run else None,
            })
        return jsonify(result)

    @app.route("/api/digests/<slug>/runs")
    def list_runs(slug):
        """Recent run summaries (status, duration, cost, article count) for
        the run-history table."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        runs = _scan_runs(info["data_root"])
        result = []
        for run in runs:
            result.append({
                "date": run["date"],
                "status": run["status"],
                "duration_s": run.get("duration_s"),
                "cost": (run.get("totals") or {}).get("cost"),
                "article_count": _extract_article_count(run),
            })
        return jsonify(result)

    @app.route("/api/digests/<slug>/runs/<date>")
    def get_run(slug, date):
        """Full ``run.json`` for one date, or a minimal synthesized record
        for older runs that predate structured logging."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        if not _validate_date(date):
            return jsonify({"error": "Invalid date"}), 400
        work_dir = info["data_root"] / "work" / date
        if not work_dir.exists():
            return jsonify({"error": "Run not found"}), 404
        data = _read_json(work_dir / "run.json")
        if not data:
            # Synthesize from artifacts
            has_final = (work_dir / "final-digest.md").exists()
            extracted = _read_json(work_dir / "extracted.json")
            article_count = len(extracted) if isinstance(extracted, list) else 0
            data = {
                "date": date,
                "digest": "",
                "started_at": None,
                "completed_at": None,
                "status": "success" if has_final else "unknown",
                "error": None,
                "duration_s": None,
                "stages": [],
                "totals": None,
            }
        return jsonify(data)

    @app.route("/api/digests/<slug>/runs/<date>/funnel")
    def get_funnel(slug, date):
        """Article counts at each pipeline stage (extracted → clustered →
        deduped → prioritized → formatted) for the funnel chart."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        if not _validate_date(date):
            return jsonify({"error": "Invalid date"}), 400
        work_dir = info["data_root"] / "work" / date

        def _count_file(name):
            data = _read_json(work_dir / name)
            return len(data) if isinstance(data, list) else 0

        # Read counts from stage artifacts
        extracted = _count_file("extracted.json")
        clusters = _count_file("clusters.json")
        deduped = _count_file("deduped.json")
        prioritized = _count_file("prioritized.json")

        # If no prioritized.json, articles weren't pruned — use deduped count
        if prioritized == 0 and deduped > 0:
            prioritized_path = work_dir / "prioritized.json"
            if not prioritized_path.exists():
                prioritized = deduped

        # Formatted = same count as prioritized if final digest exists
        final = (work_dir / "final-digest.md").exists()
        formatted = prioritized if final else 0

        return jsonify({
            "extracted": extracted,
            "clustered": clusters,
            "deduped": deduped,
            "prioritized": prioritized,
            "formatted": formatted,
        })

    @app.route("/api/digests/<slug>/runs/<date>/sources")
    def get_run_sources(slug, date):
        """Per-source raw-file breakdown for one run (which sources had
        content vs were empty)."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        if not _validate_date(date):
            return jsonify({"error": "Invalid date"}), 400
        work_dir = info["data_root"] / "work" / date
        if not work_dir.exists():
            return jsonify([])
        result = []
        for f in sorted(work_dir.glob("raw-*.txt")):
            # Parse source key from filename: raw-{type}-{key}.txt
            name = f.stem  # raw-twitter-simonw
            parts = name.split("-", 2)  # ['raw', 'twitter', 'simonw']
            source_type = parts[1] if len(parts) > 1 else ""
            source_key = f"{parts[1]}:{parts[2]}" if len(parts) > 2 else name
            size = f.stat().st_size
            result.append({
                "source_key": source_key,
                "source_type": source_type,
                "file": f.name,
                "size_bytes": size,
                "has_content": size > 0,
            })
        return jsonify(result)

    @app.route("/api/digests/<slug>/sources")
    def list_sources(slug):
        """All configured sources annotated with health metrics (yield rate,
        exclusive-article count, days active) from the last 14 days."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404

        cfg_sources = _list_sources(info["config"])
        source_state = _read_json(info["data_root"] / ".source_state.json") or {}
        source_metrics = _compute_source_metrics(info["data_root"])

        result = []
        for src in cfg_sources:
            key = src["source_key"]
            state = source_state.get(key, {})
            seen_ids = state.get("seen_ids", [])
            seen_count = len(seen_ids) if isinstance(seen_ids, list) else len(seen_ids.keys())
            m = source_metrics.get(key, {})
            result.append({
                **src,
                "last_updated": state.get("last_updated"),
                "seen_count": seen_count,
                "extracted": m.get("extracted", 0),
                "in_digest": m.get("in_digest", 0),
                "yield_rate": m.get("yield_rate"),
                "exclusive": m.get("exclusive", 0),
                "avg_score": m.get("avg_score"),
                "days_active": m.get("days_active", 0),
            })
        return jsonify(result)

    @app.route("/api/digests/<slug>/delivery")
    def get_delivery(slug):
        """Delivery overview: subscriber count plus per-date send aggregates
        (sent / failed / retries) for the last 14 days."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        data_root = info["data_root"]

        subscribers = load_subscribers(data_root)
        sends = _read_jsonl_tail(data_root / "send_history.jsonl", 1000)

        # Aggregate sends by date
        by_date = {}
        for s in sends:
            d = s.get("digest_date", "")
            if d not in by_date:
                by_date[d] = {"date": d, "total": 0, "sent": 0, "failed": 0, "retries": 0}
            by_date[d]["total"] += 1
            by_date[d]["retries"] += int(s.get("retries") or 0)
            if s.get("status") == "sent":
                by_date[d]["sent"] += 1
            else:
                by_date[d]["failed"] += 1

        recent_sends = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)

        return jsonify({
            "subscriber_count": len(subscribers),
            "recent_sends": recent_sends[:14],
        })

    @app.route("/api/digests/<slug>/delivery/sends")
    def get_delivery_sends(slug):
        """Most recent individual send entries (newest first, capped at 100)."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        sends = _read_jsonl_tail(info["data_root"] / "send_history.jsonl", 100)
        sends.reverse()  # newest first
        return jsonify(sends)

    @app.route("/api/digests/<slug>/podcast")
    def get_podcast(slug):
        """Podcast summary: episode list, RSS sync check, and (if
        ``--podcast-stats`` has been run) estimated subscribers + downloads."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        data_root = info["data_root"]
        cfg = info["config"]
        podcast_stats, podcast_downloads = _load_podcast_stats(data_root)
        download_lookup = (podcast_downloads.get("episodes") or {}) if isinstance(podcast_downloads, dict) else {}

        enabled = cfg.get("podcast", {}).get("enabled", False)
        podcasts_dir = data_root / "podcasts"

        # List episodes from filesystem
        episodes = []
        if podcasts_dir.exists():
            mp3_files = sorted(podcasts_dir.glob("*.mp3"), reverse=True)
            for mp3 in mp3_files:
                date = mp3.stem  # 2026-03-24
                script_path = podcasts_dir / f"{date}.txt"
                episodes.append({
                    "date": date,
                    "has_mp3": True,
                    "mp3_size": mp3.stat().st_size,
                    "has_script": script_path.exists(),
                    "download_count": ((download_lookup.get(date) or {}).get("downloads") or 0),
                })

        # RSS info
        rss_items = _parse_rss_items(data_root / "podcast.xml")

        return jsonify({
            "enabled": enabled,
            "name": cfg.get("podcast", {}).get("name", ""),
            "episodes": episodes,
            "rss_item_count": len(rss_items),
            "estimated_subscribers": podcast_stats.get("estimated_subscribers", 0),
            "subscriber_window_days": podcast_stats.get("subscriber_window_days"),
            "stats_last_computed_at": podcast_stats.get("last_computed_at"),
            "log_coverage_start": podcast_stats.get("log_coverage_start"),
            "log_coverage_end": podcast_stats.get("log_coverage_end"),
            "collecting_data": not discover_log_paths(data_root),
        })

    @app.route("/api/digests/<slug>/podcast/runs")
    def list_podcast_runs(slug):
        """Recent podcast-pipeline run summaries (one per episode)."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        runs = _scan_podcast_runs(info["data_root"])
        result = []
        for run in runs:
            totals = run.get("totals") or {}
            audio_s = totals.get("audio_duration_s")
            result.append({
                "date": run.get("date", ""),
                "status": run.get("status", "unknown"),
                "duration_s": run.get("duration_s"),
                "cost": totals.get("cost"),
                "audio_minutes": round(audio_s / 60, 1) if audio_s else None,
                "mp3_size": totals.get("mp3_size"),
            })
        return jsonify(result)

    @app.route("/api/digests/<slug>/podcast/runs/<date>")
    def get_podcast_run(slug, date):
        """Full podcast ``run.json`` for one date, or a minimal synthesized
        record built from the MP3/script files when no run.json exists."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        if not _validate_date(date):
            return jsonify({"error": "Invalid date"}), 400
        run_path = info["data_root"] / "podcasts" / f"{date}-run.json"
        data = _read_json(run_path)
        if not data:
            # Synthesize from MP3/script files
            podcasts_dir = info["data_root"] / "podcasts"
            mp3_path = podcasts_dir / f"{date}.mp3"
            script_path = podcasts_dir / f"{date}.txt"
            if not mp3_path.exists() and not script_path.exists():
                return jsonify({"error": "Podcast run not found"}), 404
            mp3_size = mp3_path.stat().st_size if mp3_path.exists() else 0
            est_duration = mp3_size / 16000 if mp3_size else None
            data = {
                "date": date,
                "digest": "",
                "pipeline_type": "podcast",
                "status": "success" if mp3_path.exists() else "unknown",
                "duration_s": None,
                "stages": [],
                "totals": {
                    "audio_duration_s": est_duration,
                    "mp3_size": mp3_size or None,
                },
                "_synthetic": True,
            }
        return jsonify(data)

    @app.route("/api/digests/<slug>/config")
    def get_config(slug):
        """Return the digest's config with secrets and internal keys stripped."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        return jsonify(_sanitize_config(info["config"]))

    # --- Source CRUD Routes ---

    @app.route("/api/digests/<slug>/sources/<source_type>/<key>")
    def get_source_detail(slug, source_type, key):
        """Single source's config block (for the source-edit form)."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        detail = _get_source_detail(info["config"], source_type, key)
        if not detail:
            return jsonify({"error": "Source not found"}), 404
        return jsonify(detail)

    @app.route("/api/digests/<slug>/sources", methods=["POST"])
    def create_source(slug):
        """Add a new source to the digest's config. Validates type, key
        format, and required fields before writing config.json."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404

        body = request.get_json(silent=True) or {}
        source_type = body.get("source_type", "")
        key = body.get("key", "")
        fields = body.get("fields", {})

        valid_types = ("twitter", "blog", "research", "newsletter", "github_trending")
        if source_type not in valid_types:
            return jsonify({"error": f"source_type must be one of: {', '.join(valid_types)}"}), 400

        # Validate key format
        if not key or not re.match(r"^[a-z0-9_]+$", key):
            return jsonify({"error": "key must be non-empty, lowercase alphanumeric + underscores only"}), 400

        if source_type == "github_trending" and key != "trending":
            return jsonify({"error": "key must be 'trending' for github_trending"}), 400

        # Check source doesn't already exist
        config = info["config"]
        if _get_source_detail(config, source_type, key) is not None:
            return jsonify({"error": "Source already exists"}), 409

        # Validate fields
        errors = _validate_source(source_type, key, fields)
        if errors:
            return jsonify({"error": "Validation failed", "details": errors}), 400

        # Apply mutation
        sources = config.setdefault("sources", {})
        if source_type == "twitter":
            twitter = sources.setdefault("twitter", {"accounts": []})
            twitter.setdefault("accounts", []).append(key)
        elif source_type == "blog":
            sources.setdefault("blogs", {})[key] = fields
        elif source_type == "research":
            sources.setdefault("research", {})[key] = fields
        elif source_type == "newsletter":
            nl = sources.setdefault("newsletters", {"sources": {}})
            nl.setdefault("sources", {})[key] = fields
        elif source_type == "github_trending":
            sources["github_trending"] = fields

        _write_config(digest_configs[slug], config)
        return jsonify({"ok": True}), 201

    @app.route("/api/digests/<slug>/sources/<source_type>/<key>", methods=["PUT"])
    def update_source(slug, source_type, key):
        """Update an existing source's fields. Body fields are merged into
        the existing record so a partial PUT can't drop unspecified keys."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404

        body = request.get_json(silent=True) or {}
        fields = body.get("fields", {})

        config = info["config"]
        if _get_source_detail(config, source_type, key) is None:
            return jsonify({"error": "Source not found"}), 404

        # Merge with existing fields to prevent data loss on partial updates
        existing = _get_source_detail(config, source_type, key)
        merged = {**existing["fields"], **fields}
        fields = merged

        errors = _validate_source(source_type, key, fields)
        if errors:
            return jsonify({"error": "Validation failed", "details": errors}), 400

        # Apply mutation
        sources = config.get("sources", {})
        if source_type == "twitter":
            return jsonify({"error": "Twitter sources have no editable fields"}), 400
        elif source_type == "blog":
            sources.get("blogs", {})[key] = fields
        elif source_type == "research":
            sources.get("research", {})[key] = fields
        elif source_type == "newsletter":
            sources.get("newsletters", {}).get("sources", {})[key] = fields
        elif source_type == "github_trending":
            sources["github_trending"] = fields

        _write_config(digest_configs[slug], config)
        return jsonify({"ok": True})

    @app.route("/api/digests/<slug>/sources/<source_type>/<key>", methods=["DELETE"])
    def delete_source(slug, source_type, key):
        """Remove a source from config and prune its entry from
        ``.source_state.json`` so a re-added source starts fresh."""
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404

        config = info["config"]
        if _get_source_detail(config, source_type, key) is None:
            return jsonify({"error": "Source not found"}), 404

        # Remove from config
        sources = config.get("sources", {})
        if source_type == "twitter":
            accounts = sources.get("twitter", {}).get("accounts", [])
            if key in accounts:
                accounts.remove(key)
            if not accounts:
                sources.pop("twitter", None)
        elif source_type == "blog":
            blogs = sources.get("blogs", {})
            blogs.pop(key, None)
            if not blogs:
                sources.pop("blogs", None)
        elif source_type == "research":
            research = sources.get("research", {})
            research.pop(key, None)
            if not research:
                sources.pop("research", None)
        elif source_type == "newsletter":
            nl_sources = sources.get("newsletters", {}).get("sources", {})
            nl_sources.pop(key, None)
            if not nl_sources:
                sources.pop("newsletters", None)
        elif source_type == "github_trending":
            sources.pop("github_trending", None)

        _write_config(digest_configs[slug], config)

        # Clean source state
        state_key_map = {
            "twitter": f"twitter:{key}",
            "blog": f"blog:{key}",
            "research": f"research:{key}",
            "newsletter": f"newsletter:{key}",
            "github_trending": "github_trending:trending",
        }
        state_key = state_key_map.get(source_type, "")
        if state_key:
            _clean_source_state(info["data_root"], state_key)

        return jsonify({"ok": True})

    @app.route("/health")
    def health():
        """Liveness probe — returns 200 with the active digest slugs."""
        return jsonify({"status": "ok", "digests": list(digest_configs.keys())})

    # Serve frontend
    @app.route("/")
    def index():
        """Serve the built Preact SPA, or a hint message if the bundle is
        missing (i.e., ``cd console && npm run build`` was never run)."""
        if app.static_folder and Path(app.static_folder, "index.html").exists():
            return app.send_static_file("index.html")
        return jsonify({"message": "Console frontend not built. Run: cd console && npm run build"})

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "Internal server error"}), 500

    return app
