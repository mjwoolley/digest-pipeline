#!/usr/bin/env python3
"""
Stage 1: Gather raw content from all sources (concurrent).
Usage:
  - CLI: python3 gather.py --config /path/to/config.json [work_dir]
  - Import: from gather import gather_all
"""
import email
import email.policy
import imaplib
import json
import logging
import re
import subprocess
import sys
import os
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger("digest")

MAX_CONCURRENT = 6


# ── Credential loading ──────────────────────────────────────────────────────

def load_secrets(secrets_file: Path):
    """Load key=value pairs from secrets.env if vars aren't already set."""
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key and value and key not in os.environ:
                os.environ[key] = value


# ── Source fetchers ──────────────────────────────────────────────────────────

def _fetch_twitter(account: str, auth_token: str, ct0: str) -> dict:
    """Fetch tweets for a single account."""
    name = f"@{account}"
    env = {**os.environ, "AUTH_TOKEN": auth_token, "CT0": ct0}
    try:
        try:
            result = subprocess.run(
                ["bird", "search", f"from:{account}", "-n", "10"],
                capture_output=True, text=True, env=env, timeout=30
            )
        except FileNotFoundError:
            raise RuntimeError(
                "'bird' CLI not found. Install with: npm install -g bird "
                "(see https://github.com/nichochar/bird)"
            )
        content = result.stdout.strip() if result.returncode == 0 else ""
        if not content:
            logger.warning(f"[GATHER] {name}: empty or failed")
        else:
            logger.info(f"[GATHER] {name}: OK")
        return {"name": name, "type": "twitter", "key": f"twitter-{account}",
                "content": content}
    except Exception as e:
        logger.warning(f"[GATHER] {name}: {e}")
        return {"name": name, "type": "twitter", "key": f"twitter-{account}",
                "content": ""}


def _get_last_success_date(data_root: Path = None) -> datetime | None:
    """Find the date of the last successful digest run from output files.

    Looks for YYYY-MM-DD.md files in the data_root directory.
    Returns the datetime of the most recent one, or None if none found.
    """
    if data_root is None:
        return None
    try:
        dates = []
        for f in data_root.iterdir():
            if f.suffix == ".md" and re.match(r"\d{4}-\d{2}-\d{2}", f.stem):
                dates.append(datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc))
        return max(dates) if dates else None
    except Exception:
        return None


def _fetch_newsletter(key: str, nl: dict, imap_host: str,
                      imap_email: str, imap_password: str,
                      last_success: datetime = None) -> dict:
    """Fetch a newsletter via IMAP.

    Connects to the IMAP server, searches for recent emails matching the
    sender address, and returns the plain-text body of the most recent match.

    If last_success is provided, uses that as the lookback date instead of
    lookback_days, ensuring no newsletters are missed between runs.
    """
    name = nl["name"]
    sender = nl["from"]
    if last_success is not None:
        since_date = last_success.strftime("%d-%b-%Y")
    else:
        lookback_days = nl.get("lookback_days", 1)
        since_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")

    try:
        conn = imaplib.IMAP4_SSL(imap_host)
        conn.login(imap_email, imap_password)
        conn.select("INBOX", readonly=True)

        # Search for recent messages from the sender
        status, msg_ids = conn.search(None, f'(FROM "{sender}" SINCE {since_date})')
        if status != "OK" or not msg_ids[0]:
            conn.logout()
            logger.info(f"[GATHER] {name}: no recent issue")
            return {"name": name, "type": "newsletter", "key": f"nl-{key}",
                    "content": ""}

        # Fetch the most recent match
        latest_id = msg_ids[0].split()[-1]
        status, msg_data = conn.fetch(latest_id, "(RFC822)")
        conn.logout()

        if status != "OK":
            logger.warning(f"[GATHER] {name}: fetch failed")
            return {"name": name, "type": "newsletter", "key": f"nl-{key}",
                    "content": ""}

        msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
        content = _extract_email_text(msg)
        # Truncate to first 300 lines
        content = "\n".join(content.split("\n")[:300])

        logger.info(f"[GATHER] {name}: OK")
        return {"name": name, "type": "newsletter", "key": f"nl-{key}",
                "content": content}
    except Exception as e:
        logger.warning(f"[GATHER] {name}: {e}")
        return {"name": name, "type": "newsletter", "key": f"nl-{key}",
                "content": ""}


