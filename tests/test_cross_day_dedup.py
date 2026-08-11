"""Tests for the layered cross-day dedup: URL index, lexical titles, embeddings."""
import json
import math

from digest_pipeline.dedup_index import (
    article_urls, best_title_match, canonicalize_url, filter_by_url_index,
    load_url_index, normalize_title_tokens, record_shipped, save_url_index,
    title_similarity,
)
from digest_pipeline.seen_articles import (
    filter_seen, grey_zone_candidates, load_history, save_today,
)


def _vec(angle_deg, dims=8):
    rad = math.radians(angle_deg)
    v = [0.0] * dims
    v[0] = math.cos(rad)
    v[1] = math.sin(rad)
    return v


# ── canonicalize_url ─────────────────────────────────────────────────────────

def test_canonical_strips_utm():
    a = canonicalize_url("https://example.com/post?utm_source=x&utm_medium=y")
    b = canonicalize_url("https://example.com/post")
    assert a == b


def test_canonical_strips_ref_and_fbclid():
    a = canonicalize_url("https://example.com/post?ref=twitter&fbclid=abc")
    assert a == canonicalize_url("https://example.com/post")


def test_canonical_keeps_meaningful_params():
    a = canonicalize_url("https://example.com/watch?v=abc123")
    b = canonicalize_url("https://example.com/watch?v=zzz999")
    assert a != b
    assert "v=abc123" in a


def test_canonical_host_case_and_www():
    assert (canonicalize_url("https://WWW.Example.COM/Post")
            == canonicalize_url("https://example.com/Post"))


def test_canonical_trailing_slash_and_fragment():
    assert (canonicalize_url("https://example.com/post/#section")
            == canonicalize_url("https://example.com/post"))


def test_canonical_amp_variants():
    assert (canonicalize_url("https://example.com/post/amp")
            == canonicalize_url("https://example.com/post"))
    assert (canonicalize_url("https://amp.example.com/post")
            == canonicalize_url("https://example.com/post"))


def test_canonical_scheme_normalized():
    assert (canonicalize_url("http://example.com/post")
            == canonicalize_url("https://example.com/post"))


def test_canonical_query_param_order():
    assert (canonicalize_url("https://e.com/p?b=2&a=1")
            == canonicalize_url("https://e.com/p?a=1&b=2"))


def test_canonical_empty_and_garbage():
    assert canonicalize_url("") == ""
    assert canonicalize_url(None) == ""
    assert canonicalize_url("not a url") != ""  # doesn't raise


# ── URL index persistence + filtering ────────────────────────────────────────

def test_url_index_roundtrip(tmp_path):
    index = record_shipped({}, [{"title": "A", "urls": ["https://e.com/a?utm_source=x"]}],
                           "2026-08-10")
    save_url_index(tmp_path, index, "2026-08-10")
    loaded = load_url_index(tmp_path)
    assert loaded == {"https://e.com/a": "2026-08-10"}


def test_url_index_prunes_old_entries(tmp_path):
    index = {"https://e.com/old": "2026-07-01", "https://e.com/new": "2026-08-09"}
    save_url_index(tmp_path, index, "2026-08-10", lookback_days=14)
    loaded = load_url_index(tmp_path)
    assert "https://e.com/old" not in loaded
    assert "https://e.com/new" in loaded


def test_url_index_missing_and_corrupt(tmp_path):
    assert load_url_index(tmp_path) == {}
    (tmp_path / ".shipped_urls.json").write_text("not json", encoding="utf-8")
    assert load_url_index(tmp_path) == {}


def test_filter_by_url_index_cross_source():
    """The same story from a different source but the same target URL is caught."""
    index = {"https://lab.example/model": "2026-08-09"}
    articles = [
        {"title": "Lab drops new model", "urls": ["https://lab.example/model?utm_source=tw"]},
        {"title": "Unrelated", "urls": ["https://other.example/x"]},
    ]
    kept, skipped = filter_by_url_index(articles, index)
    assert [a["title"] for a in kept] == ["Unrelated"]
    assert skipped[0]["_skip_reason"].startswith("url shipped")


def test_article_urls_handles_both_fields():
    assert article_urls({"urls": ["a"], "url": "b"}) == ["a", "b"]
    assert article_urls({}) == []


# ── title similarity ─────────────────────────────────────────────────────────

def test_title_tokens_drop_stopwords_and_short():
    tokens = normalize_title_tokens("Claude Memory and Connectors Now Free for All Users")
    assert "and" not in tokens and "for" not in tokens
    assert {"claude", "memory", "connectors", "free", "users"} <= tokens


def test_title_similarity_identical():
    t = "OpenSandbox: Alibaba's General-Purpose Sandbox for AI Agents"
    assert title_similarity(t, t) == 1.0


