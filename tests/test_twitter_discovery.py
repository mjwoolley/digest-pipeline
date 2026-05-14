"""Tests for digest_pipeline.twitter_discovery."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from digest_pipeline import twitter_discovery
from digest_pipeline.twitter_discovery import (
    Candidate,
    DiscoveryReport,
    _Author,
    _ingest_tweet,
    _parse_twitter_time,
    _posts_per_week,
    _rank,
    discover,
    format_telegram,
)


# ── Helpers / fixtures ─────────────────────────────────────────────────────

def _tweet(handle: str, *, followers: int, bio: str = "",
           created_at: str = "Thu May 14 12:00:00 +0000 2026",
           likes: int = 0, rts: int = 0, replies: int = 0,
           name: str = "Name") -> dict:
    """Build a bird --json-full-shaped tweet dict for one author."""
    return {
        "id": "1",
        "text": "hello",
        "createdAt": created_at,
        "likeCount": likes,
        "retweetCount": rts,
        "replyCount": replies,
        "author": {"username": handle, "name": name},
        "_raw": {
            "core": {
                "user_results": {
                    "result": {
                        "legacy": {
                            "followers_count": followers,
                            "description": bio,
                        },
                        "core": {"name": name, "screen_name": handle},
                    }
                }
            }
        },
    }


def _cfg(*, accounts=None, enabled=True, keywords=("AI",), **disc_overrides) -> dict:
    return {
        "digest": {"name": "Test Digest"},
        "_data_root": Path("/tmp/test-ai"),
        "sources": {
            "twitter": {
                "accounts": list(accounts or []),
                "discovery": {
                    "enabled": enabled,
                    "keywords": list(keywords),
                    "min_followers": 1000,
                    "min_posts_per_week": 0.1,
                    "top_n": 10,
                    **disc_overrides,
                },
            }
        },
    }


# ── Pure-function tests ────────────────────────────────────────────────────

def test_parse_twitter_time():
    t = _parse_twitter_time("Thu May 14 12:52:25 +0000 2026")
    assert t == datetime(2026, 5, 14, 12, 52, 25, tzinfo=timezone.utc)


def test_parse_twitter_time_bad_input():
    assert _parse_twitter_time(None) is None
    assert _parse_twitter_time("") is None
    assert _parse_twitter_time("not a date") is None


def test_posts_per_week_single_tweet():
    t = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert _posts_per_week([t]) == pytest.approx(7.0)


def test_posts_per_week_seven_days_spread():
    times = [datetime(2026, 5, 1, tzinfo=timezone.utc),
             datetime(2026, 5, 8, tzinfo=timezone.utc)]
    # 2 tweets across 7 days → 2 per week.
    assert _posts_per_week(times) == pytest.approx(2.0)


def test_ingest_tweet_merges_duplicates():
    authors: dict[str, _Author] = {}
    _ingest_tweet(_tweet("ada", followers=5000, bio="hello", likes=10), "AI", authors)
    _ingest_tweet(_tweet("ada", followers=5100, bio="", likes=5,
                          created_at="Thu May 21 12:00:00 +0000 2026"),
                  "LLM", authors)
    assert "ada" in authors
    a = authors["ada"]
    assert a.followers == 5100  # takes max
    assert a.bio == "hello"     # retains non-empty
    assert a.matched_keywords == {"AI", "LLM"}
    assert a.engagement_total == 15
    assert len(a.tweet_times) == 2
    assert len(a.tweet_texts) == 2  # captures tweet text for LLM eval


def test_ingest_tweet_caps_samples():
    authors: dict[str, _Author] = {}
    for i in range(twitter_discovery.MAX_TWEET_SAMPLES + 5):
        t = _tweet("ada", followers=5000,
                    created_at=f"Thu May {1 + (i % 28):02d} 12:00:00 +0000 2026")
        t["text"] = f"tweet {i}"
        _ingest_tweet(t, "AI", authors)
    assert len(authors["ada"].tweet_texts) == twitter_discovery.MAX_TWEET_SAMPLES


def test_ingest_tweet_skips_missing_handle():
    authors: dict[str, _Author] = {}
    t = _tweet("ada", followers=5000)
    t["author"] = {}
    _ingest_tweet(t, "AI", authors)
    assert authors == {}


def test_rank_filters_existing_handles():
    authors = {
        "ada": _Author("ada", followers=10000, tweet_times=[
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 8, tzinfo=timezone.utc),
        ], matched_keywords={"AI"}),
        "babbage": _Author("babbage", followers=10000, tweet_times=[
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 8, tzinfo=timezone.utc),
        ], matched_keywords={"AI"}),
    }
    existing = {"ada"}
    out = _rank(authors, existing, min_followers=1000, min_posts_per_week=0.1,
                weights=twitter_discovery.DEFAULT_WEIGHTS)
    handles = [c.handle for c in out]
    assert handles == ["babbage"]


def test_rank_filters_min_followers_and_min_posts():
    authors = {
        "low_followers": _Author("low_followers", followers=500, tweet_times=[
            datetime(2026, 5, 1, tzinfo=timezone.utc),
        ]),
        "infrequent": _Author("infrequent", followers=10000, tweet_times=[
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ]),
        # 1 tweet in a tight window will hit the single-tweet 7/wk default.
        # Use 2 widely-spaced tweets to actually fail min_posts_per_week.
        "infrequent2": _Author("infrequent2", followers=10000, tweet_times=[
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 1, tzinfo=timezone.utc),
        ]),
        "good": _Author("good", followers=20000, tweet_times=[
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 8, tzinfo=timezone.utc),
        ]),
    }
    out = _rank(authors, set(), min_followers=1000, min_posts_per_week=1.0,
                weights=twitter_discovery.DEFAULT_WEIGHTS)
    handles = [c.handle for c in out]
    assert "low_followers" not in handles
    assert "infrequent2" not in handles
    assert "good" in handles


def test_rank_sorts_by_score():
    base_times = [
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 8, tzinfo=timezone.utc),
    ]
    authors = {
        f"a{i}": _Author(f"a{i}", followers=1000 * (i + 1),
                          tweet_times=base_times, matched_keywords={"AI"})
        for i in range(5)
    }
    out = _rank(authors, set(), min_followers=100, min_posts_per_week=0.1,
                weights=twitter_discovery.DEFAULT_WEIGHTS)
    # Highest-follower account should win on the log10 follower term.
    assert out[0].handle == "a4"
    assert out[1].handle == "a3"
    assert len(out) == 5  # _rank no longer truncates; caller does


# ── discover() end-to-end ──────────────────────────────────────────────────

def test_discover_disabled_returns_empty():
    cfg = _cfg(enabled=False)
    report = discover(cfg)
    assert report.enabled is False
    assert report.candidates == []


def test_discover_no_keywords_returns_empty():
    cfg = _cfg(keywords=())
    report = discover(cfg)
    assert report.candidates == []
    assert report.keywords_searched == []


def test_discover_missing_credentials_reports_error(monkeypatch):
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CT0", raising=False)
    cfg = _cfg()
    report = discover(cfg)
    assert report.candidates == []
    assert any("AUTH_TOKEN" in e for e in report.errors)


def test_discover_end_to_end_with_mocked_bird(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "fake")
    monkeypatch.setenv("CT0", "fake")
    cfg = _cfg(
        accounts=["karpathy"],  # should be filtered out as existing
        keywords=("AI",),
        min_followers=1000,
        min_posts_per_week=0.1,
    )
    fake_tweets = [
        _tweet("karpathy", followers=900_000,
               created_at="Thu May 14 12:00:00 +0000 2026", likes=100),
        _tweet("newuser", followers=50_000, bio="builds AI agents",
               created_at="Thu May 14 12:00:00 +0000 2026", likes=200),
        _tweet("newuser", followers=50_000, bio="builds AI agents",
               created_at="Thu May 21 12:00:00 +0000 2026", likes=300),
        _tweet("smaller", followers=500, bio="too small",
               created_at="Thu May 14 12:00:00 +0000 2026"),
    ]

    def fake_search(keyword, n, auth_token, ct0):
        assert keyword == "AI"
        return fake_tweets

    monkeypatch.setattr(twitter_discovery, "_search_keyword", fake_search)
    report = discover(cfg)

    handles = [c.handle for c in report.candidates]
    assert "karpathy" not in handles      # filtered out as already-tracked
    assert "smaller" not in handles       # below min_followers
    assert "newuser" in handles
    new = next(c for c in report.candidates if c.handle == "newuser")
    assert new.followers == 50_000
    assert new.bio == "builds AI agents"
    assert new.avg_engagement == 250.0   # (200 + 300) / 2
    assert new.matched_keywords == ["AI"]
    assert new.sample_tweets == 2


def test_discover_continues_when_one_keyword_fails(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "fake")
    monkeypatch.setenv("CT0", "fake")
    cfg = _cfg(keywords=("good", "bad"), min_followers=1000, min_posts_per_week=0.1)

    def fake_search(keyword, n, auth_token, ct0):
        if keyword == "bad":
            raise RuntimeError("bird timeout")
        return [_tweet("alice", followers=10_000,
                       created_at="Thu May 14 12:00:00 +0000 2026")]

    monkeypatch.setattr(twitter_discovery, "_search_keyword", fake_search)
    report = discover(cfg)
    assert any("bad" in e for e in report.errors)
    assert [c.handle for c in report.candidates] == ["alice"]


# ── format_telegram ────────────────────────────────────────────────────────

def test_format_telegram_disabled_returns_empty():
    report = DiscoveryReport(digest_name="X", digest_slug="x", date="2026-05-14",
                              enabled=False)
    assert format_telegram(report) == ""


def test_format_telegram_no_candidates():
    report = DiscoveryReport(digest_name="X", digest_slug="x", date="2026-05-14",
                              enabled=True, keywords_searched=["AI"])
    msg = format_telegram(report)
    assert "Suggested new Twitter accounts" in msg
    assert "No new candidates" in msg


def test_format_telegram_lists_candidates():
    report = DiscoveryReport(
        digest_name="AI Digest", digest_slug="ai", date="2026-05-14", enabled=True,
        keywords_searched=["LLM"],
        candidates=[
            Candidate(handle="ada", name="Ada", bio="builds things",
                       followers=12345, posts_per_week=3.5, avg_engagement=42.0,
                       score=10.0, matched_keywords=["LLM"], sample_tweets=5),
        ],
        skipped_existing=2,
    )
    msg = format_telegram(report)
    assert "@ada" in msg
    assert "12.3k" in msg            # formatted followers
    assert "3.5/wk" in msg
    assert "builds things" in msg
    assert "Skipped 2" in msg


def test_format_telegram_shows_llm_rationale_when_present():
    report = DiscoveryReport(
        digest_name="AI", digest_slug="ai", date="2026-05-14", enabled=True,
        candidates=[
            Candidate(handle="ada", name="Ada", bio="should not show",
                       followers=12345, posts_per_week=3.5, avg_engagement=42.0,
                       score=10.0, matched_keywords=["LLM"], sample_tweets=5,
                       llm_score=9, llm_rationale="builds with Claude Code"),
        ],
    )
    msg = format_telegram(report)
    assert "[LLM: 9/10]" in msg
    assert "builds with Claude Code" in msg
    assert "should not show" not in msg  # bio yields to rationale


# ── LLM filter integration ────────────────────────────────────────────────

def test_llm_filter_disabled_passes_through(monkeypatch):
    """When llm_filter.enabled is false, candidates aren't LLM-scored."""
    monkeypatch.setenv("AUTH_TOKEN", "fake")
    monkeypatch.setenv("CT0", "fake")
    cfg = _cfg(keywords=("AI",), min_followers=1000, min_posts_per_week=0.1)
    cfg["sources"]["twitter"]["discovery"]["llm_filter"] = {"enabled": False}

    fake_tweets = [_tweet("ada", followers=50_000,
                            created_at="Thu May 14 12:00:00 +0000 2026")]
    monkeypatch.setattr(twitter_discovery, "_search_keyword",
                          lambda *a, **k: fake_tweets)
    report = discover(cfg)
    assert len(report.candidates) == 1
    assert report.candidates[0].llm_score is None