def _extract_email_text(msg: email.message.Message) -> str:
    """Extract plain text from an email message.

    Prefers text/plain parts. Falls back to text/html with tags stripped.
    """
    # Try plain text first
    body = msg.get_body(preferencelist=("plain",))
    if body:
        return body.get_content()

    # Fall back to HTML with tags stripped
    body = msg.get_body(preferencelist=("html",))
    if body:
        html = body.get_content()
        # Simple tag stripping — good enough for newsletter extraction
        text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    return ""


def _fetch_blog(key: str, blog: dict) -> dict:
    """Fetch and parse a blog RSS feed."""
    name = blog["name"]
    try:
        try:
            result = subprocess.run(
                ["curl", "-sL", "--max-time", "10", blog["feed_url"]],
                capture_output=True, text=True, timeout=15
            )
        except FileNotFoundError:
            raise RuntimeError(
                "'curl' not found. Install with: apt install curl (Linux) "
                "or brew install curl (macOS)"
            )
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(f"[GATHER] {name}: fetch failed")
            return {"name": name, "type": "blog", "key": f"blog-{key}",
                    "content": ""}

        parsed = parse_rss_recent(result.stdout)
        if parsed is not None:
            content = parsed
            logger.info(f"[GATHER] {name}: OK (XML parsed)")
        else:
            content = "\n".join(result.stdout.split("\n")[:300])
            logger.info(f"[GATHER] {name}: OK (raw truncated)")
        return {"name": name, "type": "blog", "key": f"blog-{key}",
                "content": content}
    except Exception as e:
        logger.warning(f"[GATHER] {name}: {e}")
        return {"name": name, "type": "blog", "key": f"blog-{key}",
                "content": ""}


# ── RSS parsing ──────────────────────────────────────────────────────────────

def parse_rss_recent(xml_text, hours=24):
    """Parse RSS/Atom XML, return only entries from the last `hours` hours.
    Falls back to raw truncation if parsing fails."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    entries = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "dc": "http://purl.org/dc/elements/1.1/",
    }

    # Try Atom format
    for entry in root.findall(".//atom:entry", ns):
        updated = entry.findtext("atom:updated", "", ns)
        published = entry.findtext("atom:published", "", ns)
        title = entry.findtext("atom:title", "", ns)
        link_el = entry.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        content = (entry.findtext("atom:content", "", ns)
                   or entry.findtext("atom:summary", "", ns) or "")
        date_str = updated or published
        if date_str and _parse_date(date_str) and _parse_date(date_str) < cutoff:
            continue
        entries.append(f"TITLE: {title}\nLINK: {link}\n{content[:2000]}\n---")

    # Try RSS 2.0 format
    if not entries:
        for item in root.findall(".//item"):
            pub_date = (item.findtext("pubDate", "")
                        or item.findtext("dc:date", "", ns))
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "") or ""
            content_encoded = item.findtext(
                "{http://purl.org/rss/1.0/modules/content/}encoded", "") or ""
            body = content_encoded or desc
            if pub_date and _parse_date(pub_date) and _parse_date(pub_date) < cutoff:
                continue
            entries.append(f"TITLE: {title}\nLINK: {link}\n{body[:2000]}\n---")

    return "\n".join(entries) if entries else ""


def _parse_date(date_str):
    """Best-effort parse of RSS/Atom date strings."""
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ]:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


# ── GitHub Trending HTML parsing ─────────────────────────────────────────────

class TrendingParser(HTMLParser):
    """Parse GitHub Trending HTML into a list of repo dicts."""

    def __init__(self):
        super().__init__()
        self.repos = []
        self._current = None
        self._in_h2 = False
        self._in_link = False
        self._in_desc = False
        self._in_lang = False
        self._in_stars = False
        self._text_buf = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "")

        if tag == "article" and "Box-row" in classes:
            self._current = {"owner": "", "repo": "", "description": "",
                             "language": "", "stars": 0, "url": ""}
        elif self._current is not None:
            if tag == "h2" and "h3" in classes:
                self._in_h2 = True
            elif tag == "a" and self._in_h2:
                href = attrs_dict.get("href", "")
                parts = [p for p in href.split("/") if p]
                if len(parts) >= 2:
                    self._current["owner"] = parts[0]
                    self._current["repo"] = parts[1]
                    self._current["url"] = f"https://github.com/{parts[0]}/{parts[1]}"
                self._in_link = True
            elif tag == "p" and "col-9" in classes:
                self._in_desc = True
                self._text_buf = ""
            elif tag == "span" and attrs_dict.get("itemprop") == "programmingLanguage":
                self._in_lang = True
                self._text_buf = ""
            elif tag == "span" and "d-inline-block" in classes and "float-sm-right" in classes:
                self._in_stars = True
                self._text_buf = ""

    def handle_endtag(self, tag):
        if tag == "article" and self._current is not None:
            self.repos.append(self._current)
            self._current = None
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "a" and self._in_link:
            self._in_link = False
        elif tag == "p" and self._in_desc:
            self._current["description"] = self._text_buf.strip()
            self._in_desc = False
        elif tag == "span" and self._in_lang:
            self._current["language"] = self._text_buf.strip()
            self._in_lang = False
        elif tag == "span" and self._in_stars:
            m = re.search(r"([\d,]+)\s+stars?\s+(today|this\s+week|this\s+month)", self._text_buf)
            if m:
                self._current["stars"] = int(m.group(1).replace(",", ""))
            self._in_stars = False

    def handle_data(self, data):
        if self._in_desc or self._in_lang or self._in_stars:
            self._text_buf += data


def _fetch_readme(owner: str, repo: str) -> str:
    """Fetch first 3000 chars of a repo's README via raw.githubusercontent.com."""
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", "5",
             f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md"],
            capture_output=True, text=True, timeout=8
        )
        if result.returncode == 0 and "404" not in result.stdout[:20]:
            return result.stdout[:3000]
    except Exception:
        pass
    return ""


