"""Cross-source, cross-day duplicate detection primitives.

Two cheap layers that run before (and independently of) embedding similarity:

1. A **global canonical-URL index** of everything shipped in recent digests.
   The per-source seen-ID state can't catch the same URL arriving via a
   different source (it's keyed by source_key); this index is global.
2. **Lexical title matching** — normalized-token Jaccard similarity. Two
   outlets covering the same story usually share the headline vocabulary
   even when their body prose diverges enough to drag embedding similarity
   under any reasonable threshold.

Persisted state: {data_root}/.shipped_urls.json — {canonical_url: "YYYY-MM-DD"}.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .util import atomic_write_json

logger = logging.getLogger("digest")

URL_INDEX_FILENAME = ".shipped_urls.json"
DEFAULT_URL_LOOKBACK_DAYS = 14

# Tracking params that don't change the destination document
_TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|ref|ref_src|ref_url|fbclid|gclid|mc_cid|mc_eid|igshid|si|s|feature)$",
    re.IGNORECASE,
)


def canonicalize_url(url: str) -> str:
    """Normalize a URL so trivially different links to the same page compare equal.

    Lowercases scheme/host, drops www., default ports, fragments, tracking
    query params, AMP suffixes, and trailing slashes. Returns "" for
    unusable input.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return url.lower()

    path = parts.path or "/"
    # AMP variants: /amp suffix or amp. subdomain
    if host.startswith("amp."):
        host = host[4:]
    if path.endswith("/amp") or path.endswith("/amp/"):
        path = path[: path.rfind("/amp")]
    path = re.sub(r"/+$", "", path) or "/"

    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAMS.match(k) and k.lower() != "amp"
    ]
    query = urlencode(sorted(query_pairs))

    return urlunsplit(("https", host, path, query, ""))


def article_urls(article: dict) -> list[str]:
    """All URLs attached to an article (handles both url and urls fields)."""
    urls = article.get("urls") or []
    single = article.get("url")
    if single:
        urls = list(urls) + [single]
    return [u for u in urls if u]


# ── Shipped-URL index ────────────────────────────────────────────────────────

def load_url_index(data_root: Path) -> dict[str, str]:
    """Load {canonical_url: date_shipped}. Missing/corrupt file -> empty."""
    path = Path(data_root) / URL_INDEX_FILENAME
    if not path.exists():
        return {}
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(index, dict):
            return index
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[URL-INDEX] Failed to load: {e}")
    return {}