def test_llm_filter_drops_below_min_relevance(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "fake")
    monkeypatch.setenv("CT0", "fake")
    cfg = _cfg(keywords=("AI",), min_followers=1000, min_posts_per_week=0.1)
    cfg["sources"]["twitter"]["discovery"]["llm_filter"] = {
        "enabled": True, "min_relevance": 6, "evaluate_top_n": 10,
    }
    fake_tweets = [
        _tweet("builder", followers=20_000,
                created_at="Thu May 14 12:00:00 +0000 2026"),
        _tweet("influencer", followers=80_000,
                created_at="Thu May 14 12:00:00 +0000 2026"),
    ]
    monkeypatch.setattr(twitter_discovery, "_search_keyword",
                          lambda *a, **k: fake_tweets)

    # Mock the LLM: high score for "builder", low score for "influencer".
    scores = {"builder": (8, "ships with Claude Code"),
              "influencer": (2, "make money with AI")}

    def fake_eval(candidate, samples, preferences, provider):
        return scores[candidate.handle]

    monkeypatch.setattr(twitter_discovery, "_llm_evaluate", fake_eval)
    # Stub configure so we don't need API keys.
    monkeypatch.setattr(twitter_discovery.llm, "configure", lambda p: None)

    report = discover(cfg)
    handles = [c.handle for c in report.candidates]
    assert "builder" in handles
    assert "influencer" not in handles


