"""Tests for digest_pipeline.digest — pure functions, no mocking."""
from digest_pipeline.digest import batch_sources, _fmt_tokens, TokenTracker, _markdown_to_email_html


# ── batch_sources ────────────────────────────────────────────────────────────

def test_batch_sources_single_batch():
    sources = [
        {"name": "a", "content": "hello world"},
        {"name": "b", "content": "foo bar"},
    ]
    batches = batch_sources(sources)
    assert len(batches) == 1
    assert len(batches[0]) == 2


def test_batch_sources_multiple_batches():
    # Sources are truncated to MAX_SOURCE_CHARS (50K) first, so use smaller batch limit
    sources = [
        {"name": "a", "content": "x" * 40_000},
        {"name": "b", "content": "y" * 40_000},
    ]
    batches = batch_sources(sources, max_chars_per_batch=50_000)
    assert len(batches) == 2


def test_batch_sources_empty_sources_filtered():
    sources = [
        {"name": "a", "content": "real content"},
        {"name": "b", "content": ""},
        {"name": "c", "content": "   "},
    ]
    batches = batch_sources(sources)
    assert len(batches) == 1
    assert len(batches[0]) == 1


def test_batch_sources_oversized_truncation():
    long_content = "x" * 100_000
    sources = [{"name": "big", "content": long_content}]
    batches = batch_sources(sources)
    assert len(batches) == 1
    # MAX_SOURCE_CHARS is 50_000
    assert len(batches[0][0]["content"]) == 50_000


def test_batch_sources_empty_input():
    assert batch_sources([]) == []


def test_batch_sources_all_empty():
    sources = [{"name": "a", "content": ""}, {"name": "b", "content": "  "}]
    assert batch_sources(sources) == []


# ── _fmt_tokens ──────────────────────────────────────────────────────────────

def test_fmt_tokens_small():
    assert _fmt_tokens(0) == "0"
    assert _fmt_tokens(500) == "500"
    assert _fmt_tokens(999) == "999"


def test_fmt_tokens_medium():
    assert _fmt_tokens(1000) == "1.0K"
    assert _fmt_tokens(1500) == "1.5K"
    assert _fmt_tokens(9999) == "10.0K"


def test_fmt_tokens_large():
    assert _fmt_tokens(10000) == "10K"
    assert _fmt_tokens(50000) == "50K"
    assert _fmt_tokens(100000) == "100K"


# ── TokenTracker ─────────────────────────────────────────────────────────────

def test_token_tracker_add_and_totals():
    tracker = TokenTracker()
    tracker.add("Extract", "haiku", {"input_tokens": 100, "output_tokens": 50, "cost": 0.01}, 1.5)
    tracker.add("Format", "sonnet", {"input_tokens": 200, "output_tokens": 80, "cost": 0.05}, 3.0)
    assert tracker.total_input == 300
    assert tracker.total_output == 130
    assert abs(tracker.total_cost - 0.06) < 1e-9


def test_token_tracker_openrouter_keys():
    """OpenRouter uses prompt_tokens/completion_tokens instead."""
    tracker = TokenTracker()
    tracker.add("Extract", "haiku", {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.01}, 1.0)
    assert tracker.total_input == 100
    assert tracker.total_output == 50


def test_token_tracker_summary_format():
    tracker = TokenTracker()
    tracker.add("Extract", "haiku", {"input_tokens": 1000, "output_tokens": 500, "cost": 0.02}, 2.0)
    summary = tracker.summary()
    assert "Extract" in summary
    assert "haiku" in summary
    assert "$0.02" in summary


def test_token_tracker_empty():
    tracker = TokenTracker()
    assert tracker.total_input == 0
    assert tracker.total_output == 0
    assert tracker.total_cost == 0.0


# ── _markdown_to_email_html ─────────────────────────────────────────────────

def _make_config(emoji="📰", categories=None):
    return {
        "digest": {"emoji": emoji},
        "categories": categories or [{"emoji": "🤖", "id": "ai", "label": "AI"}],
    }


def test_markdown_to_html_heading():
    md = "📰 Daily Digest"
    html = _markdown_to_email_html(md, _make_config())
    assert "<h1" in html
    assert "Daily Digest" in html


def test_markdown_to_html_links():
    md = "Check [this](https://example.com) out"
    html = _markdown_to_email_html(md, _make_config())
    assert 'href="https://example.com"' in html
    assert ">this</a>" in html


def test_markdown_to_html_bold():
    md = "This is **important** text"
    html = _markdown_to_email_html(md, _make_config())
    assert "<b>important</b>" in html


def test_markdown_to_html_bullet():
    md = "• First item"
    html = _markdown_to_email_html(md, _make_config())
    assert "First item" in html
    assert "<div" in html


def test_markdown_to_html_hr():
    md = "---"
    html = _markdown_to_email_html(md, _make_config())
    assert "<hr" in html


def test_markdown_to_html_section_header():
    md = "**🤖 AI & ML**"
    html = _markdown_to_email_html(md, _make_config())
    assert "<h2" in html


def test_markdown_to_html_empty():
    html = _markdown_to_email_html("", _make_config())
    assert "<div" in html  # wrapper div still present
