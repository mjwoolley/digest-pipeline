"""Tests for digest_pipeline.cartesia — script parsing and text chunking."""
from digest_pipeline.cartesia import parse_script, _chunk_text, MAX_CHUNK_CHARS


# ── parse_script ─────────────────────────────────────────────────────────────

def test_parse_script_multi_turn():
    script = """ALEX: Hello and welcome to the show.
SARAH: Thanks for having me, Alex.
ALEX: Let's dive into today's news."""
    turns = parse_script(script)
    assert len(turns) == 3
    assert turns[0] == ("ALEX", "Hello and welcome to the show.")
    assert turns[1] == ("SARAH", "Thanks for having me, Alex.")
    assert turns[2] == ("ALEX", "Let's dive into today's news.")


def test_parse_script_multiline_turn():
    script = """ALEX: This is a long thought.
It continues on the next line.
SARAH: Got it."""
    turns = parse_script(script)
    assert len(turns) == 2
    assert turns[0] == ("ALEX", "This is a long thought. It continues on the next line.")
    assert turns[1] == ("SARAH", "Got it.")


def test_parse_script_custom_tags():
    script = """HOST: Welcome everyone.
GUEST: Happy to be here."""
    turns = parse_script(script, speaker_tags=["HOST", "GUEST"])
    assert len(turns) == 2
    assert turns[0][0] == "HOST"
    assert turns[1][0] == "GUEST"


def test_parse_script_empty():
    assert parse_script("") == []
    assert parse_script("   \n\n  ") == []


def test_parse_script_no_matching_tags():
    script = "UNKNOWN: This won't match."
    turns = parse_script(script, speaker_tags=["ALEX", "SARAH"])
    assert turns == []


def test_parse_script_skips_blank_lines():
    script = """ALEX: First line.

SARAH: After blank."""
    turns = parse_script(script)
    assert len(turns) == 2


# ── _chunk_text ──────────────────────────────────────────────────────────────

def test_chunk_text_short():
    text = "Hello, world."
    chunks = _chunk_text(text)
    assert chunks == ["Hello, world."]


def test_chunk_text_exact_limit():
    text = "a" * MAX_CHUNK_CHARS
    chunks = _chunk_text(text)
    assert len(chunks) == 1


def test_chunk_text_splits_on_sentences():
    # Build text with sentences that exceed MAX_CHUNK_CHARS
    sentence = "This is a test sentence. "
    text = sentence * (MAX_CHUNK_CHARS // len(sentence) + 5)
    assert len(text) > MAX_CHUNK_CHARS
    chunks = _chunk_text(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= MAX_CHUNK_CHARS


def test_chunk_text_no_punctuation():
    # Text without sentence boundaries forces word-boundary split
    text = "word " * (MAX_CHUNK_CHARS // 5 + 10)
    assert len(text) > MAX_CHUNK_CHARS
    chunks = _chunk_text(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= MAX_CHUNK_CHARS


def test_chunk_text_empty():
    # Empty/whitespace input returns single empty-ish chunk (per implementation)
    # Short-path returns [text] directly; empty string passes the <= MAX check
    assert _chunk_text("") == [""]
    assert _chunk_text("   ") == [""]
