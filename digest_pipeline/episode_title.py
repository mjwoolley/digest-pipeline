"""One-line episode/digest title generation.

A single LLM-generated headline is reused in three places so they always match:
the podcast RSS item title, the landing page, and the email subject line. The
digest stage generates it once (from the finished digest) and writes
``<date>.title``; the podcast stage reuses that file. Kept in its own module so
both ``digest.py`` and ``podcast.py`` can import it without coupling to each
other.
"""

import logging

from .config import render_prompt
from . import llm


def clean(raw: str, max_chars: int = 100) -> str:
    """Normalise an LLM-generated title: one line, no wrapping quotes/period, capped."""
    title = " ".join((raw or "").split())
    title = title.strip().strip('"').strip("'").strip().rstrip(".").strip()
    if len(title) > max_chars:
        title = title[:max_chars].rstrip() + "…"
    return title


def generate(source_text: str, config: dict, logger: logging.Logger,
             tracker=None) -> str:
    """Generate a one-line title from digest/script text via the cheap model.

    Returns "" on any failure so callers can fall back to a date-based title.
    When a TokenTracker is passed, the call's usage is recorded (it was
    previously discarded, understating the reported run cost).
    """
    if not (source_text or "").strip():
        return ""
    try:
        provider = config.get("llm", {}).get("provider", "openrouter")
        llm.configure(provider, config.get("llm", {}).get("models"))
        prompt = render_prompt("episode_title.md", config, {"DIGEST": source_text})
        model = llm.model_for("title")
        raw, usage = llm.chat([{"role": "user", "content": prompt}], model,
                              max_tokens=64)
        if tracker is not None:
            tracker.add("Title", model, usage, 0)
        return clean(raw)
    except Exception as e:
        logger.warning(f"[TITLE] Title generation failed: {e}")
        return ""
