"""Tests for digest_pipeline.source_state — incremental source processing."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from digest_pipeline.source_state import (
    load_state,
    save_state,
    prune_state,
    extract_and_filter,
    filter_gathered_sources,
    commit_pending,
    update_source_state,
    _extract_twitter_items,
    _extract_block_items,
    _extract_newsletter_id,
    _get_seen_ids_for_source,
    DEFAULT_TRENDING_COOLDOWN_DAYS,
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


def _newsletter_source(content, key="test_nl", message_id=None):
    source = {
        "source_key": f"newsletter:{key}",
        "source_type": "newsletter",
        "source_label": "Test Newsletter",
        "source_url": "",
        "content": content,
    }
    if message_id:
        source["message_id"] = message_id
    return source


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

SAMPLE_GITHUB = (
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


def test_extract_newsletter_id_prefers_message_id():
    """Message-ID is preferred over content hash when available."""
    nl_id = _extract_newsletter_id("Any content", message_id="abc123@example.com")
    assert nl_id == "msgid:abc123@example.com"


def test_extract_newsletter_id_message_id_ignores_content():
    """Same Message-ID with different content produces same ID."""
    id1 = _extract_newsletter_id("Content A", message_id="same@example.com")
    id2 = _extract_newsletter_id("Content B", message_id="same@example.com")
    assert id1 == id2 == "msgid:same@example.com"


def test_extract_newsletter_id_falls_back_to_hash():
    """Without Message-ID, falls back to content hash."""
    nl_id = _extract_newsletter_id("Some content", message_id=None)
    assert nl_id.startswith("hash:")


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
    source = _github_source(SAMPLE_GITHUB)
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


def test_filter_newsletter_with_message_id():
    """Newsletter with Message-ID uses it for dedup instead of content hash."""
    source = _newsletter_source("Content here", message_id="abc@example.com")
    # Not seen yet
    filtered, new_ids = extract_and_filter(source, set())
    assert len(new_ids) == 1
    assert new_ids[0] == "msgid:abc@example.com"
    assert filtered["content"] == "Content here"

    # Already seen
    filtered, new_ids = extract_and_filter(source, {"msgid:abc@example.com"})
    assert new_ids == []
    assert filtered["content"] == ""


def test_filter_newsletter_message_id_not_affected_by_content_change():
    """Same Message-ID with different content is still recognized as seen."""
    source1 = _newsletter_source("Version 1", message_id="id@example.com")
    _, ids1 = extract_and_filter(source1, set())
    source2 = _newsletter_source("Version 2 with changes", message_id="id@example.com")
    filtered, new_ids = extract_and_filter(source2, set(ids1))
    assert new_ids == []
    assert filtered["content"] == ""


# ── State persistence ────────────────────────────────────────────────────────

def test_save_and_load_state(tmp_path):
    state = {"twitter:user": {"seen_ids": ["id1", "id2"], "last_fetched": "2026-03-21"}}
    save_state(tmp_path, state)
    loaded = load_state(tmp_path)
    assert loaded == state


def test_load_state_missing_file(tmp_path):
    assert load_state(tmp_path) == {}


def test_load_state_corrupt_file(tmp_path):
    (tmp_path / ".source_state.json").write_text("not json{{{")
    assert load_state(tmp_path) == {}


def test_load_state_migrates_legacy_last_updated(tmp_path):
    """Legacy entries with last_updated should be migrated to last_fetched."""
    legacy = {
        "blog:a": {"seen_ids": ["x"], "last_updated": "2026-03-21"},
        "blog:b": {"seen_ids": ["y"], "last_updated": "2026-03-22", "last_fetched": "2026-03-23"},
    }
    (tmp_path / ".source_state.json").write_text(json.dumps(legacy))
    loaded = load_state(tmp_path)
    # a: last_updated copied to last_fetched, old key dropped
    assert loaded["blog:a"] == {"seen_ids": ["x"], "last_fetched": "2026-03-21"}
    # b: last_fetched already set; do not overwrite, just drop last_updated
    assert loaded["blog:b"] == {"seen_ids": ["y"], "last_fetched": "2026-03-23"}


# ── State pruning ────────────────────────────────────────────────────────────

def test_prune_state_keeps_recent():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    old = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
    state = {
        "blog:a": {"seen_ids": ["x"], "last_fetched": today},
        "blog:b": {"seen_ids": ["y"], "last_fetched": old},
    }
    pruned = prune_state(state, max_age_days=7)
    assert "blog:a" in pruned
    assert "blog:b" not in pruned


def test_prune_state_uses_most_recent_of_fetched_or_included():
    """Either last_fetched or last_included being recent keeps the entry."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    old = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
    state = {
        "blog:fetched_only": {"seen_ids": [], "last_fetched": today},
        "blog:included_only": {"seen_ids": [], "last_included": today},
        "blog:both_old": {"seen_ids": [], "last_fetched": old, "last_included": old},
    }
    pruned = prune_state(state, max_age_days=7)
    assert "blog:fetched_only" in pruned
    assert "blog:included_only" in pruned
    assert "blog:both_old" not in pruned