def test_llm_filter_reranks_by_llm_score(monkeypatch):
    """Composite would rank A > B (more followers); LLM flips it."""
    monkeypatch.setenv("AUTH_TOKEN", "fake")
    monkeypatch.setenv("CT0", "fake")
    cfg = _cfg(keywords=("AI",), min_followers=1000, min_posts_per_week=0.1)
    cfg["sources"]["twitter"]["discovery"]["llm_filter"] = {
        "enabled": True, "min_relevance": 0, "evaluate_top_n": 10,
    }
    fake_tweets = [
        _tweet("biggest", followers=500_000,
                created_at="Thu May 14 12:00:00 +0000 2026"),
        _tweet("better_fit", followers=10_000,
                created_at="Thu May 14 12:00:00 +0000 2026"),
    ]
    monkeypatch.setattr(twitter_discovery, "_search_keyword",
                          lambda *a, **k: fake_tweets)
    monkeypatch.setattr(twitter_discovery, "_llm_evaluate",
                          lambda c, s, p, pr: (9 if c.handle == "better_fit" else 4,
                                                "n/a"))
    monkeypatch.setattr(twitter_discovery.llm, "configure", lambda p: None)
    report = discover(cfg)
    assert [c.handle for c in report.candidates] == ["better_fit", "biggest"]