def parse_github_trending(html: str, min_stars: int, keywords: list[str]) -> str:
    """Parse GitHub Trending HTML and filter by stars + AI keywords.
    Fetches READMEs for richer keyword matching.
    Returns formatted string matching RSS convention."""
    parser = TrendingParser()
    parser.feed(html)

    entries = []
    for repo in parser.repos:
        if repo["stars"] < min_stars:
            continue
        readme = _fetch_readme(repo["owner"], repo["repo"])
        combined = (repo["description"] + " " + readme).lower()
        if not any(re.search(r'\b' + re.escape(kw) + r'\b', combined)
                   for kw in keywords):
            continue
        lang = f" [{repo['language']}]" if repo["language"] else ""
        readme_excerpt = readme[:500].strip()
        parts = [
            f"TITLE: {repo['owner']}/{repo['repo']}{lang}",
            f"LINK: {repo['url']}",
            f"Stars this week: {repo['stars']}",
            repo["description"],
        ]
        if readme_excerpt:
            parts.append(readme_excerpt)
        parts.append("---")
        entries.append("\n".join(parts))
    return "\n".join(entries)


def _fetch_github_trending(cfg: dict) -> dict:
    """Fetch GitHub Trending page and extract AI-relevant repos."""
    name = cfg["name"]
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", "10", cfg["url"]],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(f"[GATHER] {name}: fetch failed")
            return {"name": name, "type": "github_trending",
                    "key": "github-trending", "content": ""}

        content = parse_github_trending(
            result.stdout, cfg["min_stars_week"], cfg["ai_keywords"])
        if content:
            logger.info(f"[GATHER] {name}: OK")
        else:
            logger.info(f"[GATHER] {name}: no matching repos")
        return {"name": name, "type": "github_trending",
                "key": "github-trending", "content": content}
    except Exception as e:
        logger.warning(f"[GATHER] {name}: {e}")
        return {"name": name, "type": "github_trending",
                "key": "github-trending", "content": ""}


# ── Main gather function ────────────────────────────────────────────────────

