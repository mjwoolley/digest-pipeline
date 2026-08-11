#!/usr/bin/env python3
"""Main pipeline orchestrator for the daily digest.

Runs every stage in order, persists intermediate artifacts to ``work/<date>/``
for the dashboard, and tracks per-stage token usage and cost via TokenTracker
+ RunLog.

Stages (functions imported from sibling modules):
    1. GATHER       — concurrent fetch from sources                  (gather.py)
    2. SKIP-SEEN    — drop items already processed in prior runs     (source_state.py)
    3. EXTRACT      — batched Haiku calls normalize raw text to JSON (llm.py)
    4. RELEVANCE    — optional keyword + LLM topic filter            (relevance.py)
    5. CLUSTER      — embeddings + cosine similarity                 (cluster.py + llm.py)
    6. DEDUPE       — Sonnet merges each cluster                     (llm.py)
    7. CROSS-DAY    — drop articles in last N days of digests        (seen_articles.py)
    8. PRIORITIZE   — Haiku scores when over digest.max_articles     (llm.py)
    9. FORMAT       — Sonnet writes the final markdown               (llm.py)
   10. DELIVER      — email + notification                          (delivery.py)

Usage:
    python3 -m digest_pipeline.digest --config /path/to/config.json [--dry-run]
"""
import html
import json
import logging
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import pipeline_date
from . import episode_title
from .config import load_config, render_prompt
from .log import setup_logger
from . import llm
from . import delivery
from .gather import gather_all
from .cluster import cluster_articles, embedding_text
from .relevance import filter_articles
from .source_state import (load_state, save_state, prune_state,
                           filter_gathered_sources, update_source_state)
from . import source_history
from .run_log import RunLog

# ── Token Tracking ───────────────────────────────────────────────────────────

@dataclass
class StageUsage:
    stage: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    duration_seconds: float = 0.0


@dataclass
class TokenTracker:
    stages: list[StageUsage] = field(default_factory=list)

    def add(self, stage: str, model: str, usage: dict, duration: float):
        self.stages.append(StageUsage(
            stage=stage,
            model=model,
            input_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            output_tokens=usage.get("completion_tokens", usage.get("output_tokens", 0)),
            cost=usage.get("cost", 0.0),
            duration_seconds=duration,
        ))

    @property
    def total_input(self) -> int:
        return sum(s.input_tokens for s in self.stages)

    @property
    def total_output(self) -> int:
        return sum(s.output_tokens for s in self.stages)

    @property
    def total_cost(self) -> float:
        return sum(s.cost for s in self.stages)

    def summary(self) -> str:
        lines = ["---", "\U0001f4ca Token Usage"]
        for s in self.stages:
            total_tok = s.input_tokens + s.output_tokens
            tok_str = _fmt_tokens(total_tok)
            lines.append(f"\u2022 {s.stage} ({s.model}): "
                         f"{tok_str} tokens \u00b7 ${s.cost:.2f} "
                         f"\u00b7 {s.duration_seconds:.1f}s")
        total_tok = self.total_input + self.total_output
        lines.append(f"\u2022 Total: {_fmt_tokens(total_tok)} tokens \u00b7 "
                     f"${self.total_cost:.2f}")
        return "\n".join(lines)


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    elif n < 10000:
        return f"{n/1000:.1f}K"
    else:
        return f"{n//1000}K"


# ── Source Batching ──────────────────────────────────────────────────────────

MAX_SOURCE_CHARS = 500_000  # Truncate any single source to ~125K tokens

logger = logging.getLogger("digest")


def batch_sources(sources: list[dict], max_chars_per_batch: int = 200_000) -> list[list[dict]]:
    """Group sources into batches for extract calls.
    Truncates oversized sources and splits by content size to stay within
    LLM context limits (~4 chars/token, 200K chars ≈ 50K tokens)."""
    non_empty = []
    for s in sources:
        content = s.get("content", "").strip()
        if not content:
            continue
        if len(content) > MAX_SOURCE_CHARS:
            logger.info(f"[EXTRACT] Truncated {s['source_label']}: {len(content):,} -> {MAX_SOURCE_CHARS:,} chars")
            s = {**s, "content": content[:MAX_SOURCE_CHARS]}
        non_empty.append(s)

    if not non_empty:
        return []

    batches = []
    current_batch = []
    current_chars = 0

    for s in non_empty:
        content_len = len(s.get("content", ""))
        if current_batch and current_chars + content_len > max_chars_per_batch:
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append(s)
        current_chars += content_len

    if current_batch:
        batches.append(current_batch)
    return batches


