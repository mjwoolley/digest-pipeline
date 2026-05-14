"""Targeted, atomic edits to a digest's ``config.json``.

Only the Twitter ``accounts`` list is editable here, because that's what the
``/discover-twitter`` slash command needs. Other config sections are
deliberately not exposed — config.json is small and human-edited; a CLI
that can poke at arbitrary keys would invite the slash command to drift
into general config management.

Concurrency: each call re-reads the file fresh and writes via temp file +
``os.replace()`` so a concurrent edit from another session won't be
silently overwritten by stale in-memory state. A genuine TOCTOU race
remains (read → modify → replace), but for a single-user repo the worst
case is one session's add overwriting the other's — acceptable. Add a
file lock if that ever becomes a real problem.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AddResult:
    added: list[str]
    already_present: list[str]
    config_path: str


def add_twitter_accounts(
    config_path: Path | str,
    handles: list[str],
    *,
    dry_run: bool = False,
) -> AddResult:
    """Append handles to ``sources.twitter.accounts``, case-insensitively deduped.

    The input casing the user provided is preserved when a handle is new;
    case-insensitive duplicates against the existing list (or earlier in
    the same call) go into ``already_present`` instead.
    """
    if not handles:
        raise ValueError("handles list is empty")

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    sources = cfg.get("sources")
    if not isinstance(sources, dict) or "twitter" not in sources:
        raise ValueError(f"config at {path} has no sources.twitter block")
    twitter = sources["twitter"]
    if not isinstance(twitter, dict):
        raise ValueError(f"sources.twitter in {path} is not an object")

    accounts = twitter.get("accounts")
    if accounts is None:
        accounts = []
        twitter["accounts"] = accounts
    if not isinstance(accounts, list):
        raise ValueError(f"sources.twitter.accounts in {path} is not a list")

    seen_lower = {str(a).lower() for a in accounts}
    added: list[str] = []
    already_present: list[str] = []
    for raw in handles:
        h = str(raw).strip().lstrip("@")
        if not h:
            continue
        key = h.lower()
        if key in seen_lower:
            already_present.append(h)
            continue
        accounts.append(h)
        seen_lower.add(key)
        added.append(h)

    if added and not dry_run:
        _atomic_write_json(path, cfg)

    return AddResult(added=added, already_present=already_present, config_path=str(path))


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to ``path`` atomically via tempfile + os.replace."""
    payload = json.dumps(data, indent=2) + "\n"
    dir_ = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(dir_),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
