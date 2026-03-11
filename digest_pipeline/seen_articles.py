"""Rolling cross-day deduplication via stored embeddings.

Maintains a JSON file of article titles + embeddings from recent days.
Articles that match historical embeddings above a similarity threshold
are skipped to avoid repeating content across consecutive digests.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from .cluster import cosine_similarity

logger = logging.getLogger(__name__)

STORE_FILENAME = ".seen_embeddings.json"


def load_history(data_root: Path, today: str, lookback_days: int = 5) -> list[dict]:
    """Load historical article records from the last N days, excluding today.

    Returns flat list of {title, embedding} dicts from all days in the window.
    """
    store_path = data_root / STORE_FILENAME
    if not store_path.exists():
        return []

    try:
        store = json.loads(store_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[CROSS-DEDUP] Failed to load history: {e}")
        return []

    today_dt = datetime.strptime(today, "%Y-%m-%d")
    cutoff = (today_dt - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    history = []
    for date_key, entries in store.items():
        if date_key == today:
            continue
        if date_key >= cutoff:
            history.extend(entries)

    return history


def save_today(data_root: Path, date: str, articles: list[dict],
               embeddings: list[list[float]], lookback_days: int = 5) -> None:
    """Save today's article titles + embeddings, pruning old entries."""
    store_path = data_root / STORE_FILENAME

    try:
        store = json.loads(store_path.read_text()) if store_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        store = {}

    # Save today's entries
    store[date] = [
        {"title": a.get("title", ""), "embedding": emb}
        for a, emb in zip(articles, embeddings)
    ]

    # Prune entries older than the lookback window
    cutoff_dt = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=lookback_days)
    cutoff = cutoff_dt.strftime("%Y-%m-%d")
    store = {k: v for k, v in store.items() if k >= cutoff}

    store_path.write_text(json.dumps(store))
    logger.info(f"[CROSS-DEDUP] Saved {len(store[date])} embeddings for {date}")


def filter_seen(articles: list[dict], embeddings: list[list[float]],
                history: list[dict], threshold: float = 0.85
                ) -> tuple[list[dict], list[dict], list[list[float]]]:
    """Filter out articles that match historical embeddings above threshold.

    Returns (kept_articles, skipped_articles, kept_embeddings).
    """
    hist_embeddings = [h["embedding"] for h in history]

    kept_articles = []
    kept_embeddings = []
    skipped = []

    for article, emb in zip(articles, embeddings):
        max_sim = max(
            (cosine_similarity(emb, h_emb) for h_emb in hist_embeddings),
            default=0.0
        )
        if max_sim > threshold:
            title = article.get("title", "(untitled)")
            logger.info(f"[CROSS-DEDUP] Skipping '{title}' (similarity={max_sim:.3f})")
            skipped.append(article)
        else:
            kept_articles.append(article)
            kept_embeddings.append(emb)

    return kept_articles, skipped, kept_embeddings


def parse_digest_markdown(text: str) -> list[dict]:
    """Parse an archived digest markdown file into [{title, description}] dicts.

    Handles both formats: `[**Title**](url)` and `• [**Title**](url)`.
    Description is the text between the title line and `_Why it matters:`.
    """
    articles = []
    # Match article title lines (must have bullet or link bracket):
    #   [**Title**](url)       — link format
    #   • [**Title**](url)     — bullet + link
    #   • **Title**            — bullet, no link (older format)
    # Excludes section headers like **🚀 Model Releases** (no bullet or bracket)
    title_pattern = re.compile(
        r'^\s*(?:•\s*(?:\[)?\*\*(.+?)\*\*|(?:\[)\*\*(.+?)\*\*\])',
        re.MULTILINE
    )

    matches = list(title_pattern.finditer(text))
    for i, match in enumerate(matches):
        title = match.group(1) or match.group(2)
        # Description starts on the next line after the title line
        title_line_end = text.index('\n', match.start()) + 1
        # Find the next _Why it matters:_ line
        why_match = re.search(r'^_Why it matters:', text[title_line_end:],
                              re.MULTILINE)
        if why_match:
            desc = text[title_line_end:title_line_end + why_match.start()].strip()
        else:
            # Fallback: grab until next title or end
            next_start = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            desc = text[title_line_end:next_start].strip()

        if title:
            articles.append({"title": title, "description": desc})

    return articles


def backfill(data_root: Path, lookback_days: int = 5) -> dict[str, int]:
    """Populate .seen_embeddings.json from existing archived digests.

    Scans data_root for YYYY-MM-DD.md files within the lookback window,
    parses articles, embeds them, and saves to the store.

    Returns {date: article_count} for each date processed.
    """
    from . import llm
    from .cluster import embedding_text

    store_path = data_root / STORE_FILENAME
    try:
        existing = json.loads(store_path.read_text()) if store_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        existing = {}

    # Find digest files matching YYYY-MM-DD.md
    date_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2})\.md$')
    today = datetime.now().strftime("%Y-%m-%d")
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    cutoff = (today_dt - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    digest_files = {}
    for f in sorted(data_root.glob("????-??-??.md")):
        m = date_pattern.match(f.name)
        if m:
            date = m.group(1)
            if cutoff <= date <= today:
                digest_files[date] = f

    result = {}
    for date in sorted(digest_files):
        if date in existing:
            logger.info(f"[BACKFILL] Skipping {date} (already in store)")
            continue

        text = digest_files[date].read_text()
        articles = parse_digest_markdown(text)
        if not articles:
            logger.info(f"[BACKFILL] No articles parsed from {date}")
            continue

        texts = [embedding_text(a) for a in articles]
        embeddings, _ = llm.embed(texts)
        save_today(data_root, date, articles, embeddings, lookback_days)
        result[date] = len(articles)
        logger.info(f"[BACKFILL] {date}: embedded {len(articles)} articles")

    return result


def simulate_dedup(data_root: Path, lookback_days: int = 5) -> dict[str, list[str]]:
    """Simulate cross-day dedup across archived digests to show what would be caught.

    Processes dates in order. For each date, loads history from prior days,
    embeds that day's articles, and reports which would have been skipped.

    Returns {date: [list of skipped article titles]}.
    """
    from . import llm
    from .cluster import embedding_text

    date_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2})\.md$')
    today = datetime.now().strftime("%Y-%m-%d")
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    cutoff = (today_dt - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    digest_files = {}
    for f in sorted(data_root.glob("????-??-??.md")):
        m = date_pattern.match(f.name)
        if m:
            date = m.group(1)
            if cutoff <= date <= today:
                digest_files[date] = f

    # Temporary in-memory store for simulation
    sim_store = {}
    result = {}

    for date in sorted(digest_files):
        text = digest_files[date].read_text()
        articles = parse_digest_markdown(text)
        if not articles:
            continue

        texts = [embedding_text(a) for a in articles]
        embeddings, _ = llm.embed(texts)

        # Build history from prior days in sim_store
        history = []
        for d, entries in sim_store.items():
            if d != date:
                history.extend(entries)

        if history:
            _, skipped, _ = filter_seen(articles, embeddings, history, threshold=0.85)
            result[date] = [s["title"] for s in skipped]
        else:
            result[date] = []

        # Save this day to sim_store
        sim_store[date] = [
            {"title": a.get("title", ""), "embedding": emb}
            for a, emb in zip(articles, embeddings)
        ]

    return result
