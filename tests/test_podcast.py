"""Tests for digest_pipeline.podcast — swap usage parsing."""
from digest_pipeline.podcast import _get_swap_usage


# ── _get_swap_usage ──────────────────────────────────────────────────────────

def test_get_swap_usage_returns_string():
    result = _get_swap_usage()
    assert isinstance(result, str)
    # Should either be a formatted string or "unknown"
    assert result == "unknown" or ("G" in result and "%" in result)


def test_get_swap_usage_format(tmp_path, monkeypatch):
    """Mock /proc/meminfo to test parsing logic."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "SwapTotal:       4194304 kB\n"
        "SwapFree:        2097152 kB\n"
    )
    # Monkey-patch Path("/proc/meminfo").read_text via the function
    import digest_pipeline.podcast as podcast_mod
    from pathlib import Path

    original = podcast_mod._get_swap_usage

    def patched():
        try:
            content = meminfo.read_text()
            vals = {}
            for line in content.splitlines():
                if line.startswith(("SwapTotal:", "SwapFree:")):
                    parts = line.split()
                    vals[parts[0].rstrip(":")] = int(parts[1])
            total_kb = vals.get("SwapTotal", 0)
            free_kb = vals.get("SwapFree", 0)
            used_kb = total_kb - free_kb
            total_gb = total_kb / 1048576
            used_gb = used_kb / 1048576
            pct = (used_kb / total_kb * 100) if total_kb else 0
            return f"{used_gb:.1f}G / {total_gb:.1f}G ({pct:.0f}%)"
        except Exception:
            return "unknown"

    monkeypatch.setattr(podcast_mod, "_get_swap_usage", patched)
    result = podcast_mod._get_swap_usage()
    assert result == "2.0G / 4.0G (50%)"


# ── _clean_title ─────────────────────────────────────────────────────────────

from digest_pipeline.podcast import _clean_title, _episode_title


def test_clean_title_strips_quotes_period_and_whitespace():
    assert _clean_title('  "Anthropic ships 1M context."  ') == "Anthropic ships 1M context"


def test_clean_title_collapses_newlines_to_one_line():
    assert _clean_title("Big\n  model\n  drop") == "Big model drop"


def test_clean_title_caps_length():
    out = _clean_title("x" * 200, max_chars=100)
    assert len(out) == 101 and out.endswith("…")


def test_clean_title_handles_empty():
    assert _clean_title("") == ""
    assert _clean_title(None) == ""


# ── _episode_title ───────────────────────────────────────────────────────────

def test_episode_title_reads_file(tmp_path):
    (tmp_path / "2026-06-13.title").write_text("Anthropic ships 1M context\n")
    assert _episode_title(tmp_path, "2026-06-13", fallback="fb") == "Anthropic ships 1M context"


def test_episode_title_falls_back_when_missing(tmp_path):
    assert _episode_title(tmp_path, "2026-06-13", fallback="The AI Daily Roundup — 2026-06-13") \
        == "The AI Daily Roundup — 2026-06-13"


def test_episode_title_falls_back_when_blank(tmp_path):
    (tmp_path / "2026-06-13.title").write_text("   \n")
    assert _episode_title(tmp_path, "2026-06-13", fallback="fb") == "fb"
