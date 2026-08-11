"""Shared low-level helpers: atomic JSON writes and LLM-output JSON extraction.

Atomic writes exist because the pipeline's state files (.source_state.json,
.seen_embeddings.json, run.json, subscribers.json) are the memory of what has
already been sent to readers — a truncated write from a crash or full disk
silently degrades to "forget everything" and re-publishes old content.
"""
import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, data, indent: int = 2) -> None:
    """Write JSON to ``path`` atomically via tempfile + os.replace."""
    payload = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
    atomic_write_text(path, payload)


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to ``path`` atomically via tempfile + os.replace."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# ── JSON extraction from LLM output ─────────────────────────────────────────
#
# One shared implementation. The pipeline previously had three divergent
# copies (llm._parse_json_array, relevance._extract_json_object,
# twitter_discovery._extract_json_object), and the weakest one guarded the
# most important call path.

def strip_code_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def extract_json_array(text: str) -> list:
    """Extract a JSON array from LLM output.

    Strips code fences, then falls back to slicing from the first ``[`` to
    the last ``]`` (models sometimes emit a preamble sentence or trailing
    commentary). Raises ValueError if no valid array can be recovered.
    """
    return _extract(text, "[", "]", list)


def extract_json_object(text: str) -> dict:
    """Extract a JSON object from LLM output. Raises ValueError on failure."""
    return _extract(text, "{", "}", dict)


def _extract(text: str, open_ch: str, close_ch: str, expected_type: type):
    cleaned = strip_code_fences(text)
    try:
        result = json.loads(cleaned)
        if isinstance(result, expected_type):
            return result
    except json.JSONDecodeError:
        pass
    start = cleaned.find(open_ch)
    end = cleaned.rfind(close_ch)
    if start != -1 and end > start:
        try:
            result = json.loads(cleaned[start:end + 1])
            if isinstance(result, expected_type):
                return result
        except json.JSONDecodeError:
            pass
    raise ValueError(
        f"No JSON {expected_type.__name__} found in LLM output: {text[:200]!r}")
