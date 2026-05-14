"""Twitter account discovery.

Searches X/Twitter for AI-related keywords via the ``bird`` CLI, aggregates
the authors of matching tweets, ranks them by a composite score combining
follower count, posting frequency, and engagement, and suggests the top N
accounts that are not already in the digest's configured handle list.

Runs independently via ``digest-pipeline --discover-twitter``, and is also
called from the weekly source audit so suggestions appear in the same
Telegram report.

Pure-Python with one external dependency: the ``bird`` CLI (same one
``gather.py`` uses for the Twitter fetcher). Requires ``AUTH_TOKEN`` and
``CT0`` env vars (Twitter session cookies).
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timezone
from typing import Optional

logger = logging.getLogger("digest")

# ── Defaults (overridable per-digest in config.json) ────────────────────────

DEFAULT_TWEETS_PER_KEYWORD = 50
DEFAULT_MIN_FOLLOWERS = 5000
DEFAULT_MIN_POSTS_PER_WEEK = 1.0
DEFAULT_TOP_N = 10
DEFAULT_WEIGHTS = {"followers": 1.0, "post_frequency": 0.5, "engagement": 0.3}

# Telegram message cap (matches source_audit.TELEGRAM_LENGTH_CAP).
TELEGRAM_LENGTH_CAP = 3800

# Per-keyword search timeout in seconds.
BIRD_TIMEOUT_S = 60

# Minimum span (days) used in posts-per-week estimation. Prevents a tight
# cluster of tweets from yielding a misleading >>1000/week rate when the
# author actually only posts in bursts.
MIN_SPAN_DAYS = 1.0


# ── Data classes ───────────────────────────────────────────────────────────

@dataclass
class Candidate:
    handle: str            # screen_name (no @)
    name: str
    bio: str
    followers: int
    posts_per_week: float  # estimated from keyword-matching tweets
    avg_engagement: float  # mean (likes + RTs + replies) per tweet seen
    score: float
    matched_keywords: list[str]
    sample_tweets: int     # how many tweets we aggregated for this author


@dataclass
class DiscoveryReport:
    digest_name: str
    digest_slug: str
    date: str
    candidates: list[Candidate] = field(default_factory=list)
    keywords_searched: list[str] = field(default_factory=list)
    skipped_existing: int = 0
    enabled: bool = True
    errors: list[str] = field(default_factory=list)


# ── Internal author accumulator ─────────────────────────────────────────────

@dataclass
class _Author:
    handle: str
    name: str = ""
    bio: str = ""
    followers: int = 0
    tweet_times: list[datetime] = field(default_factory=list)
    engagement_total: int = 0
    matched_keywords: set[str] = field(default_factory=set)


# ── Public entry point ─────────────────────────────────────────────────────

def discover(config: dict) -> DiscoveryReport:
    """Search Twitter for popular AI accounts and rank candidates.

    Pure-Python; the only side-effect is invoking the ``bird`` CLI per
    keyword. Reads config from ``config["sources"]["twitter"]["discovery"]``.
    """
    digest_name = config.get("digest", {}).get("name", "Digest")
    data_root = config.get("_data_root")
    digest_slug = data_root.name if data_root is not None else digest_name
    today_str = date_cls.today().isoformat()

    twitter_cfg = (config.get("sources") or {}).get("twitter") or {}
    disc_cfg = twitter_cfg.get("discovery") or {}

    enabled = bool(disc_cfg.get("enabled", False))
    keywords = list(disc_cfg.get("keywords") or [])

    report = DiscoveryReport(
        digest_name=digest_name,
        digest_slug=digest_slug,
        date=today_str,
        enabled=enabled,
        keywords_searched=keywords,
    )

    if not enabled or not keywords:
        return report

    tweets_per_keyword = int(disc_cfg.get("tweets_per_keyword", DEFAULT_TWEETS_PER_KEYWORD))
    min_followers = int(disc_cfg.get("min_followers", DEFAULT_MIN_FOLLOWERS))
    min_posts_per_week = float(disc_cfg.get("min_posts_per_week", DEFAULT_MIN_POSTS_PER_WEEK))
    top_n = int(disc_cfg.get("top_n", DEFAULT_TOP_N))
    weights = {**DEFAULT_WEIGHTS, **(disc_cfg.get("score_weights") or {})}

    auth_token = os.environ.get("AUTH_TOKEN")
    ct0 = os.environ.get("CT0")
    if not auth_token or not ct0:
        report.errors.append("AUTH_TOKEN and CT0 not set; cannot query bird CLI")
        return report

    existing = {h.lower() for h in twitter_cfg.get("accounts") or []}

    authors: dict[str, _Author] = {}
    for kw in keywords:
        try:
            tweets = _search_keyword(kw, tweets_per_keyword, auth_token, ct0)
        except Exception as e:
            logger.warning(f"[DISCOVER] keyword {kw!r} failed: {e}")
            report.errors.append(f"keyword {kw!r}: {e}")
            continue
        for tweet in tweets:
            _ingest_tweet(tweet, kw, authors)

    candidates = _rank(authors, existing, min_followers, min_posts_per_week, weights, top_n)
    report.candidates = candidates
    report.skipped_existing = sum(
        1 for handle in authors if handle.lower() in existing
    )
    return report


# ── Bird CLI invocation ────────────────────────────────────────────────────

def _search_keyword(keyword: str, n: int, auth_token: str, ct0: str) -> list[dict]:
    """Run ``bird search <keyword> --json-full -n <n>`` and return tweet list.

    Mirrors the subprocess pattern from ``gather.py:_fetch_twitter``.
    """
    env = {**os.environ, "AUTH_TOKEN": auth_token, "CT0": ct0}
    try:
        result = subprocess.run(
            ["bird", "search", keyword, "--json-full", "-n", str(n)],
            capture_output=True, text=True, env=env, timeout=BIRD_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "'bird' CLI not found. Install with: npm install -g bird "
            "(see https://github.com/nichochar/bird)"
        )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        raise RuntimeError(f"bird exit {result.returncode}: {stderr}")
    stdout = (result.stdout or "").strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"bird output not JSON: {e}")
    if not isinstance(data, list):
        raise RuntimeError(f"bird output is {type(data).__name__}, expected list")
    return data


# ── Tweet parsing & aggregation ────────────────────────────────────────────

def _ingest_tweet(tweet: dict, keyword: str, authors: dict[str, _Author]) -> None:
    """Pull the author from one bird --json-full tweet record and merge."""
    author_obj = tweet.get("author") or {}
    handle = (author_obj.get("username") or "").strip()
    if not handle:
        return

    raw = tweet.get("_raw") or {}
    user = (((raw.get("core") or {}).get("user_results") or {}).get("result") or {})
    legacy = user.get("legacy") or {}
    user_core = user.get("core") or {}

    followers = int(legacy.get("followers_count") or 0)
    bio = (legacy.get("description") or "").strip()
    name = (user_core.get("name") or author_obj.get("name") or "").strip()

    created_at = _parse_twitter_time(tweet.get("createdAt"))
    engagement = (
        int(tweet.get("likeCount") or 0)
        + int(tweet.get("retweetCount") or 0)
        + int(tweet.get("replyCount") or 0)
    )

    key = handle.lower()
    author = authors.get(key)
    if author is None:
        author = _Author(handle=handle, name=name, bio=bio, followers=followers)
        authors[key] = author
    else:
        if followers > author.followers:
            author.followers = followers
        if not author.bio and bio:
            author.bio = bio
        if not author.name and name:
            author.name = name

    if created_at is not None:
        author.tweet_times.append(created_at)
    author.engagement_total += engagement
    author.matched_keywords.add(keyword)


def _parse_twitter_time(s: Optional[str]) -> Optional[datetime]:
    """Parse Twitter's 'Thu May 14 12:52:25 +0000 2026' timestamp format."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# ── Ranking ────────────────────────────────────────────────────────────────

