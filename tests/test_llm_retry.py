"""Tests for llm retry/backoff, response-envelope guards, parsing, model map."""
import email.message
import io
import json
import urllib.error
from unittest import mock

import pytest

from digest_pipeline import llm


def _http_error(code, body=b"err", retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://x", code, "msg", headers,
                                  io.BytesIO(body))


def _ok_response(payload):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    return resp


# ── _request_with_retry ──────────────────────────────────────────────────────

def test_429_is_retried(monkeypatch):
    """A single rate-limit hit previously aborted the whole run."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, retry_after=1)
        return _ok_response({"ok": True})

    sleeps = []
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    data = llm._request_with_retry("https://x", b"{}", {})
    assert data == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [1.0]  # Retry-After honored


def test_5xx_is_retried(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise _http_error(503)
        return _ok_response({"ok": True})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm._request_with_retry("https://x", b"{}", {}) == {"ok": True}
    assert len(calls) == 3


def test_4xx_not_retried(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(400, body=b"bad request")

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="API error 400"):
        llm._request_with_retry("https://x", b"{}", {})
    assert len(calls) == 1


def test_retries_exhausted(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(500)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="API error 500"):
        llm._request_with_retry("https://x", b"{}", {})


def test_connection_error_retried(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("dns blip")
        return _ok_response({"ok": True})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm._request_with_retry("https://x", b"{}", {}) == {"ok": True}


def test_backoff_grows_and_caps():
    assert llm._retry_sleep_seconds(0) >= llm.RETRY_BACKOFF
    assert llm._retry_sleep_seconds(10) <= llm.RETRY_MAX_SLEEP * 1.25
    assert llm._retry_sleep_seconds(0, retry_after="7") == 7.0
    assert llm._retry_sleep_seconds(0, retry_after="9999") == llm.RETRY_MAX_SLEEP
    assert llm._retry_sleep_seconds(1, retry_after="soon") > 0  # unparseable ignored


# ── _chat_openrouter envelope guard ──────────────────────────────────────────

def _chat(payload):
    with mock.patch.object(llm, "_request_with_retry", return_value=payload):
        llm._keys["openrouter"] = "test"
        return llm._chat_openrouter([{"role": "user", "content": "hi"}],
                                    "anthropic/claude-haiku-4.5", 100)


def test_openrouter_error_envelope_raises():
    """OpenRouter returns HTTP 200 with {'error': ...} and no choices —
    previously a bare KeyError."""
    with pytest.raises(RuntimeError, match="no choices"):
        _chat({"error": {"message": "credits exhausted", "code": 402}})


def test_openrouter_empty_choices_raises():
    with pytest.raises(RuntimeError, match="no choices"):
        _chat({"choices": []})


def test_openrouter_happy_path():
    text, usage = _chat({
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    assert text == "hello"
    assert usage["cost"] > 0


def test_openrouter_truncation_logged(caplog):
    _chat({
        "choices": [{"message": {"content": "part"}, "finish_reason": "length"}],
        "usage": {},
    })
    assert any("truncated" in r.message.lower() for r in caplog.records)


# ── _parse_json_array strict mode ────────────────────────────────────────────

def test_parse_strict_raises():
    with pytest.raises(RuntimeError, match="unparseable"):
        llm._parse_json_array("no json here", strict=True)


def test_parse_non_strict_returns_empty():
    assert llm._parse_json_array("no json here") == []


def test_parse_recovers_preamble():
    assert llm._parse_json_array('Sure!\n[{"a": 1}]') == [{"a": 1}]


# ── per-stage model resolution ───────────────────────────────────────────────

def test_stage_defaults_writing_stages_on_opus(monkeypatch):
    monkeypatch.setattr(llm, "_provider", "openrouter")
    monkeypatch.setattr(llm, "_stage_overrides", {})
    assert llm.model_for("format") == "anthropic/claude-opus-5"
    assert llm.model_for("dedupe") == "anthropic/claude-opus-5"
    assert llm.model_for("podcast") == "anthropic/claude-opus-5"
    assert llm.model_for("extract") == "anthropic/claude-haiku-4.5"
    assert llm.model_for("relevance") == "anthropic/claude-haiku-4.5"


def test_stage_override_tier(monkeypatch):
    monkeypatch.setattr(llm, "_provider", "openrouter")
    monkeypatch.setattr(llm, "_stage_overrides", {"format": "sonnet"})
    assert llm.model_for("format") == "anthropic/claude-sonnet-4.6"


def test_stage_override_full_model_id(monkeypatch):
    monkeypatch.setattr(llm, "_provider", "openrouter")
    monkeypatch.setattr(llm, "_stage_overrides", {"format": "anthropic/claude-sonnet-5"})
    assert llm.model_for("format") == "anthropic/claude-sonnet-5"


def test_unknown_stage_falls_back_to_haiku(monkeypatch):
    monkeypatch.setattr(llm, "_provider", "openrouter")
    monkeypatch.setattr(llm, "_stage_overrides", {})
    assert llm.model_for("nonexistent") == "anthropic/claude-haiku-4.5"


def test_opus_pricing_present():
    assert llm.PRICING["anthropic/claude-opus-5"] == {"input": 5.00, "output": 25.00}


def test_anthropic_direct_ids_are_valid_aliases(monkeypatch):
    """The direct-Anthropic table previously carried an invalid hybrid id
    (claude-sonnet-4-6-20250514) that would 404."""
    monkeypatch.setattr(llm, "_provider", "anthropic")
    monkeypatch.setattr(llm, "_stage_overrides", {})
    for stage in llm.STAGE_DEFAULTS:
        model = llm.model_for(stage)
        assert "20250514" not in model
        assert model in llm.PRICING