# ── Prioritize selection ─────────────────────────────────────────────────────

def score_articles_by_id(scored: list[dict], n: int,
                         default: int = 5) -> list[int]:
    """Map the prioritize LLM's [{"id", "score"}] output to a score per
    article index. Unknown/malformed entries fall back to the default."""
    score_map = {}
    for item in scored:
        idx = item.get("id")
        score = item.get("score", default)
        if isinstance(idx, int) and isinstance(score, (int, float)):
            score_map[idx] = int(score)
    return [score_map.get(i, default) for i in range(n)]


def select_top_articles(articles: list[dict], scores: list[int],
                        max_articles: int) -> list[int]:
    """Category-aware selection: guarantee the top-1 of each category a slot,
    then fill the remainder by score. Returns kept indices in original order.

    Index-based so duplicate or missing titles can't collapse two articles
    into one selection slot.
    """
    by_category: dict[str, list[int]] = {}
    for i, a in enumerate(articles):
        cat = a.get("category", "other")
        by_category.setdefault(cat, []).append(i)

    kept_idx: list[int] = []
    remaining_slots = max_articles

    # First pass: pick top-1 from each category
    for cat, idxs in by_category.items():
        idxs.sort(key=lambda i: scores[i], reverse=True)
        if idxs and remaining_slots > 0:
            kept_idx.append(idxs[0])
            remaining_slots -= 1

    # Second pass: fill remaining slots by score
    kept_set = set(kept_idx)
    unkept = [i for i in range(len(articles)) if i not in kept_set]
    unkept.sort(key=lambda i: scores[i], reverse=True)
    kept_idx.extend(unkept[:remaining_slots])
    kept_idx.sort()
    return kept_idx


# ── Markdown to HTML (for email) ─────────────────────────────────────────────

def _markdown_to_email_html(md: str, config: dict,
                            unsubscribe_url: str = None,
                            date_display: str = None) -> str:
    """Convert digest markdown to styled HTML for email delivery.

    Args:
        md: Markdown digest text.
        config: Pipeline config dict.
        unsubscribe_url: Optional unsubscribe link for this recipient.
        date_display: Short date string like "Wed 3/19/2026" for the header.
    """
    digest_cfg = config.get("digest", {})
    podcast_cfg = config.get("podcast", {})
    emoji = digest_cfg.get("emoji", "📰")
    section_emojis = [c["emoji"] for c in config.get("categories", [])]

    # Branding: use digest name as the canonical brand name
    brand_name = digest_cfg.get("name", podcast_cfg.get("name", "Daily Roundup"))
    brand_logo = podcast_cfg.get("image_url", "")

    lines = md.split("\n")
    html_lines = []
    html_lines.append(
        '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', '
        'Roboto, sans-serif; max-width: 600px; margin: 0 auto; '
        'color: #333; line-height: 1.6; font-size: 15px;">'
    )

    # Branded header matching the podcast
    header_parts = []
    if brand_logo:
        header_parts.append(
            f'<img src="{brand_logo}" alt="{brand_name}" '
            f'style="width: 160px; height: 160px; border-radius: 20px; '
            f'display: block; margin: 0 auto 10px;">'
        )
    title_text = brand_name
    if date_display:
        title_text += f' for {date_display}'
    header_parts.append(
        f'<span style="font-size: 20px; font-weight: 700; '
        f'color: #1a1a1a;">{title_text}</span>'
    )
    html_lines.append(
        f'<div style="text-align: center; padding: 16px 0 12px; '
        f'border-bottom: 1px solid #eee; margin-bottom: 16px;">'
        f'{"".join(header_parts)}</div>'
    )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            html_lines.append("<br>")
            continue
        if stripped == "---":
            html_lines.append('<hr style="border: none; border-top: 1px solid #ddd; margin: 16px 0;">')
            continue

        # Escape HTML-special chars before markdown conversion: an &, <, or
        # " in an LLM-written title or URL otherwise breaks tags/attributes
        # in the rendered email. Markdown syntax chars are unaffected, so
        # the link/bold/italic regexes below still match.
        processed = html.escape(stripped, quote=True)
        processed = re.sub(
            r'\[([^\]]+)\]\(([^)]+)\)',
            r'<a href="\2" style="color: #1a73e8; text-decoration: none;">\1</a>',
            processed
        )
        processed = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', processed)
        processed = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'<i>\1</i>', processed)

        # Section headers (emoji + bold text)
        if processed.startswith("<b>") and any(e in processed for e in section_emojis):
            html_lines.append(
                f'<h2 style="font-size: 17px; margin: 20px 0 8px 0; '
                f'color: #1a1a1a;">{processed}</h2>'
            )
            continue

        # Title line (digest emoji) — skip, shown in branded header
        if processed.startswith(emoji):
            continue

        if processed.startswith("•"):
            processed = processed[1:].strip()
            html_lines.append(
                f'<div style="margin: 12px 0 4px 0;">{processed}</div>'
            )
            continue

        if processed.startswith("→"):
            html_lines.append(
                f'<div style="margin: 2px 0 12px 0; font-size: 13px; '
                f'color: #666;">{processed}</div>'
            )
            continue

        html_lines.append(f"<p style=\"margin: 4px 0;\">{processed}</p>")

    # Unsubscribe footer
    if unsubscribe_url:
        html_lines.append(
            '<div style="margin-top: 24px; padding-top: 12px; '
            'border-top: 1px solid #eee; text-align: center; '
            'font-size: 12px; color: #999;">'
            f'<a href="{unsubscribe_url}" style="color: #999; '
            f'text-decoration: underline;">Unsubscribe</a> from {brand_name}'
            '</div>'
        )

    html_lines.append("</div>")
    return "\n".join(html_lines)