def gather_all(work_dir: Path = None, sources_config: dict = None,
               data_root: Path = None) -> list[dict]:
    """Gather content from all sources concurrently.

    Args:
        work_dir: Directory for debug output files
        sources_config: Sources section from config.json. If None, looks for
                        sources.json in the old location (backward compat).
        data_root: Digest output directory (for last-success lookback).

    Returns list of dicts: {name, type, key, content}.
    """
    if sources_config is None:
        # Backward compat: load from sources.json
        skill_dir = Path(__file__).parent.parent
        sources_file = skill_dir / "sources.json"
        secrets_file = skill_dir / "sources-secrets.env"
        load_secrets(secrets_file)
        sources_config = json.load(open(sources_file))

    # Determine last successful run date for newsletter lookback
    last_success = _get_last_success_date(data_root)
    if last_success:
        logger.info(f"[GATHER] Newsletter lookback: since last run {last_success.strftime('%Y-%m-%d')}")
    else:
        logger.info("[GATHER] Newsletter lookback: using per-source lookback_days (no prior run found)")

    auth_token = os.environ.get("AUTH_TOKEN", "")
    ct0 = os.environ.get("CT0", "")

    # IMAP config for newsletters
    imap_cfg = sources_config.get("newsletters", {}).get("imap", {})
    imap_host = imap_cfg.get("host", "imap.gmail.com")
    imap_email = imap_cfg.get("email", "")
    imap_password = os.environ.get("IMAP_PASSWORD", "")

    if not auth_token or not ct0:
        logger.warning("[GATHER] Twitter credentials not found")
    if "newsletters" in sources_config and not imap_password:
        logger.warning("[GATHER] IMAP_PASSWORD not found")

    futures = {}
    results = []

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
        # Twitter
        if "twitter" in sources_config:
            for account in sources_config["twitter"]["accounts"]:
                f = pool.submit(_fetch_twitter, account, auth_token, ct0)
                futures[f] = f"twitter-{account}"

        # Newsletters
        if "newsletters" in sources_config:
            nl_sources = sources_config["newsletters"].get("sources", {})
            for key, nl in nl_sources.items():
                f = pool.submit(_fetch_newsletter, key, nl,
                                imap_host, imap_email, imap_password,
                                last_success=last_success)
                futures[f] = f"nl-{key}"

        # Blogs
        if "blogs" in sources_config:
            for key, blog in sources_config["blogs"].items():
                f = pool.submit(_fetch_blog, key, blog)
                futures[f] = f"blog-{key}"

        # Research (treated as blogs)
        if "research" in sources_config:
            for key, blog in sources_config["research"].items():
                f = pool.submit(_fetch_blog, key, blog)
                futures[f] = f"blog-{key}"

        # GitHub Trending
        if "github_trending" in sources_config:
            gh_cfg = sources_config["github_trending"]
            f = pool.submit(_fetch_github_trending, gh_cfg)
            futures[f] = "github-trending"

        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"[GATHER] {futures[future]}: unexpected error: {e}")

    # Write debug files if work_dir provided
    if work_dir:
        work_dir.mkdir(exist_ok=True)
        for r in results:
            out = work_dir / f"raw-{r['key']}.txt"
            out.write_text(r.get("content", ""))

    # Sort by key for deterministic ordering
    results.sort(key=lambda r: r["key"])

    non_empty = sum(1 for r in results if r.get("content", "").strip())
    logger.info(f"[GATHER] Done: {non_empty}/{len(results)} sources with content")

    return results


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [%(levelname)s] %(message)s")

    # Parse args
    config_path = None
    work = None
    for arg in sys.argv[1:]:
        if arg == "--config":
            continue
        if config_path is None and arg.endswith(".json"):
            config_path = arg
        elif work is None:
            work = Path(arg)

    sources = None
    if config_path:
        from digest_pipeline.config import load_config
        cfg = load_config(config_path)
        sources = cfg.get("sources")

    if work is None:
        work = Path("/tmp/digest-gather")

    results = gather_all(work, sources_config=sources)
    print(f"\nGather complete. {len(results)} sources.")
    for r in results:
        size = len(r.get("content", ""))
        status = "OK" if size > 0 else "empty"
        print(f"  {r['key']}: {size} bytes [{status}]")