def save_url_index(data_root: Path, index: dict[str, str],
                   today: str, lookback_days: int = DEFAULT_URL_LOOKBACK_DAYS) -> None:
    """Persist the index, pruning entries older than the lookback window."""
    cutoff = (datetime.strptime(today, "%Y-%m-%d")
              - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    pruned = {u: d for u, d in index.items() if d >= cutoff}
    atomic_write_json(Path(data_root) / URL_INDEX_FILENAME, pruned)


def filter_by_url_index(articles: list[dict], index: dict[str, str]
                        ) -> tuple[list[dict], list[dict]]:
    """Drop articles whose canonical URL was already shipped.

    Returns (kept, skipped). Skipped articles get _skip_reason set.
    """
    kept, skipped = [], []
    for a in articles:
        hit = None
        for u in article_urls(a):
            cu = canonicalize_url(u)
            if cu and cu in index:
                hit = (cu, index[cu])
                break
        if hit:
            a["_skip_reason"] = f"url shipped {hit[1]}: {hit[0]}"
            skipped.append(a)
            logger.info(f"[URL-INDEX] Skipping '{a.get('title', '(untitled)')}' "
                        f"(shipped {hit[1]})")
        else:
            kept.append(a)
    return kept, skipped


def record_shipped(index: dict[str, str], articles: list[dict],
                   date: str) -> dict[str, str]:
    """Add the canonical URLs of shipped articles to the index."""
    for a in articles:
        for u in article_urls(a):
            cu = canonicalize_url(u)
            if cu:
                index.setdefault(cu, date)
    return index


# ── Archived-digest parsing (shared by backfill + offline tooling) ───────────

# Markdown link on an article title line: [**Title**](url)
ARCHIVE_LINK_RE = re.compile(r"\[\*\*(.+?)\*\*\]\((https?://[^)]+)\)")
_ARCHIVE_DATE_MD = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def load_archive(data_root: Path) -> dict[str, list[dict]]:
    """Parse archived digests into {date: [{title, description, urls}]}.

    Used by the URL-index backfill and the offline comparison/replay tools.
    """
    from .seen_articles import parse_digest_markdown

    days: dict[str, list[dict]] = {}
    for f in sorted(Path(data_root).glob("????-??-??.md")):
        m = _ARCHIVE_DATE_MD.match(f.name)
        if not m:
            continue
        text = f.read_text(encoding="utf-8")
        articles = parse_digest_markdown(text)
        title_urls = {t: u for t, u in ARCHIVE_LINK_RE.findall(text)}
        for a in articles:
            url = title_urls.get(a["title"], "")
            a["urls"] = [url] if url else []
        if articles:
            days[m.group(1)] = articles
    return days


def backfill_url_index(data_root: Path, today: str,
                       lookback_days: int = DEFAULT_URL_LOOKBACK_DAYS) -> int:
    """Seed .shipped_urls.json from archived digests in the lookback window.

    Without this, a fresh checkout's URL index starts empty and cross-day
    dedup underperforms for its first lookback_days — exactly the window a
    staging/prod parallel comparison cares about. Returns the number of URLs
    now in the index.
    """
    cutoff = (datetime.strptime(today, "%Y-%m-%d")
              - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    index = load_url_index(data_root)
    for date, articles in load_archive(data_root).items():
        if cutoff <= date <= today:
            # Preserve each URL's original ship date (setdefault in
            # record_shipped keeps the earliest date written)
            record_shipped(index, articles, date)
    save_url_index(data_root, index, today, lookback_days=lookback_days)
    pruned = load_url_index(data_root)
    logger.info(f"[URL-INDEX] Backfilled {len(pruned)} shipped URLs "
                f"({cutoff}..{today})")
    return len(pruned)


# ── Lexical title similarity ─────────────────────────────────────────────────

_STOPWORDS = frozenset(
    "a all an and are as at be by for from has have in is it its new now of "
    "on or that the to was were will with your you".split()
)

_NUMERIC_TOKEN = re.compile(r"[\d.,+%]+")


def normalize_title_tokens(title: str) -> frozenset[str]:
    """Lowercased content tokens of a title (stopwords and tiny tokens dropped)."""
    tokens = re.findall(r"[a-z0-9][a-z0-9.+#-]*", (title or "").lower())
    return frozenset(t for t in tokens if len(t) >= 3 and t not in _STOPWORDS)


def _jaccard(ta: frozenset[str], tb: frozenset[str]) -> float:
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    return inter / len(ta | tb)


def title_similarity(a: str, b: str) -> float:
    """Similarity between two titles' normalized token sets.

    Max of plain Jaccard and Jaccard with numeric-only tokens dropped —
    headline variants often differ only in an added figure ("150+", "2x")
    that would otherwise dilute the overlap.
    """
    ta, tb = normalize_title_tokens(a), normalize_title_tokens(b)
    base = _jaccard(ta, tb)
    ta2 = frozenset(t for t in ta if not _NUMERIC_TOKEN.fullmatch(t))
    tb2 = frozenset(t for t in tb if not _NUMERIC_TOKEN.fullmatch(t))
    return max(base, _jaccard(ta2, tb2))


def best_title_match(title: str, history_titles: list[str]) -> tuple[float, str]:
    """Highest title similarity against a list of prior titles."""
    best, best_t = 0.0, ""
    for h in history_titles:
        sim = title_similarity(title, h)
        if sim > best:
            best, best_t = sim, h
    return best, best_t
