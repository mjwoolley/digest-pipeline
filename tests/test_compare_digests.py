"""Tests for the prod/staging comparison tool and URL-index backfill."""
import importlib.util
import json
from pathlib import Path

from digest_pipeline.dedup_index import backfill_url_index, load_url_index

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "compare_digests", REPO / "scripts" / "compare_digests.py")
compare_digests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_digests)


def _digest_md(articles):
    """Render articles in the archived-digest markdown shape."""
    blocks = []
    for title, url, desc in articles:
        blocks.append(f"• [**{title}**]({url})\n{desc}\n_Why it matters: x_\n")
    return "**🚀 Section**\n\n" + "\n".join(blocks)


def _write_day(root, date, articles, run_totals=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{date}.md").write_text(_digest_md(articles), encoding="utf-8")
    if run_totals is not None:
        work = root / "work" / date
        work.mkdir(parents=True, exist_ok=True)
        (work / "run.json").write_text(json.dumps({
            "status": "success", "duration_s": 100,
            "stages": [], "totals": run_totals,
        }), encoding="utf-8")


# ── match_articles ───────────────────────────────────────────────────────────

def test_match_by_url_beats_title():
    prod = [{"title": "Completely Renamed Headline", "urls": ["https://e.com/a?utm_source=x"]}]
    staging = [{"title": "Different Words Entirely", "urls": ["https://e.com/a"]}]
    m = compare_digests.match_articles(prod, staging)
    assert len(m["shared"]) == 1
    assert m["prod_only"] == [] and m["staging_only"] == []


def test_match_by_title_similarity():
    prod = [{"title": "Acme Widget 2.0 Launches Globally", "urls": ["https://a.com/1"]}]
    staging = [{"title": "Acme launches Widget 2.0", "urls": ["https://b.com/2"]}]
    m = compare_digests.match_articles(prod, staging)
    assert len(m["shared"]) == 1


def test_match_classifies_one_sided():
    prod = [{"title": "Only In Prod Story", "urls": ["https://p.com/1"]}]
    staging = [{"title": "Only In Staging Story", "urls": ["https://s.com/1"]}]
    m = compare_digests.match_articles(prod, staging)
    assert m["shared"] == []
    assert len(m["prod_only"]) == 1 and len(m["staging_only"]) == 1


def test_match_no_double_claim():
    """Two prod articles can't both match the same staging article."""
    prod = [{"title": "Same Story Headline", "urls": []},
            {"title": "Same Story Headline", "urls": []}]
    staging = [{"title": "Same Story Headline", "urls": []}]
    m = compare_digests.match_articles(prod, staging)
    assert len(m["shared"]) == 1
    assert len(m["prod_only"]) == 1


# ── count_repeats ────────────────────────────────────────────────────────────

def test_count_repeats_url_and_title():
    days = {
        "2026-08-09": [{"title": "Original Coverage Of Launch", "urls": ["https://e.com/x"]}],
        "2026-08-10": [
            {"title": "Renamed But Same Link", "urls": ["https://e.com/x?ref=tw"]},
            {"title": "Original coverage of launch", "urls": ["https://other.com/y"]},
            {"title": "Genuinely Fresh News Item", "urls": ["https://new.com/z"]},
        ],
    }
    repeats = compare_digests.count_repeats(days, "2026-08-10")
    kinds = {a["title"]: a["_repeat_via"] for a in repeats}
    assert kinds["Renamed But Same Link"] == "url"
    assert kinds["Original coverage of launch"].startswith("title")
    assert "Genuinely Fresh News Item" not in kinds


def test_count_repeats_window_bounded():
    days = {
        "2026-08-01": [{"title": "Old Story Outside Window", "urls": ["https://e.com/old"]}],
        "2026-08-10": [{"title": "Old Story Outside Window", "urls": ["https://e.com/old"]}],
    }
    assert compare_digests.count_repeats(days, "2026-08-10", lookback=5) == []


# ── end-to-end report ────────────────────────────────────────────────────────

def test_self_compare_all_shared(tmp_path):
    root = tmp_path / "side"
    _write_day(root, "2026-08-10", [
        ("Story One Alpha", "https://e.com/1", "Desc one."),
        ("Story Two Beta", "https://e.com/2", "Desc two."),
    ], run_totals={"input_tokens": 100, "output_tokens": 50, "cost": 0.05})
    days = compare_digests.load_archive(root)
    report, summary = compare_digests.compare_date(root, root, "2026-08-10",
                                                   days, days)
    assert summary["shared"] == 2
    assert summary["prod_only"] == 0 and summary["staging_only"] == 0
    assert summary["prod_cost"] == 0.05
    assert "Shared: 2" in report


def test_compare_reports_suppression_reason(tmp_path):
    prod = tmp_path / "prod"
    staging = tmp_path / "staging"
    _write_day(prod, "2026-08-10", [
        ("Repeated Launch Story", "https://e.com/1", "Desc."),
        ("Fresh Story", "https://e.com/2", "Desc."),
    ])
    _write_day(staging, "2026-08-10", [
        ("Fresh Story", "https://e.com/2", "Desc."),
    ])
    work = staging / "work" / "2026-08-10"
    work.mkdir(parents=True)
    (work / "cross_skipped.json").write_text(json.dumps([
        {"title": "Repeated Launch Story",
         "_skip_reason": "url shipped 2026-08-09: https://e.com/1"},
    ]), encoding="utf-8")
    prod_days = compare_digests.load_archive(prod)
    staging_days = compare_digests.load_archive(staging)
    report, summary = compare_digests.compare_date(prod, staging, "2026-08-10",
                                                   prod_days, staging_days)
    assert summary["prod_only"] == 1
    assert "staging suppressed it: url shipped" in report


def test_missing_run_json_graceful(tmp_path):
    root = tmp_path / "side"
    _write_day(root, "2026-08-10", [("Story One Alpha", "https://e.com/1", "D")])
    days = compare_digests.load_archive(root)
    report, summary = compare_digests.compare_date(root, root, "2026-08-10",
                                                   days, days)
    assert "(no run.json)" in report
    assert summary["prod_cost"] is None


# ── backfill_url_index ───────────────────────────────────────────────────────

def test_backfill_url_index_seeds_from_archives(tmp_path):
    _write_day(tmp_path, "2026-08-08",
               [("Story A Headline", "https://e.com/a?utm_source=x", "D")])
    _write_day(tmp_path, "2026-08-09",
               [("Story B Headline", "https://e.com/b", "D")])
    n = backfill_url_index(tmp_path, "2026-08-10", lookback_days=14)
    index = load_url_index(tmp_path)
    assert n == 2
    assert index["https://e.com/a"] == "2026-08-08"
    assert index["https://e.com/b"] == "2026-08-09"


def test_backfill_url_index_respects_window(tmp_path):
    _write_day(tmp_path, "2026-07-01",
               [("Ancient Story Headline", "https://e.com/old", "D")])
    _write_day(tmp_path, "2026-08-09",
               [("Recent Story Headline", "https://e.com/new", "D")])
    backfill_url_index(tmp_path, "2026-08-10", lookback_days=14)
    index = load_url_index(tmp_path)
    assert "https://e.com/old" not in index
    assert "https://e.com/new" in index


def test_backfill_url_index_idempotent(tmp_path):
    _write_day(tmp_path, "2026-08-09",
               [("Story Headline Here", "https://e.com/a", "D")])
    n1 = backfill_url_index(tmp_path, "2026-08-10")
    n2 = backfill_url_index(tmp_path, "2026-08-10")
    assert n1 == n2 == 1
