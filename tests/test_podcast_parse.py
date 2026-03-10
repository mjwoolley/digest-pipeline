"""Tests for podcast script parsing."""
from digest_pipeline.podcast import parse_script


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
