#!/usr/bin/env python3
"""Semantic (LLM-driven) prod-vs-staging comparison.

compare_digests.py matches stories lexically: canonical URL identity, else a
title-token Jaccard >= 0.6. Both signals are weak here, because each side's
FORMAT stage rewrites headlines with Opus and the two sides often cite
different primary sources for the same story. The result is false negatives in
both directions -- entries that are the same story read as "prod-only" plus
"staging-only", and a story re-shipped days later reads as new.

This tool answers the three questions lexical matching cannot:

  1. cross_side   -- which prod and staging entries are the same underlying
                     story (the honest "shared" count)
  2. within_day   -- which entries inside ONE day's digest duplicate each other
                     (compare_digests.py does not measure this at all)
  3. cross_day    -- which of today's entries re-ship a story that side already
                     ran in the prior N days (the real repeat-rate metric)

Usage:
    python3 scripts/deep_compare.py /prod-digests/ai /app/digests/ai \
        [--date YYYY-MM-DD] [--lookback N] [--model opus|sonnet|haiku] \
        [--notify CONFIG] [--out-dir DIR] [--json]

Read-only: it never writes into either digest's state, only the report.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest_pipeline import llm  # noqa: E402
from digest_pipeline.dedup_index import load_archive  # noqa: E402
from digest_pipeline.util import extract_json_object  # noqa: E402

DEFAULT_LOOKBACK = 5


# ── Prompt payloads ──────────────────────────────────────────────────────────

def _entries(articles: list[dict], limit_desc: int = 320) -> list[dict]:
    """Compact article view for the prompts: index, title, summary, source."""
    out = []
    for i, a in enumerate(articles):
        out.append({
            "id": i,
            "title": a.get("title", ""),
            "summary": (a.get("description", "") or "")[:limit_desc],
            "url": (a.get("urls") or [""])[0],
        })
    return out


CROSS_SIDE_PROMPT = """\
Below are two lists of entries from two versions of the same daily news digest,
built by two different pipelines from overlapping sources on the same day.

Pair up entries that report the SAME UNDERLYING STORY (same event, launch,
paper, or announcement), even when the headlines are worded differently or the
two sides cite different source URLs. Two entries about different aspects of
one launch are the same story. Two entries about genuinely different events are
not, even if they share a product name.

PROD entries:
{prod}

STAGING entries:
{staging}

Return ONLY JSON:
{{
  "pairs": [{{"prod_id": <int>, "staging_id": <int>, "story": "<short label>",
              "confidence": "high"|"medium"|"low"}}],
  "prod_only": [<int>, ...],
  "staging_only": [<int>, ...]
}}
"""

WITHIN_DAY_PROMPT = """\
Below are the entries from a single day of one news digest. A reader should not
see the same story twice in one issue.

Identify any GROUPS of entries that cover the same underlying story. Different
angles on one launch (the release itself, its rollout, a library adding support
for it) count as the same story ONLY if a reader would experience them as
repetitive. Genuinely distinct events that merely share a product name do not.

Entries:
{entries}

Return ONLY JSON:
{{
  "groups": [{{"ids": [<int>, ...], "story": "<short label>",
               "verdict": "redundant"|"complementary",
               "why": "<one sentence>"}}]
}}
Return an empty groups list if every entry is distinct.
"""

CROSS_DAY_PROMPT = """\
TODAY's digest entries for {date}:
{today}

Stories this SAME digest already shipped in the previous {lookback} days:
{prior}

Which of today's entries re-ship a story that already ran? A follow-up with
genuinely new developments is NOT a repeat; a restatement of the same news with
a fresh headline IS. Judge by the underlying news, not the wording.

