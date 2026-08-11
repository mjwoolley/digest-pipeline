"""Offline replay of the new dedup gates against the archived digests.

Runs only with `pytest -m replay` (excluded by default via addopts) — it
reads the real digests/ai archive, so it's a regression harness rather
than a unit test.
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "replay_dedup", REPO / "scripts" / "replay_dedup.py")
replay_dedup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_dedup)


@pytest.mark.replay
def test_known_duplicate_stories_are_caught():
    days = replay_dedup.load_archive(REPO / "digests" / "ai")
    assert days, "no archived digests found"
    result = replay_dedup.replay(days, lookback=5, title_threshold=0.6)

    caught_titles = {t for _, t, _ in result["catches"]}
    # The repeat offenders identified in the audit (same story shipped on
    # consecutive days, including one that ran five days straight).
    for marker in ("OpenSandbox", "DeerFlow", "RuFlo"):
        assert any(marker in t for t in caught_titles), f"{marker} not caught"

    # The gates must catch a substantial share of repeats without being
    # trigger-happy: the audit found ~97 near-duplicate pairs plus exact
    # re-ships; anything under 50 means a layer is dead.
    total_caught = result["caught_url"] + result["caught_title"]
    assert total_caught >= 50
    # Sanity ceiling: we should not be suppressing most of the digest.
    assert total_caught < result["articles"] * 0.3
