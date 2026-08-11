"""Twitter account discovery.

Searches X/Twitter for AI-related keywords via the ``bird`` CLI, aggregates
the authors of matching tweets, ranks them by a composite score combining
follower count, posting frequency, and engagement, and (optionally) asks
an LLM to score each candidate's bio + tweet samples against the user's
stated preferences so noise like "make money with AI" influencers gets
pushed below the cut.

Runs independently via ``digest-pipeline --discover-twitter``, and is also
called from the weekly source audit so suggestions appear in the same
Telegram report.

Pure-Python; the external dependencies are the ``bird`` CLI (same one
``gather.py`` uses for the Twitter fetcher) and, when ``llm_filter`` is
enabled, the project's ``llm.py`` helper. Requires ``AUTH_TOKEN`` and
``CT0`` env vars for Twitter cookies, plus ``OPENROUTER_API_KEY`` or
``ANTHROPIC_API_KEY`` when LLM filtering is on.
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

from . import llm

logger = logging.getLogger("digest")

# ── Defaults (overridable per-digest in config.json) ────────────────────────

DEFAULT_TWEETS_PER_KEYWORD = 50
DEFAULT_MIN_FOLLOWERS = 5000
DEFAULT_MIN_POSTS_PER_WEEK = 1.0
DEFAULT_TOP_N = 10
DEFAULT_WEIGHTS = {"followers": 2.0, "post_frequency": 0.0, "engagement": 0.3, "llm_score": 2.0}

# Telegram message cap (matches source_audit.TELEGRAM_LENGTH_CAP).
TELEGRAM_LENGTH_CAP = 3800

# Per-keyword search timeout in seconds.
BIRD_TIMEOUT_S = 60

# Minimum span (days) used in posts-per-week estimation. Prevents a tight
# cluster of tweets from yielding a misleading >>1000/week rate when the
# author actually only posts in bursts.
MIN_SPAN_DAYS = 1.0

# Max tweet samples kept per author. Bounds the LLM prompt size.
MAX_TWEET_SAMPLES = 6

# Max tweet samples copied onto a Candidate (and into the JSON report).
# Smaller than MAX_TWEET_SAMPLES because the JSON output is read by humans
# in a slash command, not by the LLM.
CANDIDATE_TWEET_SAMPLES = 5

# LLM evaluation defaults (overridable per-digest in config.json).
DEFAULT_LLM_EVALUATE_TOP_N = 30
DEFAULT_LLM_MIN_RELEVANCE = 6

DEFAULT_LLM_PREFERENCES = """\
You are evaluating X (Twitter) accounts for a technical builder/developer who
uses LLMs and generative AI to BUILD software (not to research it).

PREFER (score 7-10): accounts focused on
- AI-enabled coding tools (Claude Code, Cursor, Aider, Copilot, etc.)
- LLM frameworks and SDKs (LangChain, LlamaIndex, MCP, agent frameworks)
- AI infrastructure announcements (eval libraries, vector DBs, retrieval, deployment)
- Applied AI engineering, framework releases, shipping with LLMs
- Deep technical posts about building with generative AI

PENALIZE (score 0-3): accounts focused on
- "Make money with AI" / monetization / passive income hype
- Non-technical productivity / AI for entrepreneurs / business influencers
- Vibe coding / no-code AI influencer content
- Pure ML research, model training, fine-tuning research papers
- Data-science research (not applied engineering)
- Off-topic accounts (sports, politics, general news, finance, lifestyle)