def test_llm_filter_handles_eval_exception(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "fake")
    monkeypatch.setenv("CT0", "fake")
    cfg = _cfg(keywords=("AI",), min_followers=1000, min_posts_per_week=0.1)
    cfg["sources"]["twitter"]["discovery"]["llm_filter"] = {
        "enabled": True, "min_relevance": 0, "evaluate_top_n": 10,
    }
    fake_tweets = [_tweet("crashy", followers=10_000,
                            created_at="Thu May 14 12:00:00 +0000 2026")]
    monkeypatch.setattr(twitter_discovery, "_search_keyword",
                          lambda *a, **k: fake_tweets)

    def boom(*a, **k):
        raise RuntimeError("upstream 500")
    monkeypatch.setattr(twitter_discovery, "_llm_evaluate", boom)
    monkeypatch.setattr(twitter_discovery.llm, "configure", lambda p: None)
    report = discover(cfg)
    # Crashed eval → dropped from final list, error recorded.
    assert report.candidates == []
    assert any("LLM evaluation" in e for e in report.errors)


def test_extract_json_object_handles_fenced_response():
    from digest_pipeline.twitter_discovery import _extract_json_object
    text = '```json\n{"score": 7, "rationale": "good"}\n```'
    assert _extract_json_object(text) == {"score": 7, "rationale": "good"}


def test_extract_json_object_finds_json_inside_prose():
    from digest_pipeline.twitter_discovery import _extract_json_object
    text = 'Sure, here is my answer: {"score": 3, "rationale": "x"} thanks!'
    assert _extract_json_object(text) == {"score": 3, "rationale": "x"}


