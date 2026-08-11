#!/usr/bin/env python3
"""Stage 1: gather raw content from every configured source, concurrently.

One fetcher per source type (Twitter via the ``bird`` CLI, IMAP for
newsletters, ``curl`` + XML parsing for RSS/Atom feeds, custom HTML parsers
for sites that don't expose a feed, and a GitHub Trending scraper). Fetches
run in a ``ThreadPoolExecutor`` capped at ``MAX_CONCURRENT``. Each fetcher
returns a uniform ``{source_key, source_type, source_label, source_url,
content}`` dict so downstream stages don't have to special-case sources.

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
        for line in secrets_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key and value and key not in os.environ:
                os.environ[key] = value


# ── Source fetchers ──────────────────────────────────────────────────────────

def _fetch_twitter(account: str, auth_token: str, ct0: str) -> dict:
    """Fetch tweets for a single account."""
    source_label = f"@{account}"
    source_key = f"twitter:{account}"
    source_url = f"https://twitter.com/{account}"
    env = {**os.environ, "AUTH_TOKEN": auth_token, "CT0": ct0}
    base = {"source_key": source_key, "source_type": "twitter",
            "source_label": source_label, "source_url": source_url}
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
            logger.warning(f"[GATHER] {source_label}: empty or failed")
        else:
            logger.info(f"[GATHER] {source_label}: OK")
        return {**base, "content": content}
    except Exception as e:
        logger.warning(f"[GATHER] {source_label}: {e}")
        return {**base, "content": ""}


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
    source_label = nl["name"]
    source_key = f"newsletter:{key}"
    base = {"source_key": source_key, "source_type": "newsletter",
            "source_label": source_label, "source_url": ""}
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
            logger.info(f"[GATHER] {source_label}: no recent issue")
            return {**base, "content": ""}

        # Fetch the most recent match
        latest_id = msg_ids[0].split()[-1]
        status, msg_data = conn.fetch(latest_id, "(RFC822)")
        conn.logout()

        if status != "OK":
            logger.warning(f"[GATHER] {source_label}: fetch failed")
            return {**base, "content": ""}

        msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
        content = _extract_email_text(msg)
        # Truncate to first 300 lines
        content = "\n".join(content.split("\n")[:300])

        # Extract Message-ID for stable dedup identity
        msg_id = _extract_message_id(msg)

        logger.info(f"[GATHER] {source_label}: OK")
        result = {**base, "content": content}
        if msg_id:
            result["message_id"] = msg_id
        return result
    except Exception as e:
        logger.warning(f"[GATHER] {source_label}: {e}")
        return {**base, "content": ""}


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


def _extract_message_id(msg: email.message.Message) -> str | None:
    """Extract the Message-ID header, stripped of angle brackets."""
    raw = msg.get("Message-ID", "")
    if not raw:
        return None
    # Strip angle brackets: <abc@example.com> -> abc@example.com
    cleaned = raw.strip().strip("<>").strip()
    return cleaned if cleaned else None


def _is_bad_payload(text: str) -> bool:
    """Check if a fetched payload is an error page or app-shell, not real content.

    Note: ``_next/static`` alone is NOT a rejection signal — legitimate Next.js
    pages (e.g. Anthropic /news) contain it.  Only explicit error markers and
    "Not Found" titles/headings trigger rejection.
    """
    error_markers = ["NEXT_HTTP_ERROR_FALLBACK", "__next_error__"]
    for marker in error_markers:
        if marker in text:
            return True
    # Detect generic "Not Found" HTML error pages (title or heading)
    if re.search(r'<(title|h1)[^>]*>\s*Not Found\s*</', text, re.IGNORECASE):
        return True
    return False


def _fetch_blog(key: str, blog: dict, source_type: str = "blog") -> dict:
    """Fetch and parse a blog source. Dispatches by strategy."""
    source_label = blog["name"]
    source_key = f"{source_type}:{key}"
    source_url = blog.get("url", blog.get("feed_url", ""))
    base = {"source_key": source_key, "source_type": source_type,
            "source_label": source_label, "source_url": source_url}
    strategy = blog.get("strategy", "rss")

    if strategy == "html_scrape":
        return _fetch_blog_html_scrape(key, blog, source_type=source_type)

    # Default: RSS strategy
    try:
        try:
            result = subprocess.run(
                ["curl", "-sL", "--compressed", "--max-time", "10", blog["feed_url"]],
                capture_output=True, text=True, timeout=15
            )
        except FileNotFoundError:
            raise RuntimeError(
                "'curl' not found. Install with: apt install curl (Linux) "
                "or brew install curl (macOS)"
            )
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(f"[GATHER] {source_label}: fetch failed")
            return {**base, "content": ""}

        if _is_bad_payload(result.stdout):
            logger.warning(f"[GATHER] {source_label}: bad payload (error page or app-shell)")
            return {**base, "content": ""}

        feed_hours = blog.get("feed_hours", 24)
        parsed = parse_rss_recent(result.stdout, hours=feed_hours)
        if parsed is not None:
            content = parsed
            logger.info(f"[GATHER] {source_label}: OK (XML parsed)")
        else:
            logger.warning(f"[GATHER] {source_label}: failed to parse RSS/Atom XML")
            content = ""
        return {**base, "content": content}
    except Exception as e:
        logger.warning(f"[GATHER] {source_label}: {e}")
        return {**base, "content": ""}


# ── HTML scrape strategy ─────────────────────────────────────────────────────

class AnthropicNewsParser(HTMLParser):
    """Parse the Anthropic /news landing page to extract article links/titles.

    Skips the curated/featured section at the top of the page and only
    extracts from the chronological list that follows the <h2>News</h2> heading.
    """

    def __init__(self):
        super().__init__()
        self.articles = []  # list of (title, url)
        self._in_link = False
        self._current_href = ""
        self._text_buf = ""
        self._in_h2 = False
        self._past_news_heading = False

    def handle_starttag(self, tag, attrs):
        if tag == "h2":
            self._in_h2 = True
            self._text_buf = ""
        elif tag == "a" and self._past_news_heading:
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if href.startswith("/news/") and href != "/news/" and href != "/news":
                self._in_link = True
                self._current_href = href
                self._text_buf = ""

    def handle_endtag(self, tag):
        if tag == "h2" and self._in_h2:
            if self._text_buf.strip() == "News":
                self._past_news_heading = True
            self._in_h2 = False
        elif tag == "a" and self._in_link:
            title = self._text_buf.strip()
            if title and self._current_href:
                url = f"https://www.anthropic.com{self._current_href}"
                # Deduplicate by URL
                if not any(u == url for _, u in self.articles):
                    self.articles.append((title, url))
            self._in_link = False

    def handle_data(self, data):
        if self._in_h2 or self._in_link:
            self._text_buf += data


def parse_anthropic_news(html: str) -> str:
    """Parse the Anthropic /news page HTML and return formatted entries."""
    parser = AnthropicNewsParser()
    parser.feed(html)
    entries = []
    for title, url in parser.articles:
        entries.append(f"TITLE: {title}\nLINK: {url}\n---")
    return "\n".join(entries)


class AddeparBlogParser(HTMLParser):
    """Parse the Addepar /blog page to extract article links, titles, and descriptions."""

    def __init__(self):
        super().__init__()
        self.articles = []  # list of (title, url, description)
        self._in_feature = False
        self._current_href = ""
        self._in_h3 = False
        self._in_time = False
        self._in_desc_p = False
        self._text_buf = ""
        self._title = ""
        self._desc = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "")
        if tag == "a" and "blog-feature" in classes:
            href = attrs_dict.get("href", "")
            if href.startswith("/blog/") and href != "/blog/" and href != "/blog":
                self._in_feature = True
                self._current_href = href
                self._title = ""
                self._desc = ""
        elif self._in_feature:
            if tag == "h3":
                self._in_h3 = True
                self._text_buf = ""
            elif tag == "time":
                self._in_time = True
                self._text_buf = ""
            elif tag == "p" and "measure" in classes and not self._in_desc_p:
                self._in_desc_p = True
                self._text_buf = ""

    def handle_endtag(self, tag):
        if tag == "a" and self._in_feature:
            if self._title and self._current_href:
                url = f"https://www.addepar.com{self._current_href}"
                if not any(u == url for _, u, _ in self.articles):
                    self.articles.append((self._title, url, self._desc))
            self._in_feature = False
        elif tag == "h3" and self._in_h3:
            self._title = self._text_buf.strip()
            self._in_h3 = False
        elif tag == "time" and self._in_time:
            self._in_time = False
        elif tag == "p" and self._in_desc_p:
            self._desc = self._text_buf.strip()
            self._in_desc_p = False

    def handle_data(self, data):
        if self._in_h3 or self._in_desc_p:
            self._text_buf += data
        elif self._in_time:
            self._text_buf += data


def parse_addepar_blog(html: str) -> str:
    """Parse the Addepar /blog page HTML and return formatted entries."""
    parser = AddeparBlogParser()
    parser.feed(html)
    entries = []
    for title, url, desc in parser.articles[:15]:
        parts = [f"TITLE: {title}", f"LINK: {url}"]
        if desc:
            parts.append(desc)
        parts.append("---")
        entries.append("\n".join(parts))
    return "\n".join(entries)


def _fetch_blog_html_scrape(key: str, blog: dict,
                            source_type: str = "blog") -> dict:
    """Fetch a blog via HTML scraping instead of RSS."""
    source_label = blog["name"]
    source_key = f"{source_type}:{key}"
    source_url = blog.get("url", blog.get("feed_url", ""))
    base = {"source_key": source_key, "source_type": source_type,
            "source_label": source_label, "source_url": source_url}
    scrape_parser = blog.get("scrape_parser", "")
    try:
        result = subprocess.run(
            ["curl", "-sL", "--compressed", "--max-time", "10", source_url],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(f"[GATHER] {source_label}: fetch failed")
            return {**base, "content": ""}

        if _is_bad_payload(result.stdout):
            logger.warning(f"[GATHER] {source_label}: bad payload (error page or app-shell)")
            return {**base, "content": ""}

        if scrape_parser == "anthropic_news":
            content = parse_anthropic_news(result.stdout)
        elif scrape_parser == "addepar_blog":
            content = parse_addepar_blog(result.stdout)
        else:
            logger.warning(f"[GATHER] {source_label}: unknown scrape_parser '{scrape_parser}'")
            return {**base, "content": ""}

        if content:
            logger.info(f"[GATHER] {source_label}: OK (HTML scraped)")
        else:
            logger.warning(f"[GATHER] {source_label}: no articles found in HTML")
        return {**base, "content": content}
    except Exception as e:
        logger.warning(f"[GATHER] {source_label}: {e}")
        return {**base, "content": ""}


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
            ["curl", "-sL", "--compressed", "--max-time", "5",
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
    source_label = cfg["name"]
    source_key = "github_trending:trending"
    source_url = cfg["url"]
    base = {"source_key": source_key, "source_type": "github_trending",
            "source_label": source_label, "source_url": source_url}
    try:
        result = subprocess.run(
            ["curl", "-sL", "--compressed", "--max-time", "10", source_url],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(f"[GATHER] {source_label}: fetch failed")
            return {**base, "content": ""}

        content = parse_github_trending(
            result.stdout, cfg["min_stars_week"], cfg["ai_keywords"])
        if content:
            logger.info(f"[GATHER] {source_label}: OK")
        else:
            logger.info(f"[GATHER] {source_label}: no matching repos")
        return {**base, "content": content}
    except Exception as e:
        logger.warning(f"[GATHER] {source_label}: {e}")
        return {**base, "content": ""}


# ── Main gather function ────────────────────────────────────────────────────

def gather_all(work_dir: Path = None, sources_config: dict = None,
               data_root: Path = None) -> list[dict]:
    """Gather content from all sources concurrently.

    Args:
        work_dir: Directory for debug output files
        sources_config: Sources section from config.json. If None, looks for
                        sources.json in the old location (backward compat).
        data_root: Digest output directory (for last-success lookback).

    Returns list of dicts with source provenance:
        {source_key, source_type, source_label, source_url, content}.
    """
    if sources_config is None:
        # Backward compat: load from sources.json
        skill_dir = Path(__file__).parent.parent
        sources_file = skill_dir / "sources.json"
        secrets_file = skill_dir / "sources-secrets.env"
        load_secrets(secrets_file)
        sources_config = json.load(open(sources_file, encoding="utf-8"))

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
                futures[f] = f"twitter:{account}"

        # Newsletters
        if "newsletters" in sources_config:
            nl_sources = sources_config["newsletters"].get("sources", {})
            for key, nl in nl_sources.items():
                f = pool.submit(_fetch_newsletter, key, nl,
                                imap_host, imap_email, imap_password,
                                last_success=last_success)
                futures[f] = f"newsletter:{key}"

        # Blogs
        if "blogs" in sources_config:
            for key, blog in sources_config["blogs"].items():
                f = pool.submit(_fetch_blog, key, blog)
                futures[f] = f"blog:{key}"

        # Research (treated as blogs with distinct source_type)
        if "research" in sources_config:
            for key, blog in sources_config["research"].items():
                f = pool.submit(_fetch_blog, key, blog, source_type="research")
                futures[f] = f"research:{key}"

        # GitHub Trending
        if "github_trending" in sources_config:
            gh_cfg = sources_config["github_trending"]
            f = pool.submit(_fetch_github_trending, gh_cfg)
            futures[f] = "github_trending:trending"

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
            safe_key = r["source_key"].replace(":", "-")
            out = work_dir / f"raw-{safe_key}.txt"
            out.write_text(r.get("content", ""), encoding="utf-8")

    # Sort by source_key for deterministic ordering
    results.sort(key=lambda r: r["source_key"])

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
        print(f"  {r['source_key']}: {size} bytes [{status}]")
