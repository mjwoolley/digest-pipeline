"""Tests for digest_pipeline.delivery — pure helpers, no network calls."""
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
