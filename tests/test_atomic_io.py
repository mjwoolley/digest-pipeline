"""Tests for digest_pipeline.util atomic writes and shared JSON extraction."""
import json

import pytest

from digest_pipeline import util
from digest_pipeline.util import (
    atomic_write_json, atomic_write_text,
    extract_json_array, extract_json_object, strip_code_fences,
)


# ── atomic_write_json / atomic_write_text ────────────────────────────────────

def test_atomic_write_json_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"a": 1, "emoji": "🔗"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "emoji": "🔗"}


def test_atomic_write_json_compact(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"a": [1, 2]}, indent=None)
    assert path.read_text(encoding="utf-8").count("\n") == 1  # trailing newline only


def test_atomic_write_failure_leaves_original(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"v": 1})

    def boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(util.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(path, {"v": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 1}
    assert [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")] == []


def test_atomic_write_text_no_tmp_left_on_success(tmp_path):
    path = tmp_path / "out.txt"
    atomic_write_text(path, "hello")
    assert path.read_text(encoding="utf-8") == "hello"
    assert list(tmp_path.iterdir()) == [path]


def test_state_writers_use_atomic_write():
    """The five state writers must all route through util's atomic write."""
    import inspect

    from digest_pipeline import (
        config_writer, console_api, run_log, seen_articles, source_state, subscribers,
    )
    for mod, func in [
        (source_state, source_state.save_state),
        (seen_articles, seen_articles.save_today),
        (subscribers, subscribers.save_subscribers),
        (console_api, console_api._clean_source_state),
    ]:
        src = inspect.getsource(func)
        assert "atomic_write_json" in src, f"{mod.__name__} not atomic"
    assert "atomic_write_json" in inspect.getsource(run_log.RunLog._flush)
    assert "atomic_write_text" in inspect.getsource(config_writer._atomic_write_json)


# ── strip_code_fences ────────────────────────────────────────────────────────

def test_strip_fences_json_lang():
    assert strip_code_fences('```json\n[1]\n```') == "[1]"


def test_strip_fences_none():
    assert strip_code_fences('[1]') == "[1]"


# ── extract_json_array ───────────────────────────────────────────────────────

def test_array_plain():
    assert extract_json_array('[{"t": "A"}]') == [{"t": "A"}]


def test_array_fenced():
    assert extract_json_array('```json\n[{"t": "A"}]\n```') == [{"t": "A"}]


def test_array_with_preamble_and_trailer():
    text = 'Here are the articles:\n[{"t": "A"}, {"t": "B"}]\nLet me know!'
    assert extract_json_array(text) == [{"t": "A"}, {"t": "B"}]


def test_array_nested_brackets():
    text = 'Sure: [{"tags": ["x", "y"]}] done'
    assert extract_json_array(text) == [{"tags": ["x", "y"]}]


def test_array_malformed_raises():
    with pytest.raises(ValueError):
        extract_json_array('[{"t": "A"')


def test_array_object_input_raises():
    with pytest.raises(ValueError):
        extract_json_array('{"t": "A"}')


def test_array_empty_input_raises():
    with pytest.raises(ValueError):
        extract_json_array('')


# ── extract_json_object ──────────────────────────────────────────────────────

def test_object_plain():
    assert extract_json_object('{"relevant": true}') == {"relevant": True}


def test_object_with_preamble():
    text = 'The decision is:\n{"relevant": false, "reason": "off-topic"}'
    assert extract_json_object(text) == {"relevant": False, "reason": "off-topic"}


def test_object_fenced_with_trailer():
    text = '```json\n{"score": 7}\n```\nHope that helps.'
    assert extract_json_object(text) == {"score": 7}


def test_object_malformed_raises():
    with pytest.raises(ValueError):
        extract_json_object('not json at all')
