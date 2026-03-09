"""Tests for digest_pipeline.llm — pure helpers, no API calls."""
from digest_pipeline.llm import _parse_json_array, _calc_cost, _load_keys


# ── _parse_json_array ────────────────────────────────────────────────────────

def test_parse_json_array_simple():
    result = _parse_json_array('[{"title": "A"}, {"title": "B"}]')
    assert len(result) == 2
    assert result[0]["title"] == "A"


def test_parse_json_array_with_markdown_fences():
    text = '```json\n[{"title": "A"}]\n```'
    result = _parse_json_array(text)
    assert len(result) == 1
    assert result[0]["title"] == "A"


def test_parse_json_array_with_bare_fences():
    text = '```\n[{"x": 1}]\n```'
    result = _parse_json_array(text)
    assert len(result) == 1


def test_parse_json_array_not_array():
    result = _parse_json_array('{"title": "A"}')
    assert result == []


def test_parse_json_array_invalid_json():
    result = _parse_json_array('this is not json at all')
    assert result == []


def test_parse_json_array_empty_array():
    result = _parse_json_array('[]')
    assert result == []


def test_parse_json_array_whitespace():
    result = _parse_json_array('  \n  [{"a": 1}]  \n  ')
    assert len(result) == 1


def test_parse_json_array_nested_fences_content():
    # Fence with language tag
    text = '```json\n[{"code": "hello"}]\n```'
    result = _parse_json_array(text)
    assert result[0]["code"] == "hello"


# ── _calc_cost ───────────────────────────────────────────────────────────────

def test_calc_cost_haiku():
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    cost = _calc_cost("anthropic/claude-haiku-4.5", usage)
    # input: 1000 * 0.80 / 1M = 0.0008, output: 500 * 4.00 / 1M = 0.002
    expected = (1000 * 0.80 + 500 * 4.00) / 1_000_000
    assert abs(cost - expected) < 1e-10


def test_calc_cost_sonnet():
    usage = {"input_tokens": 2000, "output_tokens": 1000}
    cost = _calc_cost("anthropic/claude-sonnet-4.6", usage)
    expected = (2000 * 3.00 + 1000 * 15.00) / 1_000_000
    assert abs(cost - expected) < 1e-10


def test_calc_cost_embedding():
    usage = {"prompt_tokens": 5000, "completion_tokens": 0}
    cost = _calc_cost("openai/text-embedding-3-small", usage)
    expected = (5000 * 0.02) / 1_000_000
    assert abs(cost - expected) < 1e-10


def test_calc_cost_unknown_model():
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    cost = _calc_cost("unknown/model", usage)
    assert cost == 0.0


def test_calc_cost_zero_tokens():
    cost = _calc_cost("anthropic/claude-haiku-4.5", {"prompt_tokens": 0, "completion_tokens": 0})
    assert cost == 0.0


def test_calc_cost_anthropic_direct_model():
    usage = {"input_tokens": 1000, "output_tokens": 500}
    cost = _calc_cost("claude-haiku-4-5-20251001", usage)
    expected = (1000 * 0.80 + 500 * 4.00) / 1_000_000
    assert abs(cost - expected) < 1e-10


# ── _load_keys ───────────────────────────────────────────────────────────────

def test_load_keys_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    keys = _load_keys()
    assert keys["openrouter"] == "or-test-key"
    assert "anthropic" not in keys


def test_load_keys_both_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    keys = _load_keys()
    assert keys["openrouter"] == "or-key"
    assert keys["anthropic"] == "ant-key"


def test_load_keys_no_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    keys = _load_keys()
    assert keys == {}
