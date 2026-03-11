"""Tests for digest_pipeline.seen_articles — pure functions, no mocking."""
import json
import math

from digest_pipeline.seen_articles import (
    load_history, save_today, filter_seen, parse_digest_markdown,
)


# ── Helper: generate a unit vector at a given angle ──────────────────────────

def _vec(angle_deg, dims=8):
    """Return a unit vector rotated by angle_deg in first two dims."""
    rad = math.radians(angle_deg)
    v = [0.0] * dims
    v[0] = math.cos(rad)
    v[1] = math.sin(rad)
    return v


# ── load_history ─────────────────────────────────────────────────────────────

def test_load_history_no_file(tmp_path):
    assert load_history(tmp_path, "2026-03-10") == []


def test_load_history_corrupt_json(tmp_path):
    (tmp_path / ".seen_embeddings.json").write_text("not json")
    assert load_history(tmp_path, "2026-03-10") == []


def test_load_history_excludes_today(tmp_path):
    store = {
        "2026-03-09": [{"title": "A", "embedding": [1.0]}],
        "2026-03-10": [{"title": "B", "embedding": [2.0]}],
    }
    (tmp_path / ".seen_embeddings.json").write_text(json.dumps(store))
    history = load_history(tmp_path, "2026-03-10")
    assert len(history) == 1
    assert history[0]["title"] == "A"


def test_load_history_respects_lookback(tmp_path):
    store = {
        "2026-03-01": [{"title": "Old", "embedding": [1.0]}],
        "2026-03-09": [{"title": "Recent", "embedding": [2.0]}],
    }
    (tmp_path / ".seen_embeddings.json").write_text(json.dumps(store))
    history = load_history(tmp_path, "2026-03-10", lookback_days=5)
    assert len(history) == 1
    assert history[0]["title"] == "Recent"


def test_load_history_multiple_days(tmp_path):
    store = {
        "2026-03-07": [{"title": "A", "embedding": [1.0]}],
        "2026-03-08": [{"title": "B", "embedding": [2.0]}],
        "2026-03-09": [{"title": "C", "embedding": [3.0]}],
    }
    (tmp_path / ".seen_embeddings.json").write_text(json.dumps(store))
    history = load_history(tmp_path, "2026-03-10", lookback_days=5)
    assert len(history) == 3


# ── save_today ───────────────────────────────────────────────────────────────

def test_save_today_creates_file(tmp_path):
    articles = [{"title": "X"}, {"title": "Y"}]
    embeds = [[1.0, 2.0], [3.0, 4.0]]
    save_today(tmp_path, "2026-03-10", articles, embeds)

    store = json.loads((tmp_path / ".seen_embeddings.json").read_text())
    assert "2026-03-10" in store
    assert len(store["2026-03-10"]) == 2
    assert store["2026-03-10"][0]["title"] == "X"
    assert store["2026-03-10"][0]["embedding"] == [1.0, 2.0]


def test_save_today_prunes_old_entries(tmp_path):
    # Pre-populate with an old entry
    old_store = {"2026-02-01": [{"title": "Old", "embedding": [0.0]}]}
    (tmp_path / ".seen_embeddings.json").write_text(json.dumps(old_store))

    save_today(tmp_path, "2026-03-10", [{"title": "New"}], [[1.0]])
    store = json.loads((tmp_path / ".seen_embeddings.json").read_text())
    assert "2026-02-01" not in store
    assert "2026-03-10" in store


def test_save_today_overwrites_same_date(tmp_path):
    save_today(tmp_path, "2026-03-10", [{"title": "First"}], [[1.0]])
    save_today(tmp_path, "2026-03-10", [{"title": "Second"}], [[2.0]])
    store = json.loads((tmp_path / ".seen_embeddings.json").read_text())
    assert len(store["2026-03-10"]) == 1
    assert store["2026-03-10"][0]["title"] == "Second"


# ── filter_seen ──────────────────────────────────────────────────────────────

def test_filter_seen_no_history():
    articles = [{"title": "A"}, {"title": "B"}]
    embeds = [_vec(0), _vec(90)]
    kept, skipped, kept_embeds = filter_seen(articles, embeds, [])
    assert len(kept) == 2
    assert len(skipped) == 0
    assert len(kept_embeds) == 2


def test_filter_seen_skips_duplicates():
    history = [{"title": "Old A", "embedding": _vec(0)}]
    articles = [
        {"title": "New A (same topic)"},  # near-identical direction
        {"title": "Unique B"},
    ]
    embeds = [_vec(2), _vec(90)]  # 2° away = very similar; 90° = orthogonal
    kept, skipped, kept_embeds = filter_seen(articles, embeds, history, threshold=0.85)
    assert len(skipped) == 1
    assert skipped[0]["title"] == "New A (same topic)"
    assert len(kept) == 1
    assert kept[0]["title"] == "Unique B"
    assert len(kept_embeds) == 1


