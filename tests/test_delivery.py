"""Tests for digest_pipeline.delivery — pure helpers, no network calls."""
import io
import json
import urllib.error

from digest_pipeline import delivery
from digest_pipeline.delivery import _split_message, _fmt_tokens, MAX_MSG_LEN


# ── _split_message ───────────────────────────────────────────────────────────

def test_split_message_short():
    msg = "Hello, world!"
    assert _split_message(msg) == ["Hello, world!"]


def test_split_message_exact_limit():
    msg = "a" * MAX_MSG_LEN
    assert _split_message(msg) == [msg]


def test_split_message_splits_on_newline():
    # Build a message that exceeds MAX_MSG_LEN with newlines
    line = "x" * 100 + "\n"
    msg = line * (MAX_MSG_LEN // len(line) + 5)
    assert len(msg) > MAX_MSG_LEN
    chunks = _split_message(msg)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= MAX_MSG_LEN


def test_split_message_no_newlines():
    # Long message without newlines — hard cut at MAX_MSG_LEN
    msg = "x" * (MAX_MSG_LEN + 500)
    chunks = _split_message(msg)
    assert len(chunks) == 2
    assert len(chunks[0]) == MAX_MSG_LEN
    assert len(chunks[1]) == 500


def test_split_message_empty():
    assert _split_message("") == [""]


def test_split_message_multiple_splits():
    # Three chunks worth of content
    msg = ("a" * 4000 + "\n") * 3  # ~12003 chars, MAX_MSG_LEN=4096
    chunks = _split_message(msg)
    assert len(chunks) == 3
    for chunk in chunks:
        assert len(chunk) <= MAX_MSG_LEN


# ── _fmt_tokens ──────────────────────────────────────────────────────────────

def test_fmt_tokens_small():
    assert _fmt_tokens(0) == "0"
    assert _fmt_tokens(999) == "999"


def test_fmt_tokens_medium():
    assert _fmt_tokens(1000) == "1.0K"
    assert _fmt_tokens(5500) == "5.5K"


def test_fmt_tokens_large():
    assert _fmt_tokens(10000) == "10K"
    assert _fmt_tokens(100000) == "100K"


# ── _post_telegram_message ───────────────────────────────────────────────────

class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_400():
    return urllib.error.HTTPError(
        "https://api.telegram.org", 400, "Bad Request", None,
        io.BytesIO(b'{"ok":false,"description":"can\'t parse entities"}'))


def test_post_telegram_retries_plain_text_on_400(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data)
        sent.append(body)
        if "parse_mode" in body:
            raise _http_400()
        return _FakeResp()

    monkeypatch.setattr(delivery.urllib.request, "urlopen", fake_urlopen)
    text = "FAILED: lower max_tokens (limit_source: openrouter_credits)"
    assert delivery._post_telegram_message(text, "tok", "chat") is True
    assert [b.get("parse_mode") for b in sent] == ["Markdown", None]
    assert sent[1]["text"] == text


def test_post_telegram_gives_up_when_plain_text_also_400(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http_400()

    monkeypatch.setattr(delivery.urllib.request, "urlopen", fake_urlopen)
    assert delivery._post_telegram_message("x", "tok", "chat") is False
    assert len(calls) == 2


# ── send_alert email ─────────────────────────────────────────────────────────

def _alert_cfg(**delivery_extra):
    return {"digest": {"name": "Test Digest"},
            "delivery": {"notify": {"method": "none"}, **delivery_extra}}


def test_send_alert_emails_configured_address(monkeypatch):
    sent = []
    monkeypatch.setattr(delivery, "send_email",
                        lambda subject, body, config, to_override=None, **kw:
                        sent.append((subject, body, to_override)))
    cfg = _alert_cfg(alert_email={"to": "ops@example.com"})
    assert delivery.send_alert("Pipeline", "boom <b>", cfg) is True
    assert len(sent) == 1
    subject, body, to = sent[0]
    assert to == "ops@example.com"
    assert "Test Digest failed (Pipeline)" in subject
    assert "boom &lt;b&gt;" in body


def test_send_alert_without_alert_email_sends_no_email(monkeypatch):
    sent = []
    monkeypatch.setattr(delivery, "send_email", lambda *a, **kw: sent.append(a))
    assert delivery.send_alert("Pipeline", "boom", _alert_cfg()) is True
    assert sent == []


def test_send_alert_survives_email_failure(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("Resend down")
    monkeypatch.setattr(delivery, "send_email", boom)
    cfg = _alert_cfg(alert_email={"to": "ops@example.com"})
    assert delivery.send_alert("Pipeline", "boom", cfg) is True