def _rank(
    authors: dict[str, _Author],
    existing: set[str],
    min_followers: int,
    min_posts_per_week: float,
    weights: dict,
    top_n: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for key, author in authors.items():
        if key in existing:
            continue
        if author.followers < min_followers:
            continue
        n_tweets = len(author.tweet_times)
        if n_tweets == 0:
            continue
        posts_per_week = _posts_per_week(author.tweet_times)
        if posts_per_week < min_posts_per_week:
            continue
        avg_engagement = author.engagement_total / max(1, n_tweets)
        score = (
            weights.get("followers", 1.0) * math.log10(max(1, author.followers))
            + weights.get("post_frequency", 0.5) * posts_per_week
            + weights.get("engagement", 0.3) * math.log10(max(1.0, avg_engagement))
        )
        candidates.append(Candidate(
            handle=author.handle,
            name=author.name,
            bio=author.bio,
            followers=author.followers,
            posts_per_week=posts_per_week,
            avg_engagement=avg_engagement,
            score=score,
            matched_keywords=sorted(author.matched_keywords),
            sample_tweets=n_tweets,
        ))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_n]


def _posts_per_week(times: list[datetime]) -> float:
    """Estimate posts-per-week from a list of (keyword-matching) tweet times.

    Note: this is the rate of *keyword-matching* tweets, not the account's
    overall post rate. That makes it a better signal for "active in this
    topic area" than a generic activity check.
    """
    if not times:
        return 0.0
    if len(times) == 1:
        return 1.0 / MIN_SPAN_DAYS * 7.0
    span_seconds = (max(times) - min(times)).total_seconds()
    span_days = max(MIN_SPAN_DAYS, span_seconds / 86400.0)
    return len(times) / span_days * 7.0


# ── Formatting ─────────────────────────────────────────────────────────────

def format_telegram(report: DiscoveryReport) -> str:
    """Render a DiscoveryReport as a Markdown block for Telegram."""
    if not report.enabled:
        return ""

    lines = [f"🔍 {report.digest_name} — Suggested new Twitter accounts ({report.date})"]

    if not report.candidates:
        if report.errors:
            lines.append("")
            lines.append(f"No candidates ({report.errors[0]}).")
        else:
            lines.append("")
            lines.append("No new candidates surfaced for the configured keywords.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"Searched: {', '.join(report.keywords_searched)}")
    if report.skipped_existing:
        lines.append(f"Skipped {report.skipped_existing} already-tracked handle(s).")
    lines.append("")

    for c in report.candidates:
        followers_str = _fmt_followers(c.followers)
        bio = c.bio.replace("\n", " ").strip()
        if len(bio) > 120:
            bio = bio[:117] + "…"
        lines.append(
            f"• `@{c.handle}` — {followers_str} followers, "
            f"{c.posts_per_week:.1f}/wk on {', '.join(c.matched_keywords)}"
        )
        if bio:
            lines.append(f"    _{bio}_")

    if report.errors:
        lines.append("")
        lines.append(f"({len(report.errors)} keyword(s) failed)")

    msg = "\n".join(lines)
    if len(msg) > TELEGRAM_LENGTH_CAP:
        msg = msg[: TELEGRAM_LENGTH_CAP - 20] + "\n…(truncated)"
    return msg


def _fmt_followers(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)
