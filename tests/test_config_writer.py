"""Tests for digest_pipeline.config_writer.add_twitter_accounts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digest_pipeline import config_writer, util
from digest_pipeline.config_writer import AddResult, add_twitter_accounts


def _write_cfg(path: Path, accounts: list[str] | None = None) -> None:
    cfg = {
        "digest": {"name": "Test"},
        "sources": {"twitter": {"accounts": list(accounts or [])}},
    }
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def _read_accounts(path: Path) -> list[str]:
    return json.loads(path.read_text())["sources"]["twitter"]["accounts"]


def test_adds_new_handles_preserving_input_order(tmp_path: Path):
    p = tmp_path / "config.json"
    _write_cfg(p, accounts=["existing"])
    result = add_twitter_accounts(p, ["alice", "bob", "carol"])
    assert isinstance(result, AddResult)
    assert result.added == ["alice", "bob", "carol"]
    assert result.already_present == []
    assert _read_accounts(p) == ["existing", "alice", "bob", "carol"]


def test_case_insensitive_dedup_against_existing(tmp_path: Path):
    p = tmp_path / "config.json"
    _write_cfg(p, accounts=["karpathy"])
    result = add_twitter_accounts(p, ["Karpathy", "swyx"])
    assert result.added == ["swyx"]
    assert result.already_present == ["Karpathy"]
    # Existing entry preserved at its original casing; no duplicate appended.
    assert _read_accounts(p) == ["karpathy", "swyx"]


def test_case_insensitive_dedup_within_one_call(tmp_path: Path):
    p = tmp_path / "config.json"
    _write_cfg(p)
    result = add_twitter_accounts(p, ["alice", "ALICE", "Alice"])
    assert result.added == ["alice"]
    assert result.already_present == ["ALICE", "Alice"]
    assert _read_accounts(p) == ["alice"]


def test_strips_leading_at_sign(tmp_path: Path):
    p = tmp_path / "config.json"
    _write_cfg(p)
    result = add_twitter_accounts(p, ["@alice", "  @bob  "])
    assert result.added == ["alice", "bob"]
    assert _read_accounts(p) == ["alice", "bob"]


def test_dry_run_does_not_write(tmp_path: Path):
    p = tmp_path / "config.json"
    _write_cfg(p, accounts=["existing"])
    original = p.read_text()
    result = add_twitter_accounts(p, ["alice"], dry_run=True)
    assert result.added == ["alice"]
    assert p.read_text() == original


def test_missing_twitter_block_raises(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"digest": {"name": "x"}, "sources": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="sources.twitter"):
        add_twitter_accounts(p, ["alice"])


def test_missing_accounts_list_initialises_it(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"digest": {"name": "x"}, "sources": {"twitter": {}}}),
        encoding="utf-8",
    )
    result = add_twitter_accounts(p, ["alice"])
    assert result.added == ["alice"]
    assert _read_accounts(p) == ["alice"]


def test_accounts_not_a_list_raises(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"sources": {"twitter": {"accounts": "not-a-list"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not a list"):
        add_twitter_accounts(p, ["alice"])


def test_empty_handles_raises(tmp_path: Path):
    p = tmp_path / "config.json"
    _write_cfg(p)
    with pytest.raises(ValueError, match="empty"):
        add_twitter_accounts(p, [])


def test_missing_config_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        add_twitter_accounts(tmp_path / "nope.json", ["alice"])


def test_atomic_write_failure_leaves_original_intact(tmp_path: Path, monkeypatch):
    """If os.replace raises, the original file must not be partially updated."""
    p = tmp_path / "config.json"
    _write_cfg(p, accounts=["existing"])
    original = p.read_text()

    real_replace = util.os.replace

    def boom(src, dst):
        raise OSError("simulated crash mid-replace")

    monkeypatch.setattr(util.os, "replace", boom)
    with pytest.raises(OSError):
        add_twitter_accounts(p, ["alice"])
    # Original file unchanged.
    assert p.read_text() == original
    # And no leftover .tmp files in the directory.
    leftovers = [f for f in p.parent.iterdir() if f.name.endswith(".tmp")]
    assert leftovers == []

    # Sanity: with real replace restored, it still works.
    monkeypatch.setattr(util.os, "replace", real_replace)
    result = add_twitter_accounts(p, ["alice"])
    assert result.added == ["alice"]


def test_preserves_other_config_keys(tmp_path: Path):
    """Writing the twitter accounts list must not drop unrelated keys."""
    p = tmp_path / "config.json"
    cfg = {
        "digest": {"name": "Test", "max_articles": 20},
        "podcast": {"enabled": True},
        "sources": {
            "twitter": {"accounts": ["existing"], "lookback": "36h"},
            "blogs": {"x": {"feed_url": "http://example.com"}},
        },
    }
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    add_twitter_accounts(p, ["alice"])
    after = json.loads(p.read_text())
    assert after["digest"]["max_articles"] == 20
    assert after["podcast"]["enabled"] is True
    assert after["sources"]["twitter"]["lookback"] == "36h"
    assert after["sources"]["blogs"]["x"]["feed_url"] == "http://example.com"
    assert after["sources"]["twitter"]["accounts"] == ["existing", "alice"]
