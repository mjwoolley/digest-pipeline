"""Tests for digest_pipeline.config — pure helpers + config loading with tmp_path."""
import json
from pathlib import Path

from digest_pipeline.config import (
    get_voice_map,
    get_speaker_tags,
    _render_categories,
    _render_sections,
    _render_hosts,
    load_config,
)


# ── get_voice_map ────────────────────────────────────────────────────────────

def test_get_voice_map_normal():
    config = {
        "podcast": {
            "hosts": [
                {"tag": "ALEX", "voice_cartesia": "cart-id-1", "voice_kokoro": "kok-id-1"},
                {"tag": "SARAH", "voice_cartesia": "cart-id-2", "voice_kokoro": "kok-id-2"},
            ]
        }
    }
    result = get_voice_map(config, "cartesia")
    assert result == {"ALEX": "cart-id-1", "SARAH": "cart-id-2"}


def test_get_voice_map_missing_voice_key():
    config = {
        "podcast": {
            "hosts": [
                {"tag": "ALEX", "voice_cartesia": "cart-id-1"},
                {"tag": "SARAH"},  # missing voice_cartesia
            ]
        }
    }
    result = get_voice_map(config, "cartesia")
    assert result == {"ALEX": "cart-id-1"}


def test_get_voice_map_no_podcast():
    assert get_voice_map({}, "cartesia") == {}


# ── get_speaker_tags ─────────────────────────────────────────────────────────

def test_get_speaker_tags_normal():
    config = {
        "podcast": {
            "hosts": [{"tag": "ALEX"}, {"tag": "SARAH"}]
        }
    }
    assert get_speaker_tags(config) == ["ALEX", "SARAH"]


def test_get_speaker_tags_no_hosts():
    assert get_speaker_tags({}) == []
    assert get_speaker_tags({"podcast": {}}) == []


# ── _render_categories ───────────────────────────────────────────────────────

def test_render_categories():
    cats = [
        {"id": "ai", "description": "Artificial Intelligence"},
        {"id": "web", "description": "Web Development"},
    ]
    result = _render_categories(cats)
    assert "- **ai**: Artificial Intelligence" in result
    assert "- **web**: Web Development" in result


def test_render_categories_empty():
    assert _render_categories([]) == ""


# ── _render_sections ─────────────────────────────────────────────────────────

def test_render_sections():
    cats = [
        {"id": "ai", "emoji": "🤖", "label": "AI & ML"},
        {"id": "web", "emoji": "🌐", "label": "Web"},
    ]
    result = _render_sections(cats)
    assert "**🤖 AI & ML**" in result
    assert "[ai items]" in result
    assert "[web items]" in result


# ── _render_hosts ────────────────────────────────────────────────────────────

def test_render_hosts():
    hosts = [
        {"tag": "ALEX", "role": "Main host"},
        {"tag": "SARAH", "role": "Co-host"},
    ]
    result = _render_hosts(hosts)
    assert "- **ALEX**: Main host" in result
    assert "- **SARAH**: Co-host" in result


def test_render_hosts_empty():
    assert _render_hosts([]) == ""


# ── load_config ──────────────────────────────────────────────────────────────

def test_load_config(tmp_path):
    config_data = {"digest": {"name": "Test Digest"}, "sources": {}}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config_data))

    result = load_config(str(config_file))
    assert result["digest"]["name"] == "Test Digest"
    assert result["_data_root"] == tmp_path
    assert isinstance(result["_pipeline_dir"], Path)


def test_load_config_not_found():
    import pytest
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/config.json")


def test_load_config_loads_secrets(tmp_path):
    """secrets.env is loaded from the project root (_pipeline_dir.parent), not _data_root."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"x": 1}))

    # Place secrets.env at the project root (digest_pipeline/../secrets.env)
    import digest_pipeline.config as config_mod
    project_root = Path(config_mod.__file__).resolve().parent.parent
    secrets_file = project_root / "secrets.env"
    already_exists = secrets_file.exists()
    if not already_exists:
        secrets_file.write_text("TEST_DIGEST_SECRET_KEY_XYZ=hello123\n# comment\n\n")
    else:
        # Project root secrets.env exists; check it loads env vars from there
        pass

    import os
    if not already_exists:
        os.environ.pop("TEST_DIGEST_SECRET_KEY_XYZ", None)
        load_config(str(config_file))
        assert os.environ.get("TEST_DIGEST_SECRET_KEY_XYZ") == "hello123"
        # Clean up
        os.environ.pop("TEST_DIGEST_SECRET_KEY_XYZ", None)
        secrets_file.unlink()
    else:
        # Just verify load_config doesn't crash with existing secrets.env
        load_config(str(config_file))
