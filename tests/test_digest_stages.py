"""Tests for orchestrator pieces: prioritize selection, RunLog states,
crash-handler guards, email HTML escaping."""
import inspect
import json

from digest_pipeline import digest as digest_mod
from digest_pipeline.digest import (
    _markdown_to_email_html, score_articles_by_id, select_top_articles,
)
from digest_pipeline.run_log import RunLog


# ── score_articles_by_id ─────────────────────────────────────────────────────

def test_scores_matched_by_id_not_title():
    scored = [{"id": 1, "score": 9}, {"id": 0, "score": 2}]
    assert score_articles_by_id(scored, 2) == [2, 9]


def test_scores_malformed_entries_default():
    scored = [{"id": "zero", "score": 9}, {"id": 1}, {"score": 3}, "junk"[0:0] or {}]
    assert score_articles_by_id(scored, 2) == [5, 5]


def test_scores_missing_ids_default():
    assert score_articles_by_id([], 3) == [5, 5, 5]


# ── select_top_articles ──────────────────────────────────────────────────────

def _arts(specs):
    return [{"title": t, "category": c} for t, c in specs]


def test_select_guarantees_category_representation():
    articles = _arts([("A1", "models"), ("A2", "models"), ("A3", "models"),
                      ("B1", "tools")])
    scores = [9, 8, 7, 1]  # tools scores lowest but must still get a slot
    kept = select_top_articles(articles, scores, max_articles=3)
    assert 3 in kept  # B1 kept despite lowest score
    assert len(kept) == 3


def test_select_fills_by_score():
    articles = _arts([("A", "x"), ("B", "x"), ("C", "x"), ("D", "x")])
    scores = [1, 9, 5, 7]
    kept = select_top_articles(articles, scores, max_articles=2)
    assert kept == [1, 3]  # top scores, original order preserved


def test_select_duplicate_titles_do_not_collapse():
    """Two distinct articles with the same title previously collapsed via
    title-keyed set membership, silently losing one selection slot."""
    articles = _arts([("Same Title", "x"), ("Same Title", "y"), ("Other", "z")])
    scores = [5, 5, 5]
    kept = select_top_articles(articles, scores, max_articles=3)
    assert kept == [0, 1, 2]


def test_select_untitled_articles_survive():
    articles = [{"category": "x"}, {"category": "y"}]
    kept = select_top_articles(articles, [5, 5], max_articles=2)
    assert kept == [0, 1]


# ── RunLog terminal states ───────────────────────────────────────────────────

def test_runlog_skip_finalizes(tmp_path):
    """Skip exits previously left run.json in 'running' forever."""
    rl = RunLog("Test", "2026-08-11", tmp_path)
    rl.skip("all sources empty")
    data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert data["status"] == "skipped"
    assert data["completed_at"] is not None
    assert data["error"] == "all sources empty"


def test_runlog_fail_finalizes(tmp_path):
    rl = RunLog("Test", "2026-08-11", tmp_path)
    rl.fail("boom")
    data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert data["status"] == "failure"


def test_skip_exits_finalize_runlog_in_main():
    """Every skip sys.exit in main() must be preceded by run_log.skip."""
    src = inspect.getsource(digest_mod.main)
    for chunk in src.split("sys.exit(1")[:-1]:
        pass  # crude: verified via count below
    assert src.count("run_log.skip(msg)") == src.count("sys.exit(10)") + src.count("sys.exit(11)")


def test_main_crash_guard_and_workdir_parents():
    src = inspect.getsource(digest_mod.main)
    assert "run_log = None" in src
    assert "if run_log is not None" in src
    assert "mkdir(parents=True, exist_ok=True)" in src


# ── email HTML escaping ──────────────────────────────────────────────────────

_CFG = {"digest": {"name": "Test", "emoji": "🧪"}, "categories": [], "podcast": {}}


def test_email_html_escapes_ampersand_and_angle_brackets():
    md = "• [**Q&A: AI <matters>**](https://e.com/a) \nBody with 5 < 6 & more."
    html_out = _markdown_to_email_html(md, _CFG)
    assert "Q&amp;A" in html_out
    assert "&lt;matters&gt;" in html_out
    assert "<matters>" not in html_out


def test_email_html_escapes_quote_in_url():
    md = '• [**Title**](https://e.com/a"onmouseover=alert(1))'
    html_out = _markdown_to_email_html(md, _CFG)
    assert 'href="https://e.com/a&quot;onmouseover' in html_out


def test_email_html_links_still_render():
    md = "• [**Title**](https://e.com/a)"
    html_out = _markdown_to_email_html(md, _CFG)
    assert '<a href="https://e.com/a"' in html_out
    assert "<b>Title</b>" in html_out