def test_title_similarity_real_duplicate_pair():
    """A real pair from the archives that shipped on consecutive days."""
    sim = title_similarity(
        "Claude Memory and Connectors Now Free for All Users",
        "Claude Memory and 150+ Connectors Now Free",
    )
    assert sim >= 0.6


def test_title_similarity_different_stories():
    sim = title_similarity(
        "OpenAI releases GPT-5 benchmark results",
        "Anthropic announces enterprise pricing changes",
    )
    assert sim < 0.3


def test_title_similarity_empty():
    assert title_similarity("", "anything here") == 0.0


def test_best_title_match():
    sim, match = best_title_match(
        "DeerFlow 2.0 Released", ["Unrelated Story", "DeerFlow 2.0 released today"])
    assert match == "DeerFlow 2.0 released today"
    assert sim > 0.6


# ── filter_seen: layered gates ───────────────────────────────────────────────

def test_filter_seen_title_gate_catches_divergent_embeddings():
    """Same headline, orthogonal embeddings (divergent body prose) — the
    lexical gate must catch what the embedding gate misses. This is the
    exact failure mode behind repeated stories in the shipped digests."""
    history = [{"title": "DeerFlow 2.0 Agent Framework Released",
                "embedding": _vec(0)}]
    articles = [{"title": "DeerFlow 2.0 agent framework released"}]
    kept, skipped, kept_emb = filter_seen(
        articles, [_vec(80)], history, threshold=0.85, title_threshold=0.6)
    assert kept == []
    assert "title match" in skipped[0]["_skip_reason"]


def test_filter_seen_embedding_gate_still_works():
    history = [{"title": "irrelevant", "embedding": _vec(0)}]
    articles = [{"title": "Completely Different Headline Words"}]
    kept, skipped, _ = filter_seen(
        articles, [_vec(5)], history, threshold=0.85, title_threshold=0.6)
    assert kept == []
    assert "embedding similarity" in skipped[0]["_skip_reason"]


def test_filter_seen_keeps_novel():
    history = [{"title": "Old Story About Kubernetes", "embedding": _vec(0)}]
    articles = [{"title": "Fresh News About Quantum Chips"}]
    kept, skipped, kept_emb = filter_seen(
        articles, [_vec(80)], history, threshold=0.85, title_threshold=0.6)
    assert len(kept) == 1 and skipped == []
    assert kept_emb == [_vec(80)]


def test_filter_seen_title_gate_disabled():
    history = [{"title": "Same Exact Headline Words Here", "embedding": _vec(0)}]
    articles = [{"title": "Same Exact Headline Words Here"}]
    kept, skipped, _ = filter_seen(
        articles, [_vec(80)], history, threshold=0.85, title_threshold=None)
    assert len(kept) == 1  # embedding far apart, lexical gate off


def test_filter_seen_history_without_titles():
    """Legacy history entries (no title/urls keys) must not crash."""
    history = [{"embedding": _vec(0)}]
    articles = [{"title": "Anything Goes Here"}]
    kept, skipped, _ = filter_seen(articles, [_vec(80)], history,
                                   threshold=0.85, title_threshold=0.6)
    assert len(kept) == 1


# ── grey zone ────────────────────────────────────────────────────────────────

def test_grey_zone_candidates_band():
    history = [{"title": "Prior Coverage Of Launch", "embedding": _vec(0)}]
    arts = [{"title": "Suspicious", "description": "d1"},
            {"title": "Clearly Novel", "description": "d2"}]
    # cos(30°)≈0.866 → inside [0.70, 0.95); cos(80°)≈0.17 → outside
    cands = grey_zone_candidates(arts, [_vec(30), _vec(80)], history, 0.70, 0.95)
    assert len(cands) == 1
    assert cands[0]["index"] == 0
    assert cands[0]["match_title"] == "Prior Coverage Of Launch"
    assert 0.70 <= cands[0]["similarity"] < 0.95


def test_grey_zone_empty_history():
    assert grey_zone_candidates([{"title": "X"}], [_vec(0)], [], 0.7, 0.9) == []


# ── save_today stores urls; load_history reads them back ─────────────────────

def test_save_today_stores_urls(tmp_path):
    arts = [{"title": "A", "urls": ["https://e.com/a"]}]
    save_today(tmp_path, "2026-08-10", arts, [[1.0, 0.0]])
    store = json.loads((tmp_path / ".seen_embeddings.json").read_text(encoding="utf-8"))
    assert store["2026-08-10"][0]["urls"] == ["https://e.com/a"]
    history = load_history(tmp_path, "2026-08-11")
    assert history[0]["title"] == "A"
