"""Per-source incremental processing state.

Tracks which items from each source have already been processed,
so the pipeline can skip them before expensive LLM extraction.
State is persisted in {data_root}/.source_state.json.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("digest")

STATE_FILENAME = ".source_state.json"

# Default cooldown for GitHub Trending repos (days).
# Repos seen within this window are suppressed; after it expires they can reappear.
DEFAULT_TRENDING_COOLDOWN_DAYS = 14

# Tweet blocks are separated by this line
_TWEET_SEPARATOR = "──────────────────────────────────────────────────"
# Tweet URL pattern: 🔗 https://x.com/user/status/ID
_TWEET_URL_RE = re.compile(r"🔗\s+(https://x\.com/\S+)")
# TITLE/LINK block item separator
_BLOCK_SEPARATOR = "---"
# LINK line in TITLE/LINK blocks
_LINK_RE = re.compile(r"^LINK:\s*(\S+)", re.MULTILINE)


def load_state(data_root: Path) -> dict:
    """Load source state from disk. Returns {source_key: {seen_ids: [...], last_updated: "..."}}."""
    path = data_root / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[SOURCE-STATE] Failed to load state: {e}")
        return {}


def save_state(data_root: Path, state: dict) -> None:
    """Write source state to disk."""
    path = data_root / STATE_FILENAME
    path.write_text(json.dumps(state, indent=2))


def prune_state(state: dict, max_age_days: int = 7) -> dict:
    """Remove entries older than max_age_days."""
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    return {
        k: v for k, v in state.items()
        if v.get("last_updated", "9999-99-99") >= cutoff
    }


# ── Item ID extraction ──────────────────────────────────────────────────────

def _extract_twitter_items(content: str) -> list[tuple[str, str]]:
    """Extract (item_id, item_block) pairs from twitter content.

    Each tweet is separated by the unicode line separator.
    Item ID is the tweet URL from the 🔗 line.
    """
    if not content.strip():
        return []
    blocks = content.split(_TWEET_SEPARATOR)
    items = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = _TWEET_URL_RE.search(block)
        if m:
            items.append((m.group(1), block))
        else:
            # No URL found — use content hash as fallback
            h = hashlib.sha256(block.encode()).hexdigest()[:16]
            items.append((f"hash:{h}", block))
    return items


def _extract_block_items(content: str) -> list[tuple[str, str]]:
    """Extract (item_id, item_block) pairs from TITLE/LINK/--- formatted content.

    Used for RSS/Atom blogs, HTML scrape, and GitHub trending.
    Item ID is the LINK URL.
    """
    if not content.strip():
        return []
    blocks = content.split(_BLOCK_SEPARATOR)
    items = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = _LINK_RE.search(block)
        if m:
            items.append((m.group(1), block))
        else:
            h = hashlib.sha256(block.encode()).hexdigest()[:16]
            items.append((f"hash:{h}", block))
    return items


def _extract_newsletter_id(content: str, message_id: str = None) -> str | None:
    """Generate an ID for newsletter content.

    Prefers Message-ID header (stable across fetches) over content hash.
    Falls back to hash of first 1000 chars if no Message-ID available.
    """
    if message_id:
        return f"msgid:{message_id}"
    if not content.strip():
        return None
    return "hash:" + hashlib.sha256(content[:1000].encode()).hexdigest()[:32]


# ── Filtering ────────────────────────────────────────────────────────────────

def extract_and_filter(source: dict, seen_ids: set[str]) -> tuple[dict, list[str]]:
    """Filter already-seen items from a gathered source.

    Args:
        source: Gathered source dict with source_key, source_type, content.
        seen_ids: Set of previously seen item IDs for this source.

    Returns:
        (filtered_source, new_ids) — source with only new items, and their IDs.
    """
    content = source.get("content", "")
    source_type = source.get("source_type", "")

    if not content.strip():
        return source, []

    if source_type == "twitter":
        return _filter_twitter(source, content, seen_ids)
    elif source_type in ("blog", "research", "github_trending"):
        return _filter_blocks(source, content, seen_ids)
    elif source_type == "newsletter":
        return _filter_newsletter(source, content, seen_ids)
    else:
        # Unknown source type — pass through
        return source, []


def _filter_twitter(source: dict, content: str,
                    seen_ids: set[str]) -> tuple[dict, list[str]]:
    """Filter seen tweets, rebuild content from remaining blocks."""
    items = _extract_twitter_items(content)
    new_blocks = []
    new_ids = []
    for item_id, block in items:
        if item_id not in seen_ids:
            new_blocks.append(block)
            new_ids.append(item_id)
    if not new_blocks:
        filtered = {**source, "content": ""}
    else:
        filtered = {**source, "content": ("\n" + _TWEET_SEPARATOR + "\n\n").join(new_blocks)}
    return filtered, new_ids


def _filter_blocks(source: dict, content: str,
                   seen_ids: set[str]) -> tuple[dict, list[str]]:
    """Filter seen items from TITLE/LINK/--- formatted content."""
    items = _extract_block_items(content)
    new_blocks = []
    new_ids = []
    for item_id, block in items:
        if item_id not in seen_ids:
            new_blocks.append(block)
            new_ids.append(item_id)
    if not new_blocks:
        filtered = {**source, "content": ""}
    else:
        filtered = {**source, "content": ("\n" + _BLOCK_SEPARATOR + "\n").join(new_blocks)}
    return filtered, new_ids


def _filter_newsletter(source: dict, content: str,
                       seen_ids: set[str]) -> tuple[dict, list[str]]:
    """Filter newsletter by Message-ID or content hash (whole-source granularity)."""
    message_id = source.get("message_id")
    nl_id = _extract_newsletter_id(content, message_id=message_id)
    if nl_id and nl_id in seen_ids:
        return {**source, "content": ""}, []
    return source, [nl_id] if nl_id else []


def _get_seen_ids_for_source(source_key: str, entry: dict) -> set[str]:
    """Build set of currently-suppressed IDs from a state entry.

    For github_trending sources, seen_ids is a dict {id: "YYYY-MM-DD"} with
    timestamp-based cooldown. Only IDs within the cooldown window are suppressed.
    For all other sources, seen_ids is a plain list.
    """
    seen_ids_raw = entry.get("seen_ids", [])
    if source_key.startswith("github_trending:") and isinstance(seen_ids_raw, dict):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cutoff = (datetime.utcnow() - timedelta(days=DEFAULT_TRENDING_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        return {item_id for item_id, first_seen in seen_ids_raw.items()
                if first_seen >= cutoff}
    return set(seen_ids_raw)


def filter_gathered_sources(sources: list[dict], state: dict,
                            ) -> tuple[list[dict], dict[str, list[str]]]:
    """Filter all gathered sources using persisted state.

    Args:
        sources: List of gathered source dicts.
        state: Current source state (from load_state).

    Returns:
        (filtered_sources, pending_ids) where pending_ids maps
        source_key -> list of new item IDs to commit after success.
    """
    filtered = []
    pending_ids: dict[str, list[str]] = {}
    total_skipped = 0

    for source in sources:
        source_key = source["source_key"]
        entry = state.get(source_key, {})
        seen = _get_seen_ids_for_source(source_key, entry)
        filtered_source, new_ids = extract_and_filter(source, seen)
        filtered.append(filtered_source)
        if new_ids:
            pending_ids[source_key] = new_ids

        # Count skipped items
        orig_content = source.get("content", "").strip()
        filt_content = filtered_source.get("content", "").strip()
        if orig_content and not filt_content:
            total_skipped += 1
            logger.info(f"[SOURCE-STATE] {source_key}: all items previously seen, skipping")
        elif orig_content and len(new_ids) < _count_items(source):
            skipped = _count_items(source) - len(new_ids)
            total_skipped += skipped
            logger.info(f"[SOURCE-STATE] {source_key}: filtered {skipped} seen items, "
                        f"{len(new_ids)} new")

    if total_skipped:
        logger.info(f"[SOURCE-STATE] Total: filtered {total_skipped} previously seen items")

    return filtered, pending_ids


def commit_pending(state: dict, pending_ids: dict[str, list[str]],
                   date: str, max_ids: int = 500) -> dict:
    """Merge pending IDs into state after successful processing.

    For github_trending sources, stores {id: date} dicts for cooldown tracking.
    For all other sources, stores flat ID lists (rolling window).

    Args:
        state: Current state dict (mutated in place).
        pending_ids: source_key -> new item IDs from this run.
        date: Current date string (YYYY-MM-DD).
        max_ids: Max IDs to retain per source (rolling window).

    Returns:
        Updated state dict.
    """
    for source_key, new_ids in pending_ids.items():
        entry = state.get(source_key, {"seen_ids": [], "last_updated": ""})
        if source_key.startswith("github_trending:"):
            # Timestamped dict: {url: "YYYY-MM-DD"}
            existing = entry.get("seen_ids", {})
            if isinstance(existing, list):
                # Migrate from old list format: assign current date to legacy IDs
                existing = {item_id: date for item_id in existing}
            for item_id in new_ids:
                if item_id not in existing:
                    existing[item_id] = date
            # Prune expired cooldowns
            cutoff = (datetime.utcnow() - timedelta(days=DEFAULT_TRENDING_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
            existing = {k: v for k, v in existing.items() if v >= cutoff}
            # Cap size
            if len(existing) > max_ids:
                # Keep most recently seen
                sorted_items = sorted(existing.items(), key=lambda x: x[1])
                existing = dict(sorted_items[-max_ids:])
            state[source_key] = {
                "seen_ids": existing,
                "last_updated": date,
            }
        else:
            combined = entry.get("seen_ids", []) + new_ids
            # Keep only the most recent max_ids
            if len(combined) > max_ids:
                combined = combined[-max_ids:]
            state[source_key] = {
                "seen_ids": combined,
                "last_updated": date,
            }
    return state


def update_source_state(state: dict, pending_ids: dict[str, list[str]],
                        fetched_keys: set[str], included_keys: set[str],
                        date: str, max_ids: int = 500) -> dict:
    """Backward-compatible helper expected by digest.py.

    Updates per-source metadata for fetched/included timestamps and commits
    pending item IDs after a successful run.
    """
    state = {**state}
    for key in fetched_keys:
        entry = state.setdefault(key, {})
        entry["last_fetched"] = date
    for key in included_keys:
        entry = state.setdefault(key, {})
        entry["last_included"] = date
    return commit_pending(state, pending_ids, date, max_ids=max_ids)


def _count_items(source: dict) -> int:
    """Count items in a source based on its type."""
    content = source.get("content", "")
    source_type = source.get("source_type", "")
    if not content.strip():
        return 0
    if source_type == "twitter":
        return len(_extract_twitter_items(content))
    elif source_type in ("blog", "research", "github_trending"):
        return len(_extract_block_items(content))
    elif source_type == "newsletter":
        return 1
    return 0
