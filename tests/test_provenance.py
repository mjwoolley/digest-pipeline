"""Tests for source provenance schema migration.

Covers:
- Extract schema shape (source provenance stamped by code, not LLM)
- Multi-source batch source attribution via SRC tokens
- Progress summary counting by source_key
- Dedupe source provenance collection
"""
import json
from unittest.mock import patch, MagicMock

from digest_pipeline.llm import (
    extract_normalize,
    dedupe_merge,
    _collect_source_provenance,
    _parse_json_array,
)
from digest_pipeline.digest import batch_sources


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_source(key, stype, label, url="", content="some content"):
    return {
        "source_key": f"{stype}:{key}",
        "source_type": stype,
        "source_label": label,
        "source_url": url,
        "content": content,
    }


# ── Extract schema ──────────────────────────────────────────────────────────

def test_extract_stamps_provenance_from_source():
    """Extract articles get source provenance stamped by code, not LLM."""
    sources = [
        _make_source("anthropic_blog", "blog", "Anthropic News",
                      "https://www.anthropic.com/news", "Claude 4 released"),
    ]
    llm_response = json.dumps([{
        "title": "Claude 4 Released",
        "category": "model-release",
        "description": "Anthropic releases Claude 4",
        "url": "https://www.anthropic.com/news/claude-4",
        "src_ref": "SRC1",
    }])
    with patch("digest_pipeline.llm.chat", return_value=(llm_response, {"input_tokens": 0, "output_tokens": 0, "cost": 0})):
        articles, _ = extract_normalize(sources, "2026-03-21", "{{SOURCES}}")

    assert len(articles) == 1
    a = articles[0]
    assert a["source_key"] == "blog:anthropic_blog"
    assert a["source_type"] == "blog"
    assert a["source_label"] == "Anthropic News"
    assert a["source_url"] == "https://www.anthropic.com/news"
    assert a["title"] == "Claude 4 Released"
    assert a["url"] == "https://www.anthropic.com/news/claude-4"
    # src_ref should be removed after stamping
    assert "src_ref" not in a


def test_extract_multi_source_batch_attribution():
    """In multi-source batches, each article gets provenance from its source."""
    sources = [
        _make_source("claudeai", "twitter", "@claudeai",
                      "https://twitter.com/claudeai", "Tweet about Claude"),
        _make_source("openai_blog", "blog", "OpenAI Blog",
                      "https://openai.com/blog/rss.xml", "GPT-5 announced"),
    ]
    llm_response = json.dumps([
        {
            "title": "Claude update",
            "category": "model-release",
            "description": "Claude got smarter",
            "url": "https://twitter.com/claudeai/status/123",
            "src_ref": "SRC1",
        },
        {
            "title": "GPT-5 Launch",
            "category": "model-release",
            "description": "OpenAI launches GPT-5",
            "url": "https://openai.com/blog/gpt-5",
            "src_ref": "SRC2",
        },
    ])
    with patch("digest_pipeline.llm.chat", return_value=(llm_response, {"input_tokens": 0, "output_tokens": 0, "cost": 0})):
        articles, _ = extract_normalize(sources, "2026-03-21", "{{SOURCES}}")

    assert len(articles) == 2
    # First article should be attributed to Twitter source
    assert articles[0]["source_key"] == "twitter:claudeai"
    assert articles[0]["source_type"] == "twitter"
    assert articles[0]["source_label"] == "@claudeai"
    # Second article should be attributed to blog source
    assert articles[1]["source_key"] == "blog:openai_blog"
    assert articles[1]["source_type"] == "blog"
    assert articles[1]["source_label"] == "OpenAI Blog"


def test_extract_unknown_src_ref_drops_article():
    """If model emits an unknown src_ref, drop the article instead of guessing."""
    sources = [
        _make_source("test", "blog", "Test Blog", "", "some content"),
    ]
    llm_response = json.dumps([{
        "title": "Mystery Article",
        "category": "other",
        "description": "desc",
        "url": "https://example.com",
        "src_ref": "SRC99",  # bad ref
    }])
    with patch("digest_pipeline.llm.chat", return_value=(llm_response, {"input_tokens": 0, "output_tokens": 0, "cost": 0})):
        articles, _ = extract_normalize(sources, "2026-03-21", "{{SOURCES}}")

    # Article with unknown src_ref should be dropped
    assert len(articles) == 0


def test_extract_empty_sources_skipped():
    """Empty sources are skipped in SRC numbering."""
    sources = [
        _make_source("empty", "blog", "Empty", "", ""),
        _make_source("real", "blog", "Real Blog", "", "real content"),
    ]
    llm_response = json.dumps([{
        "title": "Article",
        "category": "other",
        "description": "desc",
        "url": "https://example.com",
        "src_ref": "SRC1",  # First non-empty source
    }])
    with patch("digest_pipeline.llm.chat", return_value=(llm_response, {"input_tokens": 0, "output_tokens": 0, "cost": 0})):
        articles, _ = extract_normalize(sources, "2026-03-21", "{{SOURCES}}")

    assert len(articles) == 1
    assert articles[0]["source_key"] == "blog:real"