def test_filter_seen_threshold_boundary():
    # cos(30°) ≈ 0.866, cos(35°) ≈ 0.819
    history = [{"title": "Ref", "embedding": _vec(0)}]
    articles = [{"title": "Close"}, {"title": "Far"}]
    embeds = [_vec(30), _vec(35)]
    kept, skipped, _ = filter_seen(articles, embeds, history, threshold=0.85)
    assert len(skipped) == 1  # 30° is above 0.85
    assert len(kept) == 1     # 35° is below 0.85


def test_filter_seen_all_duplicates():
    history = [{"title": "H", "embedding": _vec(0)}]
    articles = [{"title": "A"}, {"title": "B"}]
    embeds = [_vec(0), _vec(5)]  # both very close
    kept, skipped, kept_embeds = filter_seen(articles, embeds, history, threshold=0.85)
    assert len(kept) == 0
    assert len(skipped) == 2
    assert len(kept_embeds) == 0


# ── Round-trip integration ───────────────────────────────────────────────────

def test_save_then_load_and_filter(tmp_path):
    """Simulate two consecutive days: save day 1, filter day 2."""
    # Day 1: save articles
    day1_articles = [{"title": "GPT-5.4 Launches"}, {"title": "DeerFlow 2.0"}]
    day1_embeds = [_vec(0), _vec(45)]
    save_today(tmp_path, "2026-03-09", day1_articles, day1_embeds)

    # Day 2: load history from day 1, filter today's articles
    history = load_history(tmp_path, "2026-03-10", lookback_days=5)
    assert len(history) == 2

    day2_articles = [
        {"title": "GPT-5.4 Pro Released"},   # same topic as day 1's first
        {"title": "Brand New Topic"},
    ]
    day2_embeds = [_vec(3), _vec(120)]  # 3° = near duplicate; 120° = novel

    kept, skipped, kept_embeds = filter_seen(
        day2_articles, day2_embeds, history, threshold=0.85)

    assert len(skipped) == 1
    assert skipped[0]["title"] == "GPT-5.4 Pro Released"
    assert len(kept) == 1
    assert kept[0]["title"] == "Brand New Topic"


def test_five_day_rolling_window(tmp_path):
    """Simulate 6 days of accumulation, verify oldest is pruned."""
    for i in range(6):
        date = f"2026-03-{4 + i:02d}"
        save_today(tmp_path, date, [{"title": f"Day{i}"}], [_vec(i * 30)])

    # Loading from 2026-03-10 with 5-day lookback: cutoff is 2026-03-05
    # 2026-03-04 (Day0) should be outside window
    history = load_history(tmp_path, "2026-03-10", lookback_days=5)
    titles = [h["title"] for h in history]
    assert "Day0" not in titles  # 2026-03-04 is outside window
    assert "Day1" in titles      # 2026-03-05 is at boundary (included)
    assert "Day5" in titles      # 2026-03-09 is inside window


# ── parse_digest_markdown ────────────────────────────────────────────────────

SAMPLE_DIGEST = """\
⚡ AI DIGEST — Thursday, March 05, 2026

---

**🚀 Model Releases**

• [**GPT-5.4 Launches with 1M Context**](https://openai.com/gpt54)
OpenAI's GPT-5.4 combines coding with stronger document capabilities.
_Why it matters: This is OpenAI's most production-ready release yet._
→ [OpenAI Blog](https://openai.com/gpt54)

• [**Moonshine Voice: Whisper Alternative**](https://github.com/moonshine)
Moonshine is an open-source ASR toolkit optimized for edge devices.
_Why it matters: Fast on-device ASR is a critical missing piece._
→ [GitHub](https://github.com/moonshine)

---

**🛠️ Developer Tools**

[**DeerFlow 2.0: ByteDance's SuperAgent Framework**](https://github.com/bytedance/deer-flow)
ByteDance's DeerFlow 2.0 hit #1 on GitHub Trending.
_Why it matters: Open-source competitor raises the quality floor._
→ [GitHub](https://github.com/bytedance/deer-flow)
"""


def test_parse_digest_markdown_extracts_articles():
    articles = parse_digest_markdown(SAMPLE_DIGEST)
    assert len(articles) == 3
    assert articles[0]["title"] == "GPT-5.4 Launches with 1M Context"
    assert articles[1]["title"] == "Moonshine Voice: Whisper Alternative"
    assert articles[2]["title"] == "DeerFlow 2.0: ByteDance's SuperAgent Framework"


def test_parse_digest_markdown_extracts_descriptions():
    articles = parse_digest_markdown(SAMPLE_DIGEST)
    assert "coding" in articles[0]["description"]
    assert "edge devices" in articles[1]["description"]
    assert "GitHub Trending" in articles[2]["description"]


def test_parse_digest_markdown_empty():
    assert parse_digest_markdown("No articles here") == []
    assert parse_digest_markdown("") == []


def test_parse_digest_markdown_both_formats():
    """Handles both bullet-prefixed and non-bullet title lines."""
    articles = parse_digest_markdown(SAMPLE_DIGEST)
    # First two have bullets, third doesn't
    assert len(articles) == 3
