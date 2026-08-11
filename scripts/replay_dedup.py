#!/usr/bin/env python3
"""Offline replay of the lexical/URL cross-day dedup layers over archived digests.

Parses every YYYY-MM-DD.md in a digest's data root, walks the days in order,
and reports which articles the new gates (canonical-URL index + normalized
title similarity) would have suppressed — no API calls, no embeddings.

Usage:
    python3 -m scripts.replay_dedup digests/ai [--lookback 5] [--title-threshold 0.6] [-v]
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest_pipeline.dedup_index import (  # noqa: E402
    best_title_match, canonicalize_url,
)
from digest_pipeline.seen_articles import parse_digest_markdown  # noqa: E402

_LINK_RE = re.compile(r"\[\*\*(.+?)\*\*\]\((https?://[^)]+)\)")
_DATE_MD = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def load_archive(data_root: Path) -> dict[str, list[dict]]:
    """{date: [{title, description, urls}]} from archived digest markdown."""
    days = {}
    for f in sorted(data_root.glob("????-??-??.md")):
        m = _DATE_MD.match(f.name)
        if not m:
            continue
        text = f.read_text(encoding="utf-8")
        articles = parse_digest_markdown(text)
        title_urls = {t: u for t, u in _LINK_RE.findall(text)}
        for a in articles:
            url = title_urls.get(a["title"], "")
            a["urls"] = [url] if url else []
        if articles:
            days[m.group(1)] = articles
    return days


def replay(days: dict[str, list[dict]], lookback: int = 5,
           title_threshold: float = 0.6, verbose: bool = False) -> dict:
    dates = sorted(days)
    total = caught_url = caught_title = 0
    catches = []
    for i, date in enumerate(dates):
        window = [d for d in dates[:i] if d >= _shift(date, -lookback)]
        hist_titles = [a["title"] for d in window for a in days[d]]
        hist_urls = {canonicalize_url(u): d
                     for d in window for a in days[d] for u in a["urls"]
                     if canonicalize_url(u)}
        for a in days[date]:
            total += 1
            cus = [canonicalize_url(u) for u in a["urls"]]
            url_hit = next((cu for cu in cus if cu and cu in hist_urls), None)
            sim, match = best_title_match(a["title"], hist_titles)
            if url_hit:
                caught_url += 1
                catches.append((date, a["title"], f"url (shipped {hist_urls[url_hit]})"))
            elif sim >= title_threshold:
                caught_title += 1
                catches.append((date, a["title"], f"title {sim:.2f} vs '{match}'"))
    if verbose:
        for date, title, why in catches:
            print(f"  {date}  {title}\n          -> {why}")
    return {"days": len(dates), "articles": total,
            "caught_url": caught_url, "caught_title": caught_title,
            "catches": catches}


def _shift(date: str, days: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.strptime(date, "%Y-%m-%d")
            + timedelta(days=days)).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("data_root", type=Path)
    ap.add_argument("--lookback", type=int, default=5)
    ap.add_argument("--title-threshold", type=float, default=0.6)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    days = load_archive(args.data_root)
    result = replay(days, args.lookback, args.title_threshold, args.verbose)
    print(f"\nReplayed {result['days']} archived digests, "
          f"{result['articles']} articles.")
    print(f"Would have suppressed: {result['caught_url']} by URL index, "
          f"{result['caught_title']} by title similarity "
          f"(threshold {args.title_threshold}, lookback {args.lookback}d).")


if __name__ == "__main__":
    main()