NEUTRAL (4-6): general AI commentary, mixed content, unclear focus, or
accounts that occasionally cover relevant topics but aren't primarily about
them."""


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
    llm_score: Optional[int] = None    # 0-10, None if filter disabled or eval failed
    llm_rationale: str = ""
    tweet_samples: list[str] = field(default_factory=list)  # up to CANDIDATE_TWEET_SAMPLES


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

    def to_dict(self) -> dict:
        """JSON-serializable representation for the slash command pipe."""
        return {
            "digest_name": self.digest_name,
            "digest_slug": self.digest_slug,
            "date": self.date,
            "enabled": self.enabled,
            "keywords_searched": list(self.keywords_searched),
            "skipped_existing": self.skipped_existing,
            "errors": list(self.errors),
            "candidates": [
                {
                    "handle": c.handle,
                    "name": c.name,
                    "bio": c.bio,
                    "followers": c.followers,
                    "posts_per_week": c.posts_per_week,
                    "avg_engagement": c.avg_engagement,
                    "score": c.score,
                    "matched_keywords": list(c.matched_keywords),
                    "sample_tweets": c.sample_tweets,
                    "llm_score": c.llm_score,
                    "llm_rationale": c.llm_rationale,
                    "tweet_samples": list(c.tweet_samples),
                }
                for c in self.candidates
            ],
        }


# ── Internal author accumulator ─────────────────────────────────────────────

@dataclass
class _Author:
    handle: str
    name: str = ""
    bio: str = ""
    followers: int = 0
    tweet_times: list[datetime] = field(default_factory=list)
    tweet_texts: list[str] = field(default_factory=list)
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

    ranked = _rank(authors, existing, min_followers, min_posts_per_week, weights)
    report.skipped_existing = sum(
        1 for handle in authors if handle.lower() in existing
    )

    llm_cfg = disc_cfg.get("llm_filter") or {}
    if llm_cfg.get("enabled"):
        provider = (config.get("llm") or {}).get("provider", "openrouter")
        preferences = llm_cfg.get("preferences") or DEFAULT_LLM_PREFERENCES
        eval_top_n = int(llm_cfg.get("evaluate_top_n", DEFAULT_LLM_EVALUATE_TOP_N))
        min_relevance = int(llm_cfg.get("min_relevance", DEFAULT_LLM_MIN_RELEVANCE))
        ranked = _llm_filter(
            ranked, authors, preferences, provider, eval_top_n, min_relevance, weights, report
        )

    report.candidates = ranked[:top_n]
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

    text = (tweet.get("text") or "").strip()
    if text and len(author.tweet_texts) < MAX_TWEET_SAMPLES:
        author.tweet_texts.append(text)


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
) -> list[Candidate]:
    """Rule-filter and composite-rank authors. Caller truncates."""
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
            tweet_samples=list(author.tweet_texts[:CANDIDATE_TWEET_SAMPLES]),
        ))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


# ── LLM-based relevance filter ─────────────────────────────────────────────

def _llm_filter(
    candidates: list[Candidate],
    authors: dict[str, _Author],
    preferences: str,
    provider: str,
    evaluate_top_n: int,
    min_relevance: int,
    weights: dict,
    report: DiscoveryReport,
) -> list[Candidate]:
    """Ask the LLM to score each top candidate's relevance to preferences.

    Mutates ``Candidate.llm_score`` / ``llm_rationale`` on each scored item.
    Drops candidates below ``min_relevance`` and re-sorts by a blended
    score: ``composite + weights['llm_score'] * llm_score``.
    """
    if not candidates:
        return candidates

    llm.configure(provider)
    pool = candidates[:evaluate_top_n]
    failures = 0
    for c in pool:
        author = authors.get(c.handle.lower())
        samples = author.tweet_texts if author else []
        try:
            score, rationale = _llm_evaluate(c, samples, preferences, provider)
            c.llm_score = score
            c.llm_rationale = rationale
        except Exception as e:
            failures += 1
            logger.warning(f"[DISCOVER] LLM eval failed for @{c.handle}: {e}")
            c.llm_score = None
            c.llm_rationale = f"eval error: {e}"

    if failures:
        report.errors.append(f"{failures} LLM evaluation(s) failed")

    w_llm = float(weights.get("llm_score", 0.0))
    kept = [c for c in pool if c.llm_score is not None and c.llm_score >= min_relevance]
    kept.sort(key=lambda c: c.score + w_llm * (c.llm_score or 0), reverse=True)
    return kept


def _llm_evaluate(
    candidate: Candidate,
    tweet_samples: list[str],
    preferences: str,
    provider: str,
) -> tuple[int, str]:
    """Single LLM call. Returns (score 0-10, one-line rationale)."""
    samples_block = "\n".join(f"- {t[:280]}" for t in tweet_samples) or "(no samples)"
    prompt = (
        f"{preferences}\n\n"
        "Evaluate this account against the preferences above.\n\n"
        f"Handle: @{candidate.handle}\n"
        f"Name: {candidate.name}\n"
        f"Bio: {candidate.bio or '(empty)'}\n"
        f"Followers: {candidate.followers}\n"
        f"Matched keywords: {', '.join(candidate.matched_keywords)}\n\n"
        f"Recent tweets:\n{samples_block}\n\n"
        "Return JSON only (no markdown, no extra text):\n"
        '{"score": <integer 0-10>, "rationale": "<one short sentence>"}'
    )
    model = llm.model_for("discovery")
    messages = [{"role": "user", "content": prompt}]
    text, _usage = llm.chat(messages, model, max_tokens=200)
    data = _extract_json_object(text)
    score = int(data.get("score", 0))
    score = max(0, min(10, score))
    rationale = str(data.get("rationale", "")).strip()
    return score, rationale


def _extract_json_object(text: str) -> dict:
    """Parse a JSON object from a possibly-fenced LLM response."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in: {text[:200]}")
    return json.loads(text[start:end + 1])


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
        header = (
            f"• `@{c.handle}` — {followers_str} followers, "
            f"{c.posts_per_week:.1f}/wk on {', '.join(c.matched_keywords)}"
        )
        if c.llm_score is not None:
            header += f" [LLM: {c.llm_score}/10]"
        lines.append(header)
        if c.llm_rationale:
            rationale = c.llm_rationale.replace("\n", " ").strip()
            if len(rationale) > 160:
                rationale = rationale[:157] + "…"
            lines.append(f"    _{rationale}_")
        elif bio:
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
