"""Tests for digest_pipeline.source_audit."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from digest_pipeline import source_audit
from digest_pipeline.source_audit import (
    CAUSE_CONTENT_NO_ARTICLES,
    CAUSE_EXTRACTED_BUT_LOST,
    CAUSE_FETCH_EMPTY,
    CAUSE_HEALTHY,
    CAUSE_NEVER_YIELDED,
    CAUSE_NORMAL_LOW_CADENCE,
    CAUSE_NOT_FETCHED,
    CAUSE_YIELD_DROP,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

def _shift(today: str, days: int) -> str:
    return (date.fromisoformat(today) - timedelta(days=days)).isoformat()


def _write_state(data_root: Path, state: dict) -> None:
    (data_root / ".source_state.json").write_text(json.dumps(state))


def _write_ledger(data_root: Path, rows: list[dict]) -> None:
    (data_root / "source_history.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else "")
    )


def _seed_work_day(data_root: Path, day: str, *, raw_files: dict[str, str] = None,
                   extracted: list[dict] = None, run_json: dict = None) -> None:
    """Create a work/<day>/ directory with the given artifacts."""
    work = data_root / "work" / day
    work.mkdir(parents=True, exist_ok=True)
    if raw_files:
        for fname, content in raw_files.items():
            (work / fname).write_text(content)
    if extracted is not None:
        (work / "extracted.json").write_text(json.dumps(extracted))
    if run_json is not None:
        (work / "run.json").write_text(json.dumps(run_json))


@pytest.fixture
def data_root(tmp_path):
    root = tmp_path / "ai"
    root.mkdir()
    return root


def _config_with_twitter(*accts):
    return {
        "digest": {"name": "Test Digest"},
        "sources": {"twitter": {"accounts": list(accts)}},
    }


def _config_with_blog(key, stale_after_days=3):
    return {
        "digest": {"name": "Test Digest"},
        "sources": {"blogs": {key: {"name": key, "feed_url": "x", "stale_after_days": stale_after_days}}},
    }


# ── Bucket tests ────────────────────────────────────────────────────────────

def test_healthy_source_returns_healthy(data_root):
    today = "2026-05-13"
    _write_state(data_root, {
        "twitter:claudeai": {"last_fetched": today, "last_included": today, "seen_ids": []},
    })
    _write_ledger(data_root, [
        {"date": today, "source_key": "twitter:claudeai", "extracted": 3, "in_digest": 2,
         "dropped": 0, "exclusive": 1, "avg_score": 7.0},
    ])
    report = source_audit.audit(data_root, _config_with_twitter("claudeai"), today=today)
    f = report.findings[0]
    assert f.cause == CAUSE_HEALTHY
    assert report.healthy_count == 1
    assert report.actionable == []


def test_never_yielded_after_grace_window(data_root):
    today = "2026-05-13"
    old = _shift(today, 30)  # outside the 14-day grace
    _write_state(data_root, {
        "twitter:dead_handle": {"last_fetched": old, "last_included": None, "seen_ids": []},
    })
    _write_ledger(data_root, [])
    report = source_audit.audit(data_root, _config_with_twitter("dead_handle"), today=today)
    f = report.findings[0]
    assert f.cause == CAUSE_NEVER_YIELDED
    assert "no article ever produced" in f.evidence


def test_never_yielded_within_grace_window_is_healthy(data_root):
    today = "2026-05-13"
    recent = _shift(today, 5)  # inside the 14-day grace
    _write_state(data_root, {
        "twitter:newbie": {"last_fetched": recent, "last_included": None, "seen_ids": []},
    })
    _write_ledger(data_root, [])
    report = source_audit.audit(data_root, _config_with_twitter("newbie"), today=today)
    assert report.findings[0].cause == CAUSE_HEALTHY


def test_not_fetched_when_no_raw_files(data_root):
    today = "2026-05-13"
    old = _shift(today, 30)
    _write_state(data_root, {
        "twitter:missing": {"last_fetched": old, "last_included": old, "seen_ids": []},
    })
    # Has prior ledger activity but the recent work-dirs don't have a raw file.
    _write_ledger(data_root, [
        {"date": _shift(today, 40), "source_key": "twitter:missing", "extracted": 1,
         "in_digest": 1, "dropped": 0, "exclusive": 0, "avg_score": 5.0},
    ])
    for offset in range(1, 4):
        _seed_work_day(data_root, _shift(today, offset), raw_files={"raw-twitter-other.txt": "x"})
    report = source_audit.audit(data_root, _config_with_twitter("missing"), today=today)
    assert report.findings[0].cause == CAUSE_NOT_FETCHED


def test_fetch_empty_when_raw_files_all_tiny(data_root):
    today = "2026-05-13"
    old = _shift(today, 30)
    _write_state(data_root, {
        "twitter:cursorai": {"last_fetched": old, "last_included": old, "seen_ids": []},
    })
    _write_ledger(data_root, [
        {"date": _shift(today, 60), "source_key": "twitter:cursorai", "extracted": 1,
         "in_digest": 1, "dropped": 0, "exclusive": 0, "avg_score": 5.0},
    ])
    for offset in range(1, 4):
        _seed_work_day(
            data_root, _shift(today, offset),
            raw_files={"raw-twitter-cursorai.txt": "No tweets found."},
        )
    report = source_audit.audit(data_root, _config_with_twitter("cursorai"), today=today)
    f = report.findings[0]
    assert f.cause == CAUSE_FETCH_EMPTY
    assert "No tweets found" in f.evidence


def test_content_no_articles_when_extracted_is_zero(data_root):
    today = "2026-05-13"
    old = _shift(today, 30)
    _write_state(data_root, {
        "twitter:zenoware": {"last_fetched": today, "last_included": old, "seen_ids": []},
    })
    # Historical activity but nothing in stale_after_days (3) window.
    _write_ledger(data_root, [
        {"date": _shift(today, 60), "source_key": "twitter:zenoware", "extracted": 2,
         "in_digest": 2, "dropped": 0, "exclusive": 1, "avg_score": 7.0},
    ])
    # Recent runs: hefty raw file, zero articles extracted.
    for offset in range(1, 4):
        _seed_work_day(
            data_root, _shift(today, offset),
            raw_files={"raw-twitter-zenoware.txt": "X" * 2000},
            extracted=[],  # nothing produced from this source's content
        )
    report = source_audit.audit(data_root, _config_with_twitter("zenoware"), today=today)
    f = report.findings[0]
    assert f.cause == CAUSE_CONTENT_NO_ARTICLES
    assert "off-topic" in f.suggested_action.lower()


def test_extracted_but_lost_when_in_digest_is_zero(data_root):
    today = "2026-05-13"
    old = _shift(today, 30)
    _write_state(data_root, {
        "twitter:mistralai": {"last_fetched": today, "last_included": old, "seen_ids": []},
    })
    # Recent ledger rows all have extracted > 0 but in_digest = 0.
    _write_ledger(data_root, [
        {"date": _shift(today, 1), "source_key": "twitter:mistralai", "extracted": 2,
         "in_digest": 0, "dropped": 2, "exclusive": 0, "avg_score": 4.0},
        {"date": _shift(today, 2), "source_key": "twitter:mistralai", "extracted": 1,
         "in_digest": 0, "dropped": 1, "exclusive": 0, "avg_score": 3.5},
    ])
    report = source_audit.audit(data_root, _config_with_twitter("mistralai"), today=today)
    f = report.findings[0]
    assert f.cause == CAUSE_EXTRACTED_BUT_LOST
    assert "none made the digest" in f.evidence


def test_normal_low_cadence_for_sparse_sources(data_root):
    """When the badge is non-healthy but the quiet streak is within the
    historical median gap, classify as NORMAL_LOW_CADENCE (not alarm).
    Set stale_after_days low enough that the badge trips, then prove
    cadence analysis catches it."""
    today = "2026-05-13"
    _write_state(data_root, {
        "blog:one_useful_thing": {
            "last_fetched": _shift(today, 12),
            "last_included": _shift(today, 12),
            "seen_ids": [],
        },
    })
    # Historical cadence: gaps of ~14 days between active days.
    rows = []
    for gap in (12, 27, 41, 56, 70, 84):
        rows.append({
            "date": _shift(today, gap), "source_key": "blog:one_useful_thing",
            "extracted": 1, "in_digest": 1, "dropped": 0, "exclusive": 0, "avg_score": 8.0,
        })
    _write_ledger(data_root, rows)
    # Stale_after_days set low so badge is "stale" — the test is whether the
    # cadence analysis classifies the quiet stretch as NORMAL_LOW_CADENCE.
    cfg = _config_with_blog("one_useful_thing", stale_after_days=7)
    report = source_audit.audit(data_root, cfg, today=today)
    f = report.findings[0]
    assert f.cause == CAUSE_NORMAL_LOW_CADENCE
    assert "no action" in f.suggested_action.lower()


def test_yield_drop_on_healthy_source(data_root):
    today = "2026-05-13"
    _write_state(data_root, {
        "twitter:swyx": {"last_fetched": today, "last_included": today, "seen_ids": []},
    })
    # Baseline: 1 article/day for 90 days.
    rows = []
    for offset in range(15, 90):  # mid-history, plenty before recent window
        rows.append({
            "date": _shift(today, offset), "source_key": "twitter:swyx",
            "extracted": 1, "in_digest": 1, "dropped": 0, "exclusive": 0, "avg_score": 7.0,
        })
    # Recent 14 days: only one row (drops below 40% of baseline 0.83/day).
    rows.append({
        "date": _shift(today, 1), "source_key": "twitter:swyx",
        "extracted": 1, "in_digest": 1, "dropped": 0, "exclusive": 0, "avg_score": 7.0,
    })
    _write_ledger(data_root, rows)
    report = source_audit.audit(data_root, _config_with_twitter("swyx"), today=today)
    f = report.findings[0]
    assert f.cause == CAUSE_YIELD_DROP
    assert "averaging" in f.evidence.lower()


# ── Formatter tests ─────────────────────────────────────────────────────────

def test_format_telegram_groups_by_bucket(data_root):
    today = "2026-05-13"
    # Two stale sources (one NEVER_YIELDED, one FETCH_EMPTY), one healthy.
    _write_state(data_root, {
        "twitter:claudeai": {"last_fetched": today, "last_included": today, "seen_ids": []},
        "twitter:dead": {"last_fetched": _shift(today, 30), "last_included": None, "seen_ids": []},
        "twitter:emptyfeed": {"last_fetched": _shift(today, 30), "last_included": _shift(today, 30), "seen_ids": []},
    })
    _write_ledger(data_root, [
        {"date": today, "source_key": "twitter:claudeai", "extracted": 2, "in_digest": 1,
         "dropped": 0, "exclusive": 0, "avg_score": 6.0},
        {"date": _shift(today, 50), "source_key": "twitter:emptyfeed", "extracted": 1,
         "in_digest": 1, "dropped": 0, "exclusive": 0, "avg_score": 5.0},
    ])
    for offset in range(1, 4):
        _seed_work_day(
            data_root, _shift(today, offset),
            raw_files={"raw-twitter-emptyfeed.txt": "No tweets found."},
        )
    cfg = _config_with_twitter("claudeai", "dead", "emptyfeed")
    report = source_audit.audit(data_root, cfg, today=today)

    msg = source_audit.format_telegram(report, console_url="http://example/console")
    assert "Source Audit" in msg
    assert "Action recommended" in msg
    assert "twitter:dead" in msg
    assert "twitter:emptyfeed" in msg
    # Healthy source should not appear in the actionable buckets.
    assert "twitter:claudeai" not in msg
    assert "Healthy: 1 of 3" in msg
    assert "Detail: http://example/console" in msg


def test_format_telegram_all_healthy(data_root):
    today = "2026-05-13"
    _write_state(data_root, {
        "twitter:a": {"last_fetched": today, "last_included": today, "seen_ids": []},
    })
    _write_ledger(data_root, [
        {"date": today, "source_key": "twitter:a", "extracted": 1, "in_digest": 1,
         "dropped": 0, "exclusive": 0, "avg_score": 7.0},
    ])
    cfg = _config_with_twitter("a")
    report = source_audit.audit(data_root, cfg, today=today)
    msg = source_audit.format_telegram(report)
    assert "All 1 sources healthy" in msg


def test_format_telegram_truncates_at_cap(data_root, monkeypatch):
    """Synthetic report with many findings → message length capped."""
    today = "2026-05-13"
    state = {}
    ledger = []
    accts = [f"acct{i:03d}" for i in range(200)]
    for a in accts:
        state[f"twitter:{a}"] = {"last_fetched": _shift(today, 30), "last_included": None, "seen_ids": []}
    _write_state(data_root, state)
    _write_ledger(data_root, ledger)
    cfg = _config_with_twitter(*accts)
    report = source_audit.audit(data_root, cfg, today=today)
    msg = source_audit.format_telegram(report)
    # Cap is 3800 chars; truncation marker present when needed.
    assert len(msg) <= source_audit.TELEGRAM_LENGTH_CAP
    if len(msg) >= source_audit.TELEGRAM_LENGTH_CAP - 50:
        assert "truncated" in msg
