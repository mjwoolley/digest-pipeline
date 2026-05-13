"""Tests for digest_pipeline.source_history — per-source historical ledger."""
import json
import re
from pathlib import Path

from digest_pipeline import source_history


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _seed_day(work_root: Path, date: str, *, extracted, prioritized=None,
              dropped=None, deduped=None):
    day = work_root / date
    day.mkdir(parents=True, exist_ok=True)
    _write_json(day / "extracted.json", extracted)
    if prioritized is not None:
        _write_json(day / "prioritized.json", prioritized)
    if dropped is not None:
        _write_json(day / "prioritized_dropped.json", dropped)
    if deduped is not None:
        _write_json(day / "deduped.json", deduped)
    return day


def test_compute_daily_rows_basic(tmp_path):
    """Sources get one row each with extracted/in_digest/avg_score."""
    day = _seed_day(
        tmp_path, "2026-05-13",
        extracted=[
            {"title": "A", "source_key": "blog:foo"},
            {"title": "B", "source_key": "blog:foo"},
            {"title": "C", "source_key": "twitter:bar"},
        ],
        prioritized=[
            {"title": "A", "source_keys": ["blog:foo"], "_priority_score": 8},
            {"title": "C", "source_keys": ["twitter:bar"], "_priority_score": 6},
        ],
        dropped=[{"title": "B", "source_keys": ["blog:foo"], "_priority_score": 4}],
        deduped=[
            {"title": "A", "source_keys": ["blog:foo"]},
            {"title": "B", "source_keys": ["blog:foo"]},
            {"title": "C", "source_keys": ["twitter:bar"]},
        ],
    )
    rows = source_history.compute_daily_rows(day, "2026-05-13")
    by_key = {r["source_key"]: r for r in rows}

    foo = by_key["blog:foo"]
    assert foo["extracted"] == 2
    assert foo["in_digest"] == 1  # only article A made the kept list
    assert foo["dropped"] == 1
    assert foo["exclusive"] == 1  # A is the sole source on A
    # avg_score across kept + dropped from blog:foo: mean(8, 4) = 6.0
    assert foo["avg_score"] == 6.0

    bar = by_key["twitter:bar"]
    assert bar["in_digest"] == 1
    assert bar["dropped"] == 0
    assert bar["avg_score"] == 6.0


def test_compute_daily_rows_skipped_prioritize(tmp_path):
    """When prioritize.json is absent, in_digest reads from deduped.json."""
    day = _seed_day(
        tmp_path, "2026-05-13",
        extracted=[
            {"title": "A", "source_key": "blog:foo"},
            {"title": "B", "source_key": "twitter:bar"},
        ],
        deduped=[
            {"title": "A", "source_keys": ["blog:foo"]},
            {"title": "B", "source_keys": ["twitter:bar"]},
        ],
    )
    rows = source_history.compute_daily_rows(day, "2026-05-13")
    by_key = {r["source_key"]: r for r in rows}
    assert by_key["blog:foo"]["in_digest"] == 1
    assert by_key["blog:foo"]["dropped"] == 0
    assert by_key["blog:foo"]["avg_score"] is None  # no scores on skipped-prio day
    assert by_key["twitter:bar"]["in_digest"] == 1


def test_compute_daily_rows_skips_zero_extracted(tmp_path):
    """Sources mentioned only in dedup/prioritize but with no extraction
    shouldn't appear (defensive — shouldn't happen in practice)."""
    day = _seed_day(
        tmp_path, "2026-05-13",
        extracted=[{"title": "A", "source_key": "blog:foo"}],
        prioritized=[
            {"title": "A", "source_keys": ["blog:foo"], "_priority_score": 5},
        ],
    )
    rows = source_history.compute_daily_rows(day, "2026-05-13")
    assert len(rows) == 1
    assert rows[0]["source_key"] == "blog:foo"


def test_append_daily_creates_file(tmp_path):
    work = tmp_path / "work"
    _seed_day(
        work, "2026-05-13",
        extracted=[{"title": "A", "source_key": "blog:foo"}],
        deduped=[{"title": "A", "source_keys": ["blog:foo"]}],
    )
    n = source_history.append_daily(tmp_path, work / "2026-05-13", "2026-05-13")
    assert n == 1
    ledger = (tmp_path / "source_history.jsonl").read_text().splitlines()
    assert len(ledger) == 1
    row = json.loads(ledger[0])
    assert row["date"] == "2026-05-13"
    assert row["source_key"] == "blog:foo"
    assert row["extracted"] == 1


def test_append_daily_idempotent(tmp_path):
    """Re-appending for the same date short-circuits."""
    work = tmp_path / "work"
    _seed_day(
        work, "2026-05-13",
        extracted=[{"title": "A", "source_key": "blog:foo"}],
        deduped=[{"title": "A", "source_keys": ["blog:foo"]}],
    )
    source_history.append_daily(tmp_path, work / "2026-05-13", "2026-05-13")
    n = source_history.append_daily(tmp_path, work / "2026-05-13", "2026-05-13")
    assert n == 0
    ledger = (tmp_path / "source_history.jsonl").read_text().splitlines()
    assert len(ledger) == 1  # not duplicated


def test_append_daily_appends_to_existing(tmp_path):
    """A new date appends without re-checking earlier rows."""
    work = tmp_path / "work"
    _seed_day(work, "2026-05-12",
              extracted=[{"title": "A", "source_key": "blog:foo"}],
              deduped=[{"title": "A", "source_keys": ["blog:foo"]}])
    _seed_day(work, "2026-05-13",
              extracted=[{"title": "B", "source_key": "blog:foo"}],
              deduped=[{"title": "B", "source_keys": ["blog:foo"]}])

    source_history.append_daily(tmp_path, work / "2026-05-12", "2026-05-12")
    source_history.append_daily(tmp_path, work / "2026-05-13", "2026-05-13")

    lines = (tmp_path / "source_history.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["date"] == "2026-05-12"
    assert json.loads(lines[1])["date"] == "2026-05-13"


def test_rebuild_walks_all_days(tmp_path):
    """rebuild() processes every work/<date>/ dir in sorted order, overwriting
    the ledger."""
    work = tmp_path / "work"
    _seed_day(work, "2026-05-11",
              extracted=[{"title": "A", "source_key": "blog:foo"}],
              deduped=[{"title": "A", "source_keys": ["blog:foo"]}])
    _seed_day(work, "2026-05-13",
              extracted=[{"title": "B", "source_key": "twitter:bar"}],
              deduped=[{"title": "B", "source_keys": ["twitter:bar"]}])

    # Pre-seed the ledger with a row that should get overwritten.
    (tmp_path / "source_history.jsonl").write_text(
        json.dumps({"date": "1999-01-01", "source_key": "stale:row"}) + "\n"
    )

    total = source_history.rebuild(tmp_path, DATE_RE)
    assert total == 2
    lines = (tmp_path / "source_history.jsonl").read_text().splitlines()
    dates = [json.loads(l)["date"] for l in lines]
    assert dates == ["2026-05-11", "2026-05-13"]


def test_rebuild_handles_missing_work_dir(tmp_path):
    """No work/ dir — ledger gets cleared."""
    (tmp_path / "source_history.jsonl").write_text(
        json.dumps({"date": "2026-05-13", "source_key": "x"}) + "\n"
    )
    total = source_history.rebuild(tmp_path, DATE_RE)
    assert total == 0
    assert not (tmp_path / "source_history.jsonl").exists()