# ── Candidate.tweet_samples / DiscoveryReport.to_dict ─────────────────────

def test_rank_populates_tweet_samples_from_author(monkeypatch):
    """_rank should copy up to CANDIDATE_TWEET_SAMPLES texts onto each Candidate."""
    base_times = [datetime(2026, 5, 1, tzinfo=timezone.utc),
                  datetime(2026, 5, 8, tzinfo=timezone.utc)]
    author = _Author("ada", followers=10_000, tweet_times=base_times,
                     matched_keywords={"AI"})
    # Force more texts than the cap to verify truncation.
    author.tweet_texts = [f"sample {i}" for i in range(10)]
    out = _rank({"ada": author}, set(), min_followers=100, min_posts_per_week=0.1,
                weights=twitter_discovery.DEFAULT_WEIGHTS)
    assert len(out) == 1
    assert len(out[0].tweet_samples) == twitter_discovery.CANDIDATE_TWEET_SAMPLES
    # First N texts kept in order.
    assert out[0].tweet_samples == [
        f"sample {i}" for i in range(twitter_discovery.CANDIDATE_TWEET_SAMPLES)
    ]


def test_discover_candidate_tweet_samples_populated(monkeypatch):
    """End-to-end: a Candidate from discover() carries the author's tweet texts."""
    monkeypatch.setenv("AUTH_TOKEN", "fake")
    monkeypatch.setenv("CT0", "fake")
    cfg = _cfg(keywords=("AI",), min_followers=1000, min_posts_per_week=0.1)
    cfg["sources"]["twitter"]["discovery"]["llm_filter"] = {"enabled": False}

    t1 = _tweet("ada", followers=20_000,
                created_at="Thu May 14 12:00:00 +0000 2026")
    t1["text"] = "first tweet about LLMs"
    t2 = _tweet("ada", followers=20_000,
                created_at="Thu May 21 12:00:00 +0000 2026")
    t2["text"] = "second tweet about agents"
    monkeypatch.setattr(twitter_discovery, "_search_keyword",
                        lambda *a, **k: [t1, t2])
    report = discover(cfg)
    assert len(report.candidates) == 1
    c = report.candidates[0]
    assert c.tweet_samples == ["first tweet about LLMs",
                               "second tweet about agents"]


def test_discovery_report_to_dict_round_trips_through_json():
    """to_dict() must be json.dumps-able and contain the candidate fields."""
    report = DiscoveryReport(
        digest_name="AI Digest", digest_slug="ai", date="2026-05-14", enabled=True,
        keywords_searched=["AI"], skipped_existing=2, errors=["one failed"],
        candidates=[
            Candidate(
                handle="ada", name="Ada", bio="builds things",
                followers=12345, posts_per_week=3.5, avg_engagement=42.0,
                score=10.0, matched_keywords=["AI"], sample_tweets=5,
                llm_score=8, llm_rationale="ships things",
                tweet_samples=["tweet a", "tweet b"],
            ),
        ],
    )
    d = report.to_dict()
    payload = json.dumps(d)
    parsed = json.loads(payload)
    assert parsed["digest_slug"] == "ai"
    assert parsed["skipped_existing"] == 2
    assert parsed["errors"] == ["one failed"]
    assert len(parsed["candidates"]) == 1
    c = parsed["candidates"][0]
    assert c["handle"] == "ada"
    assert c["tweet_samples"] == ["tweet a", "tweet b"]
    assert c["llm_score"] == 8
    assert c["matched_keywords"] == ["AI"]


def test_discovery_report_to_dict_omits_internal_author_state():
    """The JSON payload should not leak the internal _Author dict from discover()."""
    report = DiscoveryReport(
        digest_name="x", digest_slug="x", date="2026-05-14", enabled=True,
        candidates=[
            Candidate(handle="ada", name="Ada", bio="", followers=1,
                      posts_per_week=1.0, avg_engagement=0.0, score=0.0,
                      matched_keywords=[], sample_tweets=0),
        ],
    )
    d = report.to_dict()
    json.dumps(d)  # must not raise
    assert "_Author" not in str(d)
    assert "authors" not in d
