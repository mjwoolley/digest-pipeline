"""Pronunciation rewrite layer for podcast TTS.

Transforms turn text after script parsing and before TTS synthesis.
Rewrites are ephemeral — only the TTS engine sees them.
"""

import logging
import re

logger = logging.getLogger(__name__)


def build_rewriter(config: dict) -> callable:
    """Build a rewriter function from podcast.pronunciation config.

    Returns a callable that takes a string and returns the rewritten string.
    If no pronunciation config exists, returns an identity function.
    """
    pron = config.get("podcast", {}).get("pronunciation")
    if not pron:
        return lambda text: text

    terms = pron.get("terms", {})
    regex_rules = pron.get("regex", [])

    # Sort terms longest-first so longer matches win
    sorted_terms = sorted(terms.items(), key=lambda kv: len(kv[0]), reverse=True)

    # Build a single regex alternation for all terms
    if sorted_terms:
        parts = []
        for term, _ in sorted_terms:
            escaped = re.escape(term)
            # Use lookarounds for boundary detection instead of \b,
            # since \b doesn't work cleanly with non-word chars like ( ) &
            parts.append(f"(?<![\\w])(?:{escaped})(?![\\w])")
        term_pattern = re.compile("|".join(parts), re.IGNORECASE)
        # Build a lookup keyed by lowercase for case-insensitive matching
        term_lookup = {k.lower(): v for k, v in terms.items()}
    else:
        term_pattern = None
        term_lookup = None

    # Pre-compile regex rules
    compiled_regex = []
    for rule in regex_rules:
        compiled_regex.append((
            re.compile(rule["pattern"]),
            rule["replacement"],
        ))

    def rewrite(text: str) -> str:
        # Terms first
        if term_pattern:
            text = term_pattern.sub(
                lambda m: term_lookup[m.group(0).lower()], text
            )
        # Regex second
        for pattern, replacement in compiled_regex:
            text = pattern.sub(replacement, text)
        return text

    return rewrite


def apply_rewrites(
    turns: list[tuple[str, str]], rewriter: callable
) -> list[tuple[str, str]]:
    """Apply pronunciation rewrites to turn text.

    Returns a new list — originals are not mutated.
    Speaker labels are preserved unchanged.
    """
    rewritten = []
    for speaker, text in turns:
        new_text = rewriter(text)
        if new_text != text:
            logger.debug(f"[PRONUNCIATION] {speaker}: {text!r} -> {new_text!r}")
        rewritten.append((speaker, new_text))
    return rewritten