def test_prune_state_no_date():
    state = {"blog:a": {"seen_ids": ["x"]}}
    pruned = prune_state(state, max_age_days=7)
    # No timestamps at all defaults to far future, so entry is kept.
    assert "blog:a" in pruned


# ── commit_pending ───────────────────────────────────────────────────────────

def test_commit_pending_new_source():
    state = {}
    pending = {"blog:test": ["url1", "url2"]}
    result = commit_pending(state, pending, "2026-03-21")
    assert result["blog:test"]["seen_ids"] == ["url1", "url2"]
    assert "last_updated" not in result["blog:test"]


def test_commit_pending_merge_existing():
    state = {"blog:test": {"seen_ids": ["url0"], "last_fetched": "2026-03-20"}}
    pending = {"blog:test": ["url1", "url2"]}
    result = commit_pending(state, pending, "2026-03-21")
    assert result["blog:test"]["seen_ids"] == ["url0", "url1", "url2"]
    # commit_pending must not touch last_fetched / last_included; those are
    # owned by update_source_state.
    assert result["blog:test"]["last_fetched"] == "2026-03-20"
    assert "last_updated" not in result["blog:test"]


def test_commit_pending_preserves_other_timestamps():
    """commit_pending merges into existing entry; last_fetched/last_included survive."""
    state = {"blog:test": {
        "seen_ids": ["url0"],
        "last_fetched": "2026-03-19",
        "last_included": "2026-03-18",
    }}
    pending = {"blog:test": ["url1"]}
    result = commit_pending(state, pending, "2026-03-21")
    assert result["blog:test"]["last_fetched"] == "2026-03-19"
    assert result["blog:test"]["last_included"] == "2026-03-18"


def test_commit_pending_max_ids():
    state = {"blog:test": {"seen_ids": [f"url{i}" for i in range(500)],
                           "last_fetched": "2026-03-20"}}
    pending = {"blog:test": ["new1", "new2"]}
    result = commit_pending(state, pending, "2026-03-21", max_ids=500)
    assert len(result["blog:test"]["seen_ids"]) == 500
    assert "new1" in result["blog:test"]["seen_ids"]
    assert "new2" in result["blog:test"]["seen_ids"]
    # Oldest entries trimmed
    assert "url0" not in result["blog:test"]["seen_ids"]


# ── update_source_state ──────────────────────────────────────────────────────

def test_update_source_state_writes_timestamps():
    state = update_source_state(
        {}, {"blog:a": ["url1"]}, {"blog:a", "blog:b"}, {"blog:a"}, "2026-03-21")
    assert state["blog:a"]["last_fetched"] == "2026-03-21"
    assert state["blog:a"]["last_included"] == "2026-03-21"
    assert state["blog:a"]["seen_ids"] == ["url1"]
    # Fetched but no pending IDs and not included: still has last_fetched.
    assert state["blog:b"]["last_fetched"] == "2026-03-21"
    assert "last_included" not in state["blog:b"]


def test_update_source_state_preserves_old_included_when_not_included_today():
    """A source fetched today but not included should keep its prior last_included."""
    state = {"blog:a": {
        "seen_ids": ["url0"],
        "last_fetched": "2026-03-19",
        "last_included": "2026-03-18",
    }}
    result = update_source_state(
        state, {"blog:a": ["url1"]}, {"blog:a"}, set(), "2026-03-21")
    assert result["blog:a"]["last_fetched"] == "2026-03-21"
    assert result["blog:a"]["last_included"] == "2026-03-18"


# ── GitHub Trending cooldown ─────────────────────────────────────────────────

