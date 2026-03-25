"""Flask management console API for the digest pipeline.

Read-only dashboard API that serves pipeline run data, source health,
delivery stats, and podcast status across all configured digests.

Run via: digest-pipeline --console [--digests-dir DIR] [--port PORT]
"""
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from flask import Flask, jsonify

from .config import load_config
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


def create_app(digests_dir: str = None, config_path: str = None) -> Flask:
    """Flask app factory for the management console.

    Provide either digests_dir (multi-digest) or config_path (single-digest).
    """
    # Build digest registry: {slug: {"config": dict, "data_root": Path}}
    digests = {}
    if digests_dir:
        digests_path = Path(digests_dir)
        for cfg_file in sorted(digests_path.glob("*/config.json")):
            slug = cfg_file.parent.name
            try:
                config = load_config(str(cfg_file))
                digests[slug] = {"config": config, "data_root": config["_data_root"]}
                logger.info(f"[CONSOLE] Loaded digest: {slug}")
            except Exception as e:
                logger.warning(f"[CONSOLE] Failed to load {cfg_file}: {e}")
    elif config_path:
        config = load_config(config_path)
        slug = config["_data_root"].name
        digests[slug] = {"config": config, "data_root": config["_data_root"]}

    if not digests:
        raise RuntimeError("No digest configs found")

    # Serve built frontend from console/dist/ if it exists
    dist_dir = Path(__file__).resolve().parent.parent / "console" / "dist"
    if dist_dir.is_dir():
        app = Flask(__name__, static_folder=str(dist_dir), static_url_path="")
    else:
        app = Flask(__name__)

    def _get_digest(slug):
        """Return digest info or None."""
        return digests.get(slug)

    def _validate_date(date_str):
        """Return True if date_str is a valid YYYY-MM-DD string."""
        return bool(DATE_RE.match(date_str))

    # --- API Routes ---

    @app.route("/api/digests")
    def list_digests():
        result = []
        for slug, info in sorted(digests.items()):
            cfg = info["config"]
            data_root = info["data_root"]
            runs = _scan_runs(data_root, days=1)
            last_run = runs[0] if runs else None

            subscribers = load_subscribers(data_root)

            result.append({
                "slug": slug,
                "name": cfg.get("digest", {}).get("name", slug),
                "emoji": cfg.get("digest", {}).get("emoji", ""),
                "source_count": _count_sources(cfg),
                "subscriber_count": len(subscribers),
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
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404

        cfg_sources = _list_sources(info["config"])
        source_state = _read_json(info["data_root"] / ".source_state.json") or {}

        result = []
        for src in cfg_sources:
            key = src["source_key"]
            state = source_state.get(key, {})
            seen_ids = state.get("seen_ids", [])
            seen_count = len(seen_ids) if isinstance(seen_ids, list) else len(seen_ids.keys())
            result.append({
                **src,
                "last_updated": state.get("last_updated"),
                "seen_count": seen_count,
            })
        return jsonify(result)

    @app.route("/api/digests/<slug>/delivery")
    def get_delivery(slug):
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
                by_date[d] = {"date": d, "total": 0, "sent": 0, "failed": 0}
            by_date[d]["total"] += 1
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
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        sends = _read_jsonl_tail(info["data_root"] / "send_history.jsonl", 100)
        sends.reverse()  # newest first
        return jsonify(sends)

    @app.route("/api/digests/<slug>/podcast")
    def get_podcast(slug):
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        data_root = info["data_root"]
        cfg = info["config"]

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
                })

        # RSS info
        rss_items = _parse_rss_items(data_root / "podcast.xml")

        return jsonify({
            "enabled": enabled,
            "name": cfg.get("podcast", {}).get("name", ""),
            "episodes": episodes,
            "rss_item_count": len(rss_items),
        })

    @app.route("/api/digests/<slug>/config")
    def get_config(slug):
        info = _get_digest(slug)
        if not info:
            return jsonify({"error": "Unknown digest"}), 404
        return jsonify(_sanitize_config(info["config"]))

    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "digests": list(digests.keys())})

    # Serve frontend
    @app.route("/")
    def index():
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
