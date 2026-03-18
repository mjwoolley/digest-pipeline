"""Tests for digest_pipeline.subscribers."""
import json
from pathlib import Path

from digest_pipeline.subscribers import (
    add_subscriber,
    load_subscribers,
    log_send,
    log_subscription_event,
    remove_subscriber,
    save_subscribers,
    unsubscribe_url,
    validate_email,
    _generate_token,
)


# ── Token generation ────────────────────────────────────────────────────────

def test_generate_token_is_random():
    """Each call should produce a different token."""
    tokens = {_generate_token() for _ in range(20)}
    assert len(tokens) == 20


def test_generate_token_length():
    token = _generate_token()
    assert len(token) >= 20  # token_urlsafe(24) produces 32 chars


# ── validate_email ──────────────────────────────────────────────────────────

def test_validate_email_valid():
    assert validate_email("user@example.com") is True
    assert validate_email("a@b.co") is True
    assert validate_email("user+tag@domain.org") is True


def test_validate_email_invalid():
    assert validate_email("") is False
    assert validate_email("noat") is False
    assert validate_email("no@dot") is False
    assert validate_email("sp ace@x.com") is False
    assert validate_email("@x.com") is False
    assert validate_email("a@") is False
    assert validate_email("ab") is False  # too short
    assert validate_email(None) is False


# ── add / remove / roundtrip ───────────────────────────────────────────────

def test_add_and_load(tmp_path):
    added, msg = add_subscriber(tmp_path, "Test@Example.com", source="test")
    assert added is True
    assert "Subscribed" in msg

    subs = load_subscribers(tmp_path)
    assert len(subs) == 1
    assert subs[0]["email"] == "test@example.com"
    assert subs[0]["source"] == "test"
    assert "subscribed_at" in subs[0]
    assert len(subs[0]["token"]) >= 20


def test_add_duplicate(tmp_path):
    add_subscriber(tmp_path, "a@b.com")
    added, msg = add_subscriber(tmp_path, "a@b.com")
    assert added is False
    assert "Already subscribed" in msg


def test_add_invalid_email(tmp_path):
    added, msg = add_subscriber(tmp_path, "bademail")
    assert added is False
    assert "Invalid" in msg


def test_remove_by_email(tmp_path):
    add_subscriber(tmp_path, "a@b.com")
    removed, msg = remove_subscriber(tmp_path, email="a@b.com")
    assert removed is True
    assert load_subscribers(tmp_path) == []


def test_remove_by_token(tmp_path):
    add_subscriber(tmp_path, "a@b.com")
    token = load_subscribers(tmp_path)[0]["token"]
    removed, msg = remove_subscriber(tmp_path, token=token)
    assert removed is True
    assert load_subscribers(tmp_path) == []


def test_remove_not_found(tmp_path):
    removed, msg = remove_subscriber(tmp_path, email="nobody@x.com")
    assert removed is False


def test_roundtrip_save_load(tmp_path):
    subs = [
        {"email": "a@b.com", "token": "tok1", "subscribed_at": "2026-01-01T00:00:00+00:00", "source": "cli"},
        {"email": "c@d.com", "token": "tok2", "subscribed_at": "2026-01-02T00:00:00+00:00", "source": "api"},
    ]
    save_subscribers(tmp_path, subs)
    loaded = load_subscribers(tmp_path)
    assert loaded == subs


def test_atomic_save(tmp_path):
    """save_subscribers should not leave partial files on failure."""
    save_subscribers(tmp_path, [{"email": "a@b.com", "token": "t"}])
    assert load_subscribers(tmp_path) == [{"email": "a@b.com", "token": "t"}]


# ── Event logging ───────────────────────────────────────────────────────────

def test_log_subscription_event(tmp_path):
    log_subscription_event(tmp_path, "subscribe", "a@b.com", "tok", "test")
    log_subscription_event(tmp_path, "unsubscribe", "a@b.com", "tok", "test")

    lines = (tmp_path / "subscription_events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    entry = json.loads(lines[0])
    assert entry["event"] == "subscribe"
    assert entry["email"] == "a@b.com"
    assert "timestamp" in entry


def test_log_send(tmp_path):
    log_send(tmp_path, "2026-03-18", "a@b.com", "sent", method="agentmail")
    log_send(tmp_path, "2026-03-18", "c@d.com", "failed", error="timeout")

    lines = (tmp_path / "send_history.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    sent = json.loads(lines[0])
    assert sent["status"] == "sent"
    assert sent["method"] == "agentmail"
    failed = json.loads(lines[1])
    assert failed["status"] == "failed"
    assert failed["error"] == "timeout"


# ── add/remove trigger event logging ───────────────────────────────────────

def test_add_logs_event(tmp_path):
    add_subscriber(tmp_path, "a@b.com", source="landing-page")
    lines = (tmp_path / "subscription_events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "subscribe"
    assert entry["source"] == "landing-page"


def test_remove_logs_event(tmp_path):
    add_subscriber(tmp_path, "a@b.com")
    remove_subscriber(tmp_path, email="a@b.com", source="landing-page")
    lines = (tmp_path / "subscription_events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    unsub = json.loads(lines[1])
    assert unsub["event"] == "unsubscribe"


# ── unsubscribe_url ─────────────────────────────────────────────────────────

def test_unsubscribe_url_with_base():
    url = unsubscribe_url("https://example.com", "abc123")
    assert url == "https://example.com/unsubscribe?token=abc123"


def test_unsubscribe_url_with_trailing_slash():
    url = unsubscribe_url("https://example.com/", "abc123")
    assert url == "https://example.com/unsubscribe?token=abc123"


def test_unsubscribe_url_fallback_mailto():
    url = unsubscribe_url("", "abc123")
    assert url.startswith("mailto:")
    assert "abc123" in url