Return ONLY JSON:
{{
  "repeats": [{{"today_id": <int>, "prior_date": "YYYY-MM-DD",
                "prior_title": "<headline it repeats>",
                "kind": "restatement"|"follow-up",
                "why": "<one sentence>"}}]
}}
Only include entries whose kind is "restatement". Return an empty list if none.
"""


# ── LLM calls ────────────────────────────────────────────────────────────────

def _ask(prompt: str, model: str, usage_acc: list, max_tokens: int = 3000) -> dict:
    text, usage = llm.chat([{"role": "user", "content": prompt}], model,
                           max_tokens=max_tokens)
    usage_acc.append(usage)
    return extract_json_object(text) or {}


def cross_side(prod: list[dict], staging: list[dict], model: str,
               usage: list) -> dict:
    if not prod or not staging:
        return {"pairs": [], "prod_only": list(range(len(prod))),
                "staging_only": list(range(len(staging)))}
    return _ask(CROSS_SIDE_PROMPT.format(
        prod=json.dumps(_entries(prod), indent=1),
        staging=json.dumps(_entries(staging), indent=1)), model, usage)


def within_day(articles: list[dict], model: str, usage: list) -> dict:
    if len(articles) < 2:
        return {"groups": []}
    return _ask(WITHIN_DAY_PROMPT.format(
        entries=json.dumps(_entries(articles), indent=1)), model, usage)


def cross_day(days: dict, date: str, lookback: int, model: str,
              usage: list) -> dict:
    from datetime import datetime, timedelta
    today = days.get(date, [])
    if not today:
        return {"repeats": []}
    cutoff = (datetime.strptime(date, "%Y-%m-%d")
              - timedelta(days=lookback)).strftime("%Y-%m-%d")
    prior = []
    for d in sorted(days):
        if cutoff <= d < date:
            for a in days[d]:
                prior.append({"date": d, "title": a.get("title", ""),
                              "summary": (a.get("description", "") or "")[:200]})
    if not prior:
        return {"repeats": [], "_no_history": True}
    return _ask(CROSS_DAY_PROMPT.format(
        date=date, lookback=lookback,
        today=json.dumps(_entries(today), indent=1),
        prior=json.dumps(prior, indent=1)), model, usage, max_tokens=3000)


# ── Report ───────────────────────────────────────────────────────────────────

def build_report(date: str, prod: list, staging: list, xs: dict,
                 wd_prod: dict, wd_stg: dict, cd_prod: dict, cd_stg: dict,
                 lexical: dict, usage: list) -> tuple[str, dict]:
    pairs = xs.get("pairs", [])
    high = [p for p in pairs if p.get("confidence") != "low"]
    L = [f"# Deep comparison — {date}", "",
         f"Prod: {len(prod)} articles · Staging: {len(staging)} articles", ""]

    L += ["## Story overlap (semantic vs lexical)", "",
          f"- Semantic shared: **{len(pairs)}** ({len(high)} at medium+ confidence)",
          f"- Lexical shared (compare_digests.py): **{lexical.get('shared', '?')}**"]
    missed = len(pairs) - (lexical.get("shared") or 0)
    if missed > 0:
        L.append(f"- **Lexical matching missed {missed} pair(s)** — same story, "
                 "different headline and source URL")
    L.append("")
    if pairs:
        L += ["<details><summary>Matched stories</summary>", ""]
        for p in pairs:
            pi, si = p.get("prod_id"), p.get("staging_id")
            if not isinstance(pi, int) or not isinstance(si, int):
                continue
            if pi >= len(prod) or si >= len(staging):
                continue
            L.append(f"- **{p.get('story', '?')}** ({p.get('confidence', '?')})")
            L.append(f"  - prod: {prod[pi].get('title', '?')}")
            L.append(f"  - staging: {staging[si].get('title', '?')}")
        L += ["", "</details>", ""]

    L += ["## Within-day duplication (same issue ships one story twice)", ""]
    for side, arts, wd in (("Prod", prod, wd_prod), ("Staging", staging, wd_stg)):
        red = [g for g in wd.get("groups", []) if g.get("verdict") == "redundant"]
        L.append(f"- **{side}: {len(red)} redundant group(s)**")
        for g in wd.get("groups", []):
            ids = [i for i in g.get("ids", []) if isinstance(i, int) and i < len(arts)]
            mark = "🔴" if g.get("verdict") == "redundant" else "🟡"
            L.append(f"  - {mark} {g.get('story', '?')} — {g.get('why', '')}")
            for i in ids:
                L.append(f"     - {arts[i].get('title', '?')}")
    L.append("")

    L += [f"## Cross-day repeats (re-shipped within lookback)", ""]
    for side, arts, cd in (("Prod", prod, cd_prod), ("Staging", staging, cd_stg)):
        if cd.get("_no_history"):
            L.append(f"- **{side}: no prior archive in window** — metric not applicable")
            continue
        reps = cd.get("repeats", [])
        L.append(f"- **{side}: {len(reps)}**")
        for r in reps:
            i = r.get("today_id")
            t = arts[i].get("title", "?") if isinstance(i, int) and i < len(arts) else "?"
            L.append(f"  - {t}")
            L.append(f"    ↳ repeats {r.get('prior_date', '?')}: "
                     f"'{r.get('prior_title', '?')}' — {r.get('why', '')}")
    L.append("")

    cost = sum(u.get("cost", 0.0) for u in usage)
    # llm.chat returns OpenRouter-style keys on some paths and Anthropic-style
    # on others; accept both, as the podcast stage does.
    tok = sum(u.get("input_tokens", u.get("prompt_tokens", 0))
              + u.get("output_tokens", u.get("completion_tokens", 0))
              for u in usage)
    L += ["## Analysis cost", "", f"- {len(usage)} LLM calls · {tok:,} tokens · ${cost:.3f}", ""]

    summary = {
        "date": date,
        "prod_articles": len(prod), "staging_articles": len(staging),
        "semantic_shared": len(pairs), "lexical_shared": lexical.get("shared"),
        "prod_within_day": len([g for g in wd_prod.get("groups", [])
                                if g.get("verdict") == "redundant"]),
        "staging_within_day": len([g for g in wd_stg.get("groups", [])
                                   if g.get("verdict") == "redundant"]),
        "prod_cross_day": len(cd_prod.get("repeats", [])),
        "staging_cross_day": len(cd_stg.get("repeats", [])),
        "cost": cost,
    }
    return "\n".join(L), summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prod_root", type=Path)
    ap.add_argument("staging_root", type=Path)
    ap.add_argument("--date", help="Date to analyse (default: newest common)")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    ap.add_argument("--model", default="opus", help="opus|sonnet|haiku or full id")
    ap.add_argument("--notify", metavar="CONFIG", help="Telegram summary via this config")
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--json", action="store_true", help="Print summary JSON only")
    args = ap.parse_args()

    cfg_path = args.notify or str(Path(args.staging_root) / "config.json")
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    llm.configure(cfg.get("llm", {}).get("provider", "openrouter"),
                  cfg.get("llm", {}).get("models"))
    model = llm.MODELS[cfg.get("llm", {}).get("provider", "openrouter")].get(
        args.model, args.model)

    prod_days = load_archive(args.prod_root)
    stg_days = load_archive(args.staging_root)
    common = sorted(set(prod_days) & set(stg_days))
    if not common:
        print("No common dates between the two roots.", file=sys.stderr)
        return 1
    date = args.date or common[-1]
    if date not in prod_days or date not in stg_days:
        print(f"{date} missing on one side (common: {common[-3:]})", file=sys.stderr)
        return 1

    prod, staging = prod_days[date], stg_days[date]

    # Lexical baseline, for the side-by-side contrast.
    from compare_digests import match_articles
    lex = match_articles(prod, staging)
    lexical = {"shared": len(lex["shared"])}

    usage = []
    xs = cross_side(prod, staging, model, usage)
    wd_prod = within_day(prod, model, usage)
    wd_stg = within_day(staging, model, usage)
    cd_prod = cross_day(prod_days, date, args.lookback, model, usage)
    cd_stg = cross_day(stg_days, date, args.lookback, model, usage)

    report, summary = build_report(date, prod, staging, xs, wd_prod, wd_stg,
                                   cd_prod, cd_stg, lexical, usage)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(report)

    out_dir = args.out_dir or args.staging_root
    out = Path(out_dir) / f"deep-compare-{date}.md"
    out.write_text(report, encoding="utf-8")
    print(f"\n[written to {out}]")

    if args.notify:
        from digest_pipeline import delivery
        delivery.send_notification(
            f"🔬 Deep compare — {date}\n"
            f"Semantic shared: {summary['semantic_shared']} "
            f"(lexical said {summary['lexical_shared']})\n"
            f"Within-day dupes: prod {summary['prod_within_day']} / "
            f"staging {summary['staging_within_day']}\n"
            f"Cross-day repeats: prod {summary['prod_cross_day']} / "
            f"staging {summary['staging_cross_day']}\n"
            f"Analysis cost: ${summary['cost']:.2f}", cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