# ── Main Pipeline ────────────────────────────────────────────────────────────

def main():
    """Run the full digest pipeline for today's date.

    Reads the config path from ``sys.argv`` (positional or ``--config``).
    On unrecoverable failure, sends an alert via delivery.send_alert and
    exits non-zero. On "nothing to send today" exits with a skip code
    (see the exit-code note inside the function body).
    """
    # Parse args
    dry_run = "--dry-run" in sys.argv
    config_path = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config" and i < len(sys.argv) - 1:
            config_path = sys.argv[i + 1]
        elif arg.endswith(".json") and sys.argv[i - 1] != "--config":
            config_path = arg

    if not config_path:
        print("Usage: digest.py --config /path/to/config.json [--dry-run]")
        sys.exit(1)

    config = load_config(config_path)
    data_root = config["_data_root"]
    digest_cfg = config.get("digest", {})
    provider = config.get("llm", {}).get("provider", "openrouter")
    # Brand bits used by every exit path (skip notifications + delivery), so
    # define them up front — the skip blocks below run long before delivery.
    emoji = digest_cfg.get("emoji", "📰")
    tagline = digest_cfg.get("name", "Digest")

    # Publication date is anchored to the pipeline timezone (US Eastern), the
    # single source of truth shared with the podcast stage — see pipeline_date.
    _today = pipeline_date.now()
    date = _today.strftime("%Y-%m-%d")
    date_display = _today.strftime("%A, %B %d, %Y")
    work_dir = data_root / "work" / date

    # 1. Setup
    logs_dir = data_root / "logs"
    logger = setup_logger(date, logs_dir)
    logger.info(f"[PIPELINE] Starting {digest_cfg.get('name', 'Digest')} for {date}" +
                (" (DRY RUN)" if dry_run else ""))
    start_time = time.time()
    tracker = TokenTracker()

    llm.configure(provider, config.get("llm", {}).get("models"))

    # Defined before the try so the except handler can't hit an
    # UnboundLocalError (which used to mask the real failure and skip the
    # alert when RunLog construction itself failed).
    run_log = None

    try:
        # 2. Gather
        # parents=True: on a brand-new digest, work/ doesn't exist yet —
        # init.py doesn't create it, so the very first run used to crash here.
        work_dir.mkdir(parents=True, exist_ok=True)
        run_log = RunLog(digest_cfg.get("name", "Digest"), date, work_dir)
        logger.info("[PIPELINE] Stage: GATHER")
        t0 = time.time()
        sources = gather_all(work_dir, sources_config=config.get("sources"),
                             data_root=data_root)
        gather_dur = time.time() - t0

        non_empty = [s for s in sources if s.get("content", "").strip()]
        fetched_keys = {s["source_key"] for s in non_empty}
        logger.info(f"[PIPELINE] Gathered {len(non_empty)}/{len(sources)} sources "
                     f"in {gather_dur:.1f}s")
        if non_empty:
            logger.info(f"[PIPELINE] Sources: {[s['source_key'] for s in non_empty]}")

        run_log.log_stage("Gather",
                          f"Sources: {len(non_empty)}/{len(sources)}",
                          {"duration": gather_dur})

        # 2b. Incremental filter: skip previously seen items
        source_state = load_state(data_root)
        sources, pending_ids = filter_gathered_sources(sources, source_state)
        non_empty = [s for s in sources if s.get("content", "").strip()]
        if pending_ids:
            new_count = sum(len(ids) for ids in pending_ids.values())
            logger.info(f"[PIPELINE] After incremental filter: "
                        f"{len(non_empty)} sources with new content, "
                        f"{new_count} new items")

        # 3. Check: if all sources empty, exit gracefully
        if not non_empty:
            msg = "All sources empty — skipping digest"
            logger.warning(f"[PIPELINE] {msg}")
            run_log.log_stage("Gather", msg)
            total_dur = time.time() - start_time
            try:
                delivery.send_notification(
                    f"⚪ {tagline} skipped\n"
                    f"Reason: no new content after incremental filtering\n"
                    f"Sources with content before filter: {len(fetched_keys)}/{len(sources)}\n"
                    f"Duration: {total_dur:.0f}s",
                    config,
                )
            except Exception:
                logger.error("[PIPELINE] Failed to send skip notification")
            if fetched_keys:
                source_state = update_source_state(
                    source_state, {}, fetched_keys, set(), date)
                source_state = prune_state(source_state)
                save_state(data_root, source_state)
            # Exit 10/11 are skip codes, not failures — run.sh treats them as "no new
            # content today" and suppresses the failure alert it would normally send.
            run_log.skip(msg)
            sys.exit(10)

        # 4. Extract: batched Haiku calls
        logger.info("[PIPELINE] Stage: EXTRACT")
        empty_sources = [s for s in sources if not s.get("content", "").strip()]
        if empty_sources:
            empty_keys = [s["source_key"] for s in empty_sources]
            logger.info(f"[EXTRACT] Skipped {len(empty_sources)} empty sources: {empty_keys}")
        extract_prompt = render_prompt("extract_normalize.md", config)
        batches = batch_sources(sources)
        all_articles = []

        for i, batch in enumerate(batches, 1):
            t0 = time.time()
            batch_labels = [s["source_label"] for s in batch]
            logger.info(f"[EXTRACT] Batch {i}/{len(batches)}: {batch_labels}")

            articles, usage = llm.extract_normalize(batch, date, extract_prompt)
            dur = time.time() - t0

            model_name = llm.model_for("extract")
            tracker.add(f"Extract {i}/{len(batches)}", model_name, usage, dur)
            all_articles.extend(articles)

            logger.info(f"[EXTRACT] Batch {i}: {len(articles)} articles, "
                         f"{dur:.1f}s")
            # Count articles per source using source_key (stamped by code, not LLM)
            article_counts = {}
            for a in articles:
                sk = a.get("source_key", "unknown")
                article_counts[sk] = article_counts.get(sk, 0) + 1
            source_lines = "\n".join(
                f"  {s['source_label']}: {len(s.get('content', '')):,} chars -> {article_counts.get(s['source_key'], 0)} articles"
                for s in batch)
            run_log.log_stage(
                f"Extract (Batch {i}/{len(batches)})",
                f"Articles found: {len(articles)}\n{source_lines}",
                {**usage, "duration": dur}
            )

        # Save extracted articles for debugging
        (work_dir / "extracted.json").write_text(
            json.dumps(all_articles, indent=2), encoding="utf-8")

        # 5. Check: if no articles extracted, exit gracefully
        if not all_articles:
            msg = "No articles extracted — skipping digest"
            logger.warning(f"[PIPELINE] {msg}")
            run_log.log_stage("Extract", msg)
            total_dur = time.time() - start_time
            try:
                delivery.send_notification(
                    f"⚪ {tagline} skipped\n"
                    f"Reason: no articles extracted from available source content\n"
                    f"Sources with new content: {len(non_empty)}\n"
                    f"Duration: {total_dur:.0f}s",
                    config,
                )
            except Exception:
                logger.error("[PIPELINE] Failed to send skip notification")
            run_log.skip(msg)
            sys.exit(11)

        logger.info(f"[PIPELINE] Total articles extracted: {len(all_articles)}")

        # 5b. Relevance filter
        removed = []
        if config.get("relevance_filter", {}).get("enabled", False):
            logger.info("[PIPELINE] Stage: RELEVANCE FILTER")
            before = len(all_articles)
            t0 = time.time()
            all_articles, removed, rel_usage = filter_articles(all_articles, config)
            if rel_usage.get("input_tokens") or rel_usage.get("cost"):
                tracker.add("Relevance", llm.model_for("relevance"),
                            rel_usage, time.time() - t0)
            logger.info(f"[RELEVANCE] Kept {len(all_articles)}/{before} articles, removed {len(removed)}")
            if removed:
                (work_dir / "filtered.json").write_text(json.dumps(removed, indent=2), encoding="utf-8")
                for article in removed[:20]:
                    logger.info(
                        f"[RELEVANCE] Dropped: {article.get('title', '(untitled)')} "
                        f"({article.get('_filter_reason', 'no reason')})"
                    )

        # Clustering / dedup thresholds — configurable per digest via the
        # optional "clustering" config block (see clustering-thresholds.md)
        clustering_cfg = config.get("clustering", {})
        intra_threshold = clustering_cfg.get("intra_day_threshold", 0.85)
        cross_threshold = clustering_cfg.get("cross_day_threshold", 0.80)
        cross_lookback = clustering_cfg.get("cross_day_lookback_days", 5)
        title_threshold = clustering_cfg.get("title_match_threshold", 0.6)
        url_lookback = clustering_cfg.get("url_lookback_days", 14)

        # 6. Cluster: embed + cosine similarity
        logger.info("[PIPELINE] Stage: CLUSTER")
        t0 = time.time()
        texts = [embedding_text(a) for a in all_articles]
        embeddings, embed_usage = llm.embed(texts)
        clusters = cluster_articles(all_articles, embeddings,
                                    threshold=intra_threshold)
        cluster_dur = time.time() - t0

        tracker.add("Cluster (embed)", llm.MODELS["openrouter"]["embedding"],
                     embed_usage, cluster_dur)

        multi = sum(1 for c in clusters if len(c) > 1)
        logger.info(f"[CLUSTER] {len(clusters)} clusters "
                     f"({multi} with duplicates), {cluster_dur:.1f}s")
        run_log.log_stage(
            "Cluster",
            f"Clusters: {len(clusters)} ({multi} with duplicates)",
            {**embed_usage, "duration": cluster_dur}
        )

        # Save clusters for debugging
        (work_dir / "clusters.json").write_text(
            json.dumps([[a.get("title", "") for a in c] for c in clusters], indent=2), encoding="utf-8")

        # 7. Dedupe: 1 Sonnet call
        logger.info("[PIPELINE] Stage: DEDUPE")
        t0 = time.time()
        dedupe_prompt = render_prompt("dedupe.md", config)
        deduped, dedupe_usage = llm.dedupe_merge(clusters, date, dedupe_prompt)
        dedupe_dur = time.time() - t0

        tracker.add("Dedupe", llm.model_for("dedupe"),
                     dedupe_usage, dedupe_dur)

        logger.info(f"[DEDUPE] {len(all_articles)} -> {len(deduped)} articles, "
                     f"{dedupe_dur:.1f}s")
        run_log.log_stage(
            "Dedupe",
            f"Articles: {len(all_articles)} -> {len(deduped)}",
            {**dedupe_usage, "duration": dedupe_dur}
        )

        # Save deduped for debugging
        (work_dir / "deduped.json").write_text(
            json.dumps(deduped, indent=2), encoding="utf-8")

        # 7b. Cross-day dedup: layered — global URL index (free), then
        # lexical title match + embedding similarity vs recent digests,
        # then optional LLM adjudication of the grey zone.
        logger.info("[PIPELINE] Stage: CROSS-DAY DEDUP")
        t0 = time.time()
        from . import dedup_index
        from .seen_articles import (
            filter_seen, grey_zone_candidates, load_history, save_today,
        )

        pre_count = len(deduped)
        url_index = dedup_index.load_url_index(data_root)
        deduped, url_skipped = dedup_index.filter_by_url_index(deduped, url_index)

        history = load_history(data_root, date, lookback_days=cross_lookback)
        if deduped:
            texts = [embedding_text(a) for a in deduped]
            embeds, cross_usage = llm.embed(texts)
        else:
            embeds, cross_usage = [], {"input_tokens": 0, "output_tokens": 0, "cost": 0}

        skipped = []
        if history and deduped:
            deduped, skipped, embeds = filter_seen(
                deduped, embeds, history, threshold=cross_threshold,
                title_threshold=title_threshold)

        # Optional grey-zone adjudication: one cheap LLM call for pairs that
        # are suspicious (>= grey_zone_low) but under the embedding gate —
        # the band where cross-source duplicates actually live.
        if clustering_cfg.get("grey_zone_llm", False) and history and deduped:
            grey_low = clustering_cfg.get("grey_zone_low", 0.70)
            candidates = grey_zone_candidates(
                deduped, embeds, history, grey_low, cross_threshold)
            if candidates:
                same_prompt = render_prompt("same_story.md", config)
                dup_ids, grey_usage = llm.same_story_check(candidates, same_prompt)
                tracker.add("CrossDedup (LLM)", llm.model_for("same_story"),
                            grey_usage, 0)
                if dup_ids:
                    by_idx = {c["index"]: c for c in candidates}
                    kept_pairs = []
                    for i, (a, e) in enumerate(zip(deduped, embeds)):
                        if i in dup_ids:
                            a["_skip_reason"] = (
                                f"LLM same-story vs '{by_idx[i]['match_title']}'")
                            logger.info(f"[CROSS-DEDUP] LLM: skipping "
                                        f"'{a.get('title', '(untitled)')}' as same "
                                        f"story as '{by_idx[i]['match_title']}'")
                            skipped.append(a)
                        else:
                            kept_pairs.append((a, e))
                    deduped = [a for a, _ in kept_pairs]
                    embeds = [e for _, e in kept_pairs]

        cross_dur = time.time() - t0
        tracker.add("CrossDedup", "text-embedding-3-small",
                     cross_usage, cross_dur)

        cross_skipped = url_skipped + skipped
        cross_skipped_count = len(cross_skipped)
        (work_dir / "cross_deduped.json").write_text(
            json.dumps(deduped, indent=2), encoding="utf-8")
        if cross_skipped:
            (work_dir / "cross_skipped.json").write_text(
                json.dumps(cross_skipped, indent=2), encoding="utf-8")

        if history or url_index:
            logger.info(f"[CROSS-DEDUP] Skipped {cross_skipped_count} previously "
                        f"seen articles ({pre_count} -> {len(deduped)}; "
                        f"{len(url_skipped)} by URL)")
            run_log.log_stage(
                "CrossDedup",
                f"Skipped {cross_skipped_count} repeats "
                f"({pre_count} -> {len(deduped)}; {len(url_skipped)} by URL)",
                {**cross_usage, "duration": cross_dur}
            )
        else:
            logger.info("[CROSS-DEDUP] No history found — nothing to compare against")
            run_log.log_stage(
                "CrossDedup",
                f"No history -- {len(deduped)} articles pass through",
                {**cross_usage, "duration": cross_dur}
            )

        # If everything today was already covered, skip instead of formatting
        # and shipping an empty digest. Seen-IDs are still committed so the
        # same items aren't refetched and re-extracted tomorrow.
        if not deduped:
            msg = "All articles were cross-day duplicates — skipping digest"
            logger.warning(f"[PIPELINE] {msg}")
            run_log.log_stage("CrossDedup", msg)
            total_dur = time.time() - start_time
            try:
                delivery.send_notification(
                    f"⚪ {tagline} skipped\n"
                    f"Reason: all {pre_count} extracted articles were "
                    f"cross-day duplicates\n"
                    f"Duration: {total_dur:.0f}s",
                    config,
                )
            except Exception:
                logger.error("[PIPELINE] Failed to send skip notification")
            if pending_ids or fetched_keys:
                source_state = update_source_state(
                    source_state, pending_ids, fetched_keys, set(), date)
                source_state = prune_state(source_state)
                save_state(data_root, source_state)
            run_log.skip(msg)
            sys.exit(10)

        # 8. Prioritize: trim to max_articles if needed
        max_articles = digest_cfg.get("max_articles")
        if max_articles and len(deduped) > max_articles:
            logger.info(f"[PIPELINE] Stage: PRIORITIZE ({len(deduped)} articles > {max_articles} max)")
            t0 = time.time()
            # The model is given (and echoes back) a positional id per article.
            # Matching by echoed id instead of title string means a re-cased or
            # normalized title can't silently fall back to the default score.
            prio_payload = [
                {"id": i,
                 "title": a.get("title", ""),
                 "description": a.get("description", ""),
                 "category": a.get("category", "")}
                for i, a in enumerate(deduped)
            ]
            prioritize_prompt = render_prompt("prioritize.md", config,
                                              {"ARTICLES": json.dumps(prio_payload, indent=2),
                                               "MAX_ARTICLES": str(max_articles)})
            scored, prio_usage = llm.prioritize_score(deduped, prioritize_prompt)
            prio_dur = time.time() - t0

            model_name = llm.model_for("prioritize")
            tracker.add("Prioritize", model_name, prio_usage, prio_dur)

            scores = score_articles_by_id(scored, len(deduped))
            for i, a in enumerate(deduped):
                a["_priority_score"] = scores[i]

            kept_idx = select_top_articles(deduped, scores, max_articles)
            kept_set = set(kept_idx)
            kept = [deduped[i] for i in kept_idx]
            dropped = [deduped[i] for i in range(len(deduped)) if i not in kept_set]

            # Save debug files
            (work_dir / "prioritized.json").write_text(
                json.dumps(kept, indent=2), encoding="utf-8")
            (work_dir / "prioritized_dropped.json").write_text(
                json.dumps(dropped, indent=2), encoding="utf-8")

            # Clean up internal score field
            for a in kept:
                a.pop("_priority_score", None)
            for a in dropped:
                a.pop("_priority_score", None)

            logger.info(f"[PRIORITIZE] {len(deduped)} -> {len(kept)} articles "
                        f"({len(dropped)} dropped), {prio_dur:.1f}s")
            run_log.log_stage(
                "Prioritize",
                f"Articles: {len(deduped)} -> {len(kept)} (dropped {len(dropped)})",
                {**prio_usage, "duration": prio_dur}
            )

            deduped = kept
            embeds = [embeds[i] for i in kept_idx]
        else:
            if max_articles:
                logger.info(f"[PIPELINE] Skipping PRIORITIZE ({len(deduped)} <= {max_articles} max)")
            else:
                logger.info("[PIPELINE] Skipping PRIORITIZE (no max_articles configured)")

        included_keys = {sk for art in deduped for sk in art.get("source_keys", [])}

        # 9. Format: 1 Sonnet call
        logger.info("[PIPELINE] Stage: FORMAT")
        t0 = time.time()
        format_prompt = render_prompt("summarize_format.md", config,
                                      {"ARTICLES": json.dumps(deduped, indent=2),
                                       "DATE": date_display})
        formatted, format_usage = llm.summarize_format(deduped, date_display,
                                                        format_prompt)
        format_dur = time.time() - t0

        tracker.add("Format", llm.model_for("format"),
                     format_usage, format_dur)

        logger.info(f"[FORMAT] Formatted digest ({len(formatted)} chars), "
                     f"{format_dur:.1f}s")
        run_log.log_stage(
            "Format",
            f"Digest: {len(formatted)} chars",
            {**format_usage, "duration": format_dur}
        )

        # Append token usage summary
        final_digest = formatted + "\n\n" + tracker.summary()

        # Save final digest
        (work_dir / "final-digest.md").write_text(final_digest, encoding="utf-8")

        # 9. Deliver: send via email
        if dry_run:
            logger.info("[PIPELINE] DRY RUN — skipping delivery")
            print("\n" + "=" * 60)
            print(final_digest)
            print("=" * 60)
        else:
            logger.info("[PIPELINE] Stage: DELIVER (Email)")
            # Generate the one-line episode title once here and persist it to
            # <date>.title so the podcast stage reuses the exact same headline.
            # The email subject is the bare title; fall back to "brand — date"
            # if title generation fails.
            episode_headline = episode_title.generate(final_digest, config, logger,
                                                      tracker=tracker)
            if episode_headline:
                podcasts_dir = data_root / "podcasts"
                podcasts_dir.mkdir(parents=True, exist_ok=True)
                (podcasts_dir / f"{date}.title").write_text(episode_headline + "\n", encoding="utf-8")
                subject = episode_headline
                logger.info(f"[TITLE] Episode title: {episode_headline}")
            else:
                subject = f"{emoji} {tagline} — {date_display}"

            # Send to subscribers with personalized unsubscribe links
            from .subscribers import unsubscribe_url as _unsub_url
            _pub_base = config.get("subscriptions", {}).get("public_base_url", "")
            _digest_slug = config["_data_root"].name
            def _make_html(email, token):
                return _markdown_to_email_html(
                    formatted, config,
                    unsubscribe_url=_unsub_url(_pub_base, token, digest=_digest_slug),
                    date_display=date_display,
                )
            def _unsub_for(email, token):
                return _unsub_url(_pub_base, token, digest=_digest_slug)
            delivery.send_email_to_subscribers(
                subject, _make_html, config, digest_date=date,
                unsubscribe_url_fn=_unsub_for)

            total_dur = time.time() - start_time
            delivery.send_notification(
                f"✅ {tagline} delivered to email\n"
                f"Articles: {len(deduped)} | Cross-day dupes skipped: {cross_skipped_count}\n"
                f"{tracker.summary()}\n"
                f"Duration: {total_dur:.0f}s | Chars: {len(final_digest)}",
                config
            )

        # Archive digest to data_root (e.g. digests/ai/2026-03-09.md)
        digest_path = data_root / f"{date}.md"
        digest_path.write_text(final_digest, encoding="utf-8")
        logger.info(f"[DELIVER] Archived to {digest_path}")

        # 10a. Record shipped content for cross-day dedup. This runs only
        # after successful delivery/archive: a run that dies in FORMAT or
        # DELIVER must not burn its articles as "seen" (the retry would
        # silently drop them all), and prioritize-dropped articles are not
        # recorded because the reader never saw them.
        save_today(data_root, date, deduped, embeds,
                   lookback_days=cross_lookback)
        url_index = dedup_index.record_shipped(url_index, deduped, date)
        dedup_index.save_url_index(data_root, url_index, date,
                                   lookback_days=url_lookback)

        # 10. Commit source state (only after successful processing)
        if pending_ids or fetched_keys or included_keys:
            source_state = update_source_state(
                source_state, pending_ids, fetched_keys, included_keys, date)
            source_state = prune_state(source_state)
            save_state(data_root, source_state)
            new_id_count = sum(len(v) for v in pending_ids.values())
            logger.info(
                f"[SOURCE-STATE] Committed {new_id_count} item IDs across "
                f"{len(pending_ids)} sources; "
                f"fetched={len(fetched_keys)} included={len(included_keys)}"
            )

        # 10b. Append per-source counts to the history ledger
        try:
            source_history.append_daily(data_root, work_dir, date)
        except Exception as e:
            logger.warning(f"[SOURCE-HISTORY] append_daily failed: {e}")

        # 11. Report. complete() runs after delivery so the title and
        # relevance calls' usage lands in run.json totals; a failure anywhere
        # above still ends in run_log.fail() via the except handler.
        run_log.complete(tracker)
        total_dur = time.time() - start_time
        logger.info(f"[PIPELINE] Complete in {total_dur:.1f}s | "
                     f"${tracker.total_cost:.2f} | "
                     f"{_fmt_tokens(tracker.total_input + tracker.total_output)} tokens")

    except Exception as e:
        logger.error(f"[PIPELINE] FAILED: {e}\n{traceback.format_exc()}")
        if run_log is not None:
            run_log.fail(str(e))
        try:
            delivery.send_alert("Pipeline", str(e), config)
        except Exception:
            logger.error("[PIPELINE] Failed to send alert")
        sys.exit(1)


if __name__ == "__main__":
    main()
