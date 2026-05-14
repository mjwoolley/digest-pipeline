"""Smoke test for `digest-pipeline --discover-twitter --json`.

Verifies that stdout is exclusively valid JSON (no log noise) when --json
is set. The slash command parses this output, so the contract matters.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from digest_pipeline import cli, twitter_discovery
from digest_pipeline.twitter_discovery import Candidate, DiscoveryReport


def _write_digest_config(digests_dir: Path, slug: str) -> Path:
    cfg_dir = digests_dir / slug
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(json.dumps({
        "digest": {"name": "Test"},
        "delivery": {"notify": {}},
        "sources": {
            "twitter": {
                "accounts": [],
                "discovery": {"enabled": True, "keywords": ["AI"]},
            }
        },
    }), encoding="utf-8")
    return cfg_path


def test_discover_twitter_json_stdout_is_pure_json(tmp_path, monkeypatch, capsys):
    digests_dir = tmp_path / "digests"
    _write_digest_config(digests_dir, "ai")

    # Build a stub DiscoveryReport so the test doesn't shell out to bird.
    fake_report = DiscoveryReport(
        digest_name="Test", digest_slug="ai", date="2026-05-14",
        enabled=True, keywords_searched=["AI"],
        candidates=[
            Candidate(
                handle="ada", name="Ada", bio="builds AI",
                followers=10_000, posts_per_week=2.5, avg_engagement=12.0,
                score=4.0, matched_keywords=["AI"], sample_tweets=2,
                llm_score=8, llm_rationale="ships",
                tweet_samples=["tweet a", "tweet b"],
            ),
        ],
    )
    monkeypatch.setattr(twitter_discovery, "discover", lambda cfg: fake_report)

    # Run via the same entry point the user invokes.
    monkeypatch.setattr(sys, "argv", [
        "digest-pipeline",
        "--discover-twitter",
        "--json",
        "--digests-dir", str(digests_dir),
        "--digest", "ai",
    ])
    cli.main()

    captured = capsys.readouterr()
    # stdout must be valid JSON, nothing else.
    parsed = json.loads(captured.out)
    assert parsed["digest_slug"] == "ai"
    assert parsed["candidates"][0]["handle"] == "ada"
    assert parsed["candidates"][0]["tweet_samples"] == ["tweet a", "tweet b"]
    # Logs may or may not appear on stderr; the contract is just that
    # stdout is clean JSON.


def test_discover_twitter_json_requires_digest_slug(tmp_path, monkeypatch, capsys):
    digests_dir = tmp_path / "digests"
    digests_dir.mkdir()
    monkeypatch.setattr(sys, "argv", [
        "digest-pipeline",
        "--discover-twitter",
        "--json",
        "--digests-dir", str(digests_dir),
    ])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "--digest" in captured.err


def test_add_twitter_account_appends_to_config(tmp_path, monkeypatch, capsys):
    digests_dir = tmp_path / "digests"
    cfg_path = _write_digest_config(digests_dir, "ai")
    monkeypatch.setattr(sys, "argv", [
        "digest-pipeline",
        "--add-twitter-account", "alice", "@bob",
        "--digest", "ai",
        "--digests-dir", str(digests_dir),
    ])
    cli.main()
    cfg = json.loads(cfg_path.read_text())
    assert cfg["sources"]["twitter"]["accounts"] == ["alice", "bob"]
    captured = capsys.readouterr()
    assert "Added" in captured.out
    assert "@alice" in captured.out
    assert "@bob" in captured.out


def test_add_twitter_account_dry_run_leaves_config_alone(tmp_path, monkeypatch, capsys):
    digests_dir = tmp_path / "digests"
    cfg_path = _write_digest_config(digests_dir, "ai")
    before = cfg_path.read_text()
    monkeypatch.setattr(sys, "argv", [
        "digest-pipeline",
        "--add-twitter-account", "alice",
        "--digest", "ai",
        "--digests-dir", str(digests_dir),
        "--dry-run",
    ])
    cli.main()
    assert cfg_path.read_text() == before
    captured = capsys.readouterr()
    assert "Would add" in captured.out


def test_add_twitter_account_requires_digest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "digest-pipeline",
        "--add-twitter-account", "alice",
        "--digests-dir", str(tmp_path),
    ])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0
