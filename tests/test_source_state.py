"""Tests for digest_pipeline.source_state — incremental source processing."""
import json
from pathlib import Path

from digest_pipeline.source_state import (
    load_state,
    save_state,
    prune_state,
    extract_and_filter,
    filter_gathered_sources,
    commit_pending,
    _extract_twitter_items,
    _extract_block_items,
    _extract_newsletter_id,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _twitter_source(content, account="testuser"):
    return {
        "source_key": f"twitter:{account}",
        "source_type": "twitter",
        "source_label": f"@{account}",
        "source_url": f"https://twitter.com/{account}",
        "content": content,
    }


def _blog_source(content, key="test_blog"):
    return {
        "source_key": f"blog:{key}",
        "source_type": "blog",
        "source_label": "Test Blog",
        "source_url": "https://example.com/feed",
        "content": content,
    }


def _github_source(content):
    return {
        "source_key": "github_trending:trending",
        "source_type": "github_trending",
        "source_label": "GitHub Trending",
        "source_url": "https://github.com/trending",
        "content": content,
    }


def _newsletter_source(content, key="test_nl"):
    return {
        "source_key": f"newsletter:{key}",
        "source_type": "newsletter",
        "source_label": "Test Newsletter",
        "source_url": "",
        "content": content,
    }


SAMPLE_TWEETS = (
    "@user (Test User):\n"
    "First tweet content\n"
    "📅 Mon Mar 18 10:00:00 +0000 2026\n"
    "🔗 https://x.com/user/status/111\n"
    "──────────────────────────────────────────────────\n\n"
    "@user (Test User):\n"
    "Second tweet content\n"
    "📅 Mon Mar 18 11:00:00 +0000 2026\n"
    "🔗 https://x.com/user/status/222\n"
    "──────────────────────────────────────────────────\n\n"
    "@user (Test User):\n"
    "Third tweet content\n"
    "📅 Mon Mar 18 12:00:00 +0000 2026\n"
    "🔗 https://x.com/user/status/333"
)

SAMPLE_BLOG = (
    "TITLE: Article One\n"
    "LINK: https://example.com/post-1\n"
    "Description of article one\n"
    "---\n"
    "TITLE: Article Two\n"
    "LINK: https://example.com/post-2\n"
    "Description of article two\n"
    "---\n"
    "TITLE: Article Three\n"
    "LINK: https://example.com/post-3\n"
    "Description of article three"
)


# ── Twitter item extraction ──────────────────────────────────────────────────

def test_extract_twitter_items():
    items = _extract_twitter_items(SAMPLE_TWEETS)
    assert len(items) == 3
    assert items[0][0] == "https://x.com/user/status/111"
    assert items[1][0] == "https://x.com/user/status/222"
    assert items[2][0] == "https://x.com/user/status/333"


def test_extract_twitter_items_empty():
    assert _extract_twitter_items("") == []
    assert _extract_twitter_items("   ") == []


def test_extract_twitter_items_no_url_fallback():
    content = "Some tweet without a URL link"
    items = _extract_twitter_items(content)
    assert len(items) == 1
    assert items[0][0].startswith("hash:")


# ── Block item extraction ────────────────────────────────────────────────────

def test_extract_block_items():
    items = _extract_block_items(SAMPLE_BLOG)
    assert len(items) == 3
    assert items[0][0] == "https://example.com/post-1"
    assert items[1][0] == "https://example.com/post-2"
    assert items[2][0] == "https://example.com/post-3"


def test_extract_block_items_empty():
    assert _extract_block_items("") == []


def test_extract_block_items_no_link_fallback():
    content = "TITLE: No Link Article\nSome content\n---\nTITLE: Another\nMore"
    items = _extract_block_items(content)
    assert len(items) == 2
    assert all(item_id.startswith("hash:") for item_id, _ in items)


# ── Newsletter ID extraction ────────────────────────────────────────────────

def test_extract_newsletter_id():
    nl_id = _extract_newsletter_id("Hello newsletter content here")
    assert nl_id is not None
    assert nl_id.startswith("hash:")


def test_extract_newsletter_id_empty():
    assert _extract_newsletter_id("") is None
    assert _extract_newsletter_id("   ") is None


def test_extract_newsletter_id_deterministic():
    content = "Same content"
    assert _extract_newsletter_id(content) == _extract_newsletter_id(content)


def test_extract_newsletter_id_different():
    assert _extract_newsletter_id("Content A") != _extract_newsletter_id("Content B")


# ── Twitter filtering ────────────────────────────────────────────────────────

def test_filter_twitter_no_seen():
    source = _twitter_source(SAMPLE_TWEETS)
    filtered, new_ids = extract_and_filter(source, set())
    assert len(new_ids) == 3
    assert "https://x.com/user/status/111" in new_ids
    assert filtered["content"]  # not empty


def test_filter_twitter_some_seen():
    source = _twitter_source(SAMPLE_TWEETS)
    seen = {"https://x.com/user/status/111", "https://x.com/user/status/333"}
    filtered, new_ids = extract_and_filter(source, seen)
    assert len(new_ids) == 1
    assert new_ids[0] == "https://x.com/user/status/222"
    assert "Second tweet" in filtered["content"]
    assert "First tweet" not in filtered["content"]
    assert "Third tweet" not in filtered["content"]


def test_filter_twitter_all_seen():
    source = _twitter_source(SAMPLE_TWEETS)
    seen = {
        "https://x.com/user/status/111",
        "https://x.com/user/status/222",
        "https://x.com/user/status/333",
    }
    filtered, new_ids = extract_and_filter(source, seen)
    assert new_ids == []
    assert filtered["content"] == ""


# ── Blog/RSS filtering ──────────────────────────────────────────────────────

def test_filter_blog_no_seen():
    source = _blog_source(SAMPLE_BLOG)
    filtered, new_ids = extract_and_filter(source, set())
    assert len(new_ids) == 3
    assert filtered["content"]


def test_filter_blog_some_seen():
    source = _blog_source(SAMPLE_BLOG)
    seen = {"https://example.com/post-1"}
    filtered, new_ids = extract_and_filter(source, seen)
    assert len(new_ids) == 2
    assert "https://example.com/post-1" not in new_ids
    assert "Article One" not in filtered["content"]
    assert "Article Two" in filtered["content"]
    assert "Article Three" in filtered["content"]


def test_filter_blog_all_seen():
    source = _blog_source(SAMPLE_BLOG)
    seen = {"https://example.com/post-1", "https://example.com/post-2",
            "https://example.com/post-3"}
    filtered, new_ids = extract_and_filter(source, seen)
    assert new_ids == []
    assert filtered["content"] == ""


# ── GitHub trending filtering ────────────────────────────────────────────────

def test_filter_github_trending():
    content = (
        "TITLE: owner/repo [Python]\n"
        "LINK: https://github.com/owner/repo\n"
        "Stars this week: 500\n"
        "Some description\n"
        "---\n"
        "TITLE: other/project [Rust]\n"
        "LINK: https://github.com/other/project\n"
        "Stars this week: 300\n"
        "Another description"
    )
    source = _github_source(content)
    seen = {"https://github.com/owner/repo"}
    filtered, new_ids = extract_and_filter(source, seen)
    assert len(new_ids) == 1
    assert new_ids[0] == "https://github.com/other/project"


# ── Newsletter filtering ────────────────────────────────────────────────────

def test_filter_newsletter_not_seen():
    source = _newsletter_source("Fresh newsletter content")
    filtered, new_ids = extract_and_filter(source, set())
    assert len(new_ids) == 1
    assert filtered["content"] == "Fresh newsletter content"


def test_filter_newsletter_seen():
    content = "Same old newsletter"
    nl_id = _extract_newsletter_id(content)
    source = _newsletter_source(content)
    filtered, new_ids = extract_and_filter(source, {nl_id})
    assert new_ids == []
    assert filtered["content"] == ""


# ── State persistence ────────────────────────────────────────────────────────

def test_save_and_load_state(tmp_path):
    state = {"twitter:user": {"seen_ids": ["id1", "id2"], "last_updated": "2026-03-21"}}
    save_state(tmp_path, state)
    loaded = load_state(tmp_path)
    assert loaded == state


def test_load_state_missing_file(tmp_path):
    assert load_state(tmp_path) == {}


def test_load_state_corrupt_file(tmp_path):
    (tmp_path / ".source_state.json").write_text("not json{{{")
    assert load_state(tmp_path) == {}


# ── State pruning ────────────────────────────────────────────────────────────

def test_prune_state_keeps_recent():
    state = {
        "blog:a": {"seen_ids": ["x"], "last_updated": "2026-03-21"},
        "blog:b": {"seen_ids": ["y"], "last_updated": "2026-03-10"},
    }
    pruned = prune_state(state, max_age_days=7)
    assert "blog:a" in pruned
    assert "blog:b" not in pruned


def test_prune_state_no_date():
    state = {"blog:a": {"seen_ids": ["x"]}}
    pruned = prune_state(state, max_age_days=7)
    # Missing last_updated defaults to far future, so entry is kept
    assert "blog:a" in pruned


# ── commit_pending ───────────────────────────────────────────────────────────

def test_commit_pending_new_source():
    state = {}
    pending = {"blog:test": ["url1", "url2"]}
    result = commit_pending(state, pending, "2026-03-21")
    assert result["blog:test"]["seen_ids"] == ["url1", "url2"]
    assert result["blog:test"]["last_updated"] == "2026-03-21"


def test_commit_pending_merge_existing():
    state = {"blog:test": {"seen_ids": ["url0"], "last_updated": "2026-03-20"}}
    pending = {"blog:test": ["url1", "url2"]}
    result = commit_pending(state, pending, "2026-03-21")
    assert result["blog:test"]["seen_ids"] == ["url0", "url1", "url2"]
    assert result["blog:test"]["last_updated"] == "2026-03-21"


def test_commit_pending_max_ids():
    state = {"blog:test": {"seen_ids": [f"url{i}" for i in range(500)],
                           "last_updated": "2026-03-20"}}
    pending = {"blog:test": ["new1", "new2"]}
    result = commit_pending(state, pending, "2026-03-21", max_ids=500)
    assert len(result["blog:test"]["seen_ids"]) == 500
    assert "new1" in result["blog:test"]["seen_ids"]
    assert "new2" in result["blog:test"]["seen_ids"]
    # Oldest entries trimmed
    assert "url0" not in result["blog:test"]["seen_ids"]


# ── filter_gathered_sources (integration) ────────────────────────────────────

def test_filter_gathered_sources_mixed():
    sources = [
        _twitter_source(SAMPLE_TWEETS),
        _blog_source(SAMPLE_BLOG),
        _newsletter_source("Some newsletter"),
    ]
    state = {
        "twitter:testuser": {
            "seen_ids": ["https://x.com/user/status/111"],
            "last_updated": "2026-03-20",
        },
    }
    filtered, pending = filter_gathered_sources(sources, state)
    assert len(filtered) == 3
    # Twitter: 2 new (out of 3)
    assert len(pending["twitter:testuser"]) == 2
    # Blog: all 3 new
    assert len(pending["blog:test_blog"]) == 3
    # Newsletter: 1 new
    assert len(pending["newsletter:test_nl"]) == 1


def test_filter_gathered_sources_empty_content():
    sources = [_blog_source("")]
    filtered, pending = filter_gathered_sources(sources, {})
    assert filtered[0]["content"] == ""
    assert "blog:test_blog" not in pending


# ── End-to-end state roundtrip ───────────────────────────────────────────────

def test_state_roundtrip(tmp_path):
    """Simulate two pipeline runs: second run filters items seen in first."""
    sources = [_blog_source(SAMPLE_BLOG)]

    # Run 1: no prior state
    state = load_state(tmp_path)
    filtered, pending = filter_gathered_sources(sources, state)
    assert len(pending["blog:test_blog"]) == 3
    state = commit_pending(state, pending, "2026-03-21")
    save_state(tmp_path, state)

    # Run 2: same content — should all be filtered
    state = load_state(tmp_path)
    filtered2, pending2 = filter_gathered_sources(sources, state)
    assert filtered2[0]["content"] == ""
    assert "blog:test_blog" not in pending2