def test_commit_pending_github_trending_uses_timestamps():
    """GitHub trending seen_ids should be a dict with dates, not a list."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    state = {}
    pending = {"github_trending:trending": [
        "https://github.com/owner/repo",
        "https://github.com/other/project",
    ]}
    result = commit_pending(state, pending, today)
    seen = result["github_trending:trending"]["seen_ids"]
    assert isinstance(seen, dict)
    assert seen["https://github.com/owner/repo"] == today
    assert seen["https://github.com/other/project"] == today


def test_commit_pending_github_trending_preserves_existing_dates():
    """Re-seeing a repo should NOT update its first-seen date."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    earlier = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")
    state = {"github_trending:trending": {
        "seen_ids": {"https://github.com/owner/repo": earlier},
        "last_fetched": earlier,
    }}
    pending = {"github_trending:trending": ["https://github.com/owner/repo"]}
    result = commit_pending(state, pending, today)
    seen = result["github_trending:trending"]["seen_ids"]
    # Original date preserved, not overwritten
    assert seen["https://github.com/owner/repo"] == earlier


def test_commit_pending_github_trending_migrates_legacy_list():
    """Old list-format seen_ids should be migrated to dict format."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    state = {"github_trending:trending": {
        "seen_ids": ["https://github.com/old/repo"],
        "last_fetched": today,
    }}
    pending = {"github_trending:trending": ["https://github.com/new/repo"]}
    result = commit_pending(state, pending, today)
    seen = result["github_trending:trending"]["seen_ids"]
    assert isinstance(seen, dict)
    assert "https://github.com/old/repo" in seen
    assert "https://github.com/new/repo" in seen


def test_get_seen_ids_cooldown_active():
    """Repos within the cooldown window are suppressed."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    entry = {"seen_ids": {"https://github.com/owner/repo": today}}
    seen = _get_seen_ids_for_source("github_trending:trending", entry)
    assert "https://github.com/owner/repo" in seen


def test_get_seen_ids_cooldown_expired():
    """Repos past the cooldown window can reappear."""
    old_date = (datetime.utcnow() - timedelta(days=DEFAULT_TRENDING_COOLDOWN_DAYS + 1)).strftime("%Y-%m-%d")
    entry = {"seen_ids": {"https://github.com/owner/repo": old_date}}
    seen = _get_seen_ids_for_source("github_trending:trending", entry)
    assert "https://github.com/owner/repo" not in seen


def test_get_seen_ids_cooldown_mixed():
    """Mix of active and expired cooldowns."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    old_date = (datetime.utcnow() - timedelta(days=DEFAULT_TRENDING_COOLDOWN_DAYS + 1)).strftime("%Y-%m-%d")
    entry = {"seen_ids": {
        "https://github.com/active/repo": today,
        "https://github.com/expired/repo": old_date,
    }}
    seen = _get_seen_ids_for_source("github_trending:trending", entry)
    assert "https://github.com/active/repo" in seen
    assert "https://github.com/expired/repo" not in seen


def test_get_seen_ids_non_trending_uses_flat_list():
    """Non-trending sources still use flat list behavior."""
    entry = {"seen_ids": ["id1", "id2"]}
    seen = _get_seen_ids_for_source("blog:test", entry)
    assert seen == {"id1", "id2"}


def test_github_trending_cooldown_integration():
    """End-to-end: repo suppressed during cooldown, reappears after expiry."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    old_date = (datetime.utcnow() - timedelta(days=DEFAULT_TRENDING_COOLDOWN_DAYS + 1)).strftime("%Y-%m-%d")

    source = _github_source(SAMPLE_GITHUB)

    # Repo seen recently — should be filtered
    state = {"github_trending:trending": {
        "seen_ids": {"https://github.com/owner/repo": today},
        "last_fetched": today,
    }}
    filtered, pending = filter_gathered_sources([source], state)
    assert len(pending.get("github_trending:trending", [])) == 1
    assert "https://github.com/other/project" in pending.get("github_trending:trending", [])

    # Repo cooldown expired — should reappear
    state = {"github_trending:trending": {
        "seen_ids": {"https://github.com/owner/repo": old_date},
        "last_fetched": old_date,
    }}
    filtered, pending = filter_gathered_sources([source], state)
    assert len(pending.get("github_trending:trending", [])) == 2
    assert "https://github.com/owner/repo" in pending["github_trending:trending"]
    assert "https://github.com/other/project" in pending["github_trending:trending"]


def test_github_trending_state_roundtrip(tmp_path):
    """Roundtrip: commit trending state, reload, verify dict format persisted."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    state = {}
    pending = {"github_trending:trending": ["https://github.com/owner/repo"]}
    state = commit_pending(state, pending, today)
    save_state(tmp_path, state)

    loaded = load_state(tmp_path)
    seen = loaded["github_trending:trending"]["seen_ids"]
    assert isinstance(seen, dict)
    assert seen["https://github.com/owner/repo"] == today


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
            "last_fetched": "2026-03-20",
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