# ── Progress summary counting ───────────────────────────────────────────────

def test_progress_counting_by_source_key():
    """Progress summary should count articles by source_key, not LLM source text.

    This is the fix for the bug where Twitter counts showed zero because
    gathered source names were matched against LLM-generated source labels.
    """
    # Simulate extracted articles with provenance stamped by code
    articles = [
        {"title": "Tweet 1", "source_key": "twitter:claudeai", "source_label": "@claudeai"},
        {"title": "Tweet 2", "source_key": "twitter:claudeai", "source_label": "@claudeai"},
        {"title": "Blog Post", "source_key": "blog:openai_blog", "source_label": "OpenAI Blog"},
    ]

    # Count by source_key (the fixed approach)
    article_counts = {}
    for a in articles:
        sk = a.get("source_key", "unknown")
        article_counts[sk] = article_counts.get(sk, 0) + 1

    assert article_counts["twitter:claudeai"] == 2
    assert article_counts["blog:openai_blog"] == 1

    # Simulate the batch with source provenance
    batch = [
        _make_source("claudeai", "twitter", "@claudeai", content="tweets"),
        _make_source("openai_blog", "blog", "OpenAI Blog", content="blog post"),
    ]

    # The progress line should correctly show counts
    for s in batch:
        count = article_counts.get(s["source_key"], 0)
        assert count > 0, f"Count for {s['source_key']} should be > 0"


# ── Dedupe source provenance ────────────────────────────────────────────────

def test_collect_source_provenance():
    """_collect_source_provenance collects unique keys and labels."""
    articles = [
        {"source_key": "blog:a", "source_label": "Blog A"},
        {"source_key": "twitter:b", "source_label": "@b"},
        {"source_key": "blog:a", "source_label": "Blog A"},  # duplicate
    ]
    prov = _collect_source_provenance(articles)
    assert prov["source_keys"] == ["blog:a", "twitter:b"]
    assert prov["source_labels"] == ["Blog A", "@b"]


def test_dedupe_singleton_provenance():
    """Singleton clusters get source_keys and source_labels lists."""
    clusters = [[{
        "title": "Article",
        "category": "other",
        "description": "desc",
        "url": "https://example.com",
        "source_key": "blog:test",
        "source_type": "blog",
        "source_label": "Test Blog",
        "source_url": "https://example.com/feed",
    }]]
    with patch("digest_pipeline.llm.chat") as mock_chat:
        result, _ = dedupe_merge(clusters, "2026-03-21", "{{CLUSTERS}}")

    # LLM should not be called for singletons
    mock_chat.assert_not_called()

    assert len(result) == 1
    a = result[0]
    assert a["source_keys"] == ["blog:test"]
    assert a["source_labels"] == ["Test Blog"]
    assert a["urls"] == ["https://example.com"]
    # Per-article source fields should be removed
    assert "source_key" not in a
    assert "source_type" not in a


def test_dedupe_multi_cluster_provenance():
    """Merged clusters get combined source_keys and source_labels."""
    clusters = [[
        {
            "title": "Article A",
            "category": "other",
            "description": "desc A",
            "url": "https://a.com",
            "source_key": "blog:a",
            "source_type": "blog",
            "source_label": "Blog A",
            "source_url": "https://a.com/feed",
        },
        {
            "title": "Article B",
            "category": "other",
            "description": "desc B",
            "url": "https://b.com",
            "source_key": "twitter:b",
            "source_type": "twitter",
            "source_label": "@b",
            "source_url": "https://twitter.com/b",
        },
    ]]
    llm_response = json.dumps([{
        "title": "Merged Article",
        "category": "other",
        "description": "combined desc",
        "urls": ["https://a.com", "https://b.com"],
    }])
    with patch("digest_pipeline.llm.chat", return_value=(llm_response, {"input_tokens": 0, "output_tokens": 0, "cost": 0})):
        result, _ = dedupe_merge(clusters, "2026-03-21", "{{CLUSTERS}}")

    assert len(result) == 1
    a = result[0]
    assert a["source_keys"] == ["blog:a", "twitter:b"]
    assert a["source_labels"] == ["Blog A", "@b"]
    assert "source_key" not in a
    assert "source_type" not in a


# ── batch_sources with new schema ───────────────────────────────────────────

def test_batch_sources_new_schema():
    """batch_sources works with new source provenance fields."""
    sources = [
        _make_source("a", "blog", "Blog A", content="hello world"),
        _make_source("b", "twitter", "@b", content="tweet"),
    ]
    batches = batch_sources(sources)
    assert len(batches) == 1
    assert len(batches[0]) == 2
    assert batches[0][0]["source_key"] == "blog:a"
