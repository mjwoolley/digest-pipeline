#!/usr/bin/env python3
"""
Daily Digest — Main Pipeline Orchestrator

Stages:
  1. GATHER   — concurrent fetch from sources (no LLM)
  2. EXTRACT  — batched Haiku calls (normalize to JSON)
  3. CLUSTER  — embedding API + cosine similarity
  4. DEDUPE   — 1 Sonnet call (merge clustered articles)
  5. FORMAT   — 1 Sonnet call (summarize + format)
  6. DELIVER  — email + notification + archive

Usage:
  python3 scripts/digest.py --config /path/to/config.json
  python3 scripts/digest.py --config /path/to/config.json --dry-run
"""
import json
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, render_prompt
from .log import setup_logger
from . import llm
from . import delivery
from .gather import gather_all
from .cluster import cluster_articles, embedding_text

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

MAX_SOURCE_CHARS = 50_000  # Truncate any single source to ~12K tokens


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


# ── Markdown to HTML (for email) ─────────────────────────────────────────────

def _markdown_to_email_html(md: str, config: dict) -> str:
    """Convert digest markdown to styled HTML for email delivery."""
    digest_cfg = config.get("digest", {})
    emoji = digest_cfg.get("emoji", "📰")
    section_emojis = [c["emoji"] for c in config.get("categories", [])]

    lines = md.split("\n")
    html_lines = []
    html_lines.append(
        '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', '
        'Roboto, sans-serif; max-width: 600px; margin: 0 auto; '
        'color: #333; line-height: 1.6; font-size: 15px;">'
    )
    for line in lines:
        stripped = line.strip()
        if not stripped:
            html_lines.append("<br>")
            continue
        if stripped == "---":
            html_lines.append('<hr style="border: none; border-top: 1px solid #ddd; margin: 16px 0;">')
            continue

        processed = stripped
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

        # Title line (digest emoji)
        if processed.startswith(emoji):
            html_lines.append(
                f'<h1 style="font-size: 22px; margin: 0 0 8px 0; '
                f'color: #1a1a1a;">{processed}</h1>'
            )
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

    html_lines.append("</div>")
    return "\n".join(html_lines)


# ── Main Pipeline ────────────────────────────────────────────────────────────

def main():
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

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_display = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    work_dir = Path(f"/tmp/digest-{date}")

    # 1. Setup
    logs_dir = data_root / "logs"
    logger = setup_logger(date, logs_dir)
    logger.info(f"[PIPELINE] Starting {digest_cfg.get('name', 'Digest')} for {date}" +
                (" (DRY RUN)" if dry_run else ""))
    start_time = time.time()
    tracker = TokenTracker()

    llm.configure(provider)

    try:
        # 2. Gather
        work_dir.mkdir(exist_ok=True)
        logger.info("[PIPELINE] Stage: GATHER")
        t0 = time.time()
        sources = gather_all(work_dir, sources_config=config.get("sources"))
        gather_dur = time.time() - t0

        non_empty = [s for s in sources if s.get("content", "").strip()]
        logger.info(f"[PIPELINE] Gathered {len(non_empty)}/{len(sources)} sources "
                     f"in {gather_dur:.1f}s")

        if not dry_run:
            delivery.send_progress("Gather",
                                   f"Sources: {len(non_empty)}/{len(sources)}",
                                   config, {"duration": gather_dur})

        # 3. Check: if all sources empty, exit gracefully
        if not non_empty:
            msg = "All sources empty — skipping digest"
            logger.warning(f"[PIPELINE] {msg}")
            if not dry_run:
                delivery.send_progress("Gather", msg, config)
            return

        # 4. Extract: batched Haiku calls
        logger.info("[PIPELINE] Stage: EXTRACT")
        extract_prompt = render_prompt("extract_normalize.md", config)
        batches = batch_sources(sources)
        all_articles = []

        for i, batch in enumerate(batches, 1):
            t0 = time.time()
            batch_names = [s["name"] for s in batch]
            logger.info(f"[EXTRACT] Batch {i}/{len(batches)}: {batch_names}")

            articles, usage = llm.extract_normalize(batch, date, extract_prompt)
            dur = time.time() - t0

            model_name = llm.MODELS[provider]["haiku"]
            tracker.add(f"Extract {i}/{len(batches)}", model_name, usage, dur)
            all_articles.extend(articles)

            logger.info(f"[EXTRACT] Batch {i}: {len(articles)} articles, "
                         f"{dur:.1f}s")
            if not dry_run:
                delivery.send_progress(
                    f"Extract (Batch {i}/{len(batches)})",
                    f"Articles found: {len(articles)}",
                    config, {**usage, "duration": dur}
                )

        # Save extracted articles for debugging
        (work_dir / "extracted.json").write_text(
            json.dumps(all_articles, indent=2))

        # 5. Check: if no articles extracted, exit gracefully
        if not all_articles:
            msg = "No articles extracted — skipping digest"
            logger.warning(f"[PIPELINE] {msg}")
            if not dry_run:
                delivery.send_progress("Extract", msg, config)
            return

        logger.info(f"[PIPELINE] Total articles extracted: {len(all_articles)}")

        # 6. Cluster: embed + cosine similarity
        logger.info("[PIPELINE] Stage: CLUSTER")
        t0 = time.time()
        texts = [embedding_text(a) for a in all_articles]
        embeddings, embed_usage = llm.embed(texts)
        clusters = cluster_articles(all_articles, embeddings, threshold=0.85)
        cluster_dur = time.time() - t0

        tracker.add("Cluster (embed)", llm.MODELS["openrouter"]["embedding"],
                     embed_usage, cluster_dur)

        multi = sum(1 for c in clusters if len(c) > 1)
        logger.info(f"[CLUSTER] {len(clusters)} clusters "
                     f"({multi} with duplicates), {cluster_dur:.1f}s")
        if not dry_run:
            delivery.send_progress(
                "Cluster",
                f"Clusters: {len(clusters)} ({multi} with duplicates)",
                config, {**embed_usage, "duration": cluster_dur}
            )

        # Save clusters for debugging
        (work_dir / "clusters.json").write_text(
            json.dumps([[a.get("title", "") for a in c] for c in clusters], indent=2))

        # 7. Dedupe: 1 Sonnet call
        logger.info("[PIPELINE] Stage: DEDUPE")
        t0 = time.time()
        dedupe_prompt = render_prompt("dedupe.md", config)
        deduped, dedupe_usage = llm.dedupe_merge(clusters, date, dedupe_prompt)
        dedupe_dur = time.time() - t0

        tracker.add("Dedupe", llm.MODELS[provider]["sonnet"],
                     dedupe_usage, dedupe_dur)

        logger.info(f"[DEDUPE] {len(all_articles)} -> {len(deduped)} articles, "
                     f"{dedupe_dur:.1f}s")
        if not dry_run:
            delivery.send_progress(
                "Dedupe",
                f"Articles: {len(all_articles)} -> {len(deduped)}",
                config, {**dedupe_usage, "duration": dedupe_dur}
            )

        # Save deduped for debugging
        (work_dir / "deduped.json").write_text(
            json.dumps(deduped, indent=2))

        # 8. Format: 1 Sonnet call
        logger.info("[PIPELINE] Stage: FORMAT")
        t0 = time.time()
        format_prompt = render_prompt("summarize_format.md", config,
                                      {"ARTICLES": json.dumps(deduped, indent=2),
                                       "DATE": date_display})
        formatted, format_usage = llm.summarize_format(deduped, date_display,
                                                        format_prompt)
        format_dur = time.time() - t0

        tracker.add("Format", llm.MODELS[provider]["sonnet"],
                     format_usage, format_dur)

        logger.info(f"[FORMAT] Formatted digest ({len(formatted)} chars), "
                     f"{format_dur:.1f}s")
        if not dry_run:
            delivery.send_progress(
                "Format",
                f"Digest: {len(formatted)} chars",
                config, {**format_usage, "duration": format_dur}
            )

        # Append token usage summary
        final_digest = formatted + "\n\n" + tracker.summary()

        # Save final digest
        (work_dir / "final-digest.md").write_text(final_digest)

        # 9. Deliver: send via email + archive
        if dry_run:
            logger.info("[PIPELINE] DRY RUN — skipping delivery")
            print("\n" + "=" * 60)
            print(final_digest)
            print("=" * 60)
        else:
            logger.info("[PIPELINE] Stage: DELIVER (Email)")
            emoji = digest_cfg.get("emoji", "📰")
            tagline = digest_cfg.get("name", "Digest")
            subject = f"{emoji} {tagline} — {date_display}"
            html_body = _markdown_to_email_html(final_digest, config)

            delivery.send_email(subject, html_body, config)
            logger.info("[DELIVER] Email delivery: OK")

            total_dur = time.time() - start_time
            delivery.send_notification(
                f"✅ {tagline} delivered to email\n"
                f"{tracker.summary()}\n"
                f"Duration: {total_dur:.0f}s | Chars: {len(final_digest)}",
                config
            )

        # Archive
        archive_dir = data_root / "archive"
        archive_dir.mkdir(exist_ok=True)
        (archive_dir / f"{date}.md").write_text(final_digest)
        logger.info(f"[DELIVER] Archived to {archive_dir / f'{date}.md'}")

        # 10. Report
        total_dur = time.time() - start_time
        logger.info(f"[PIPELINE] Complete in {total_dur:.1f}s | "
                     f"${tracker.total_cost:.2f} | "
                     f"{_fmt_tokens(tracker.total_input + tracker.total_output)} tokens")

    except Exception as e:
        logger.error(f"[PIPELINE] FAILED: {e}\n{traceback.format_exc()}")
        try:
            delivery.send_alert("Pipeline", str(e), config)
        except Exception:
            logger.error("[PIPELINE] Failed to send alert")
        sys.exit(1)


if __name__ == "__main__":
    main()
