#!/usr/bin/env python3
"""Compare prod and staging digest outputs for the same date(s).

Built for running the redesign branch in staging as a prod-parallel for a few
days: same date, two data roots, one report. Matches articles across the two
sides (canonical URL first, then title similarity), measures each side's
repeat-story rate against its own recent archives, and lays the run costs
side by side. Optionally has an LLM judge the two digests blind.

Usage:
    python3 scripts/compare_digests.py ~/digest-pipeline/digests/ai digests/ai \
        [--date YYYY-MM-DD | --days N] [--llm-judge] [--notify CONFIG] [--out-dir DIR]
"""
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest_pipeline.dedup_index import (  # noqa: E402
    best_title_match, canonicalize_url, load_archive, title_similarity,
)

TITLE_MATCH = 0.6
REPEAT_LOOKBACK_DAYS = 5


# ── Data loading ─────────────────────────────────────────────────────────────

def _urls_of(article: dict) -> set[str]:
    return {canonicalize_url(u) for u in article.get("urls", []) if u} - {""}


def load_run_totals(data_root: Path, date: str) -> dict | None:
    """Stage/cost summary from work/<date>/run.json, or None if absent."""
    path = Path(data_root) / "work" / date / "run.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    stages = []
    for s in data.get("stages", []):
        tok = s.get("tokens") or {}
        if tok:
            stages.append({
                "stage": s.get("stage", "?"),
                "input": tok.get("input", 0),
                "output": tok.get("output", 0),
                "cost": tok.get("cost", 0.0),
            })
    return {
        "status": data.get("status"),
        "duration_s": data.get("duration_s"),
        "totals": data.get("totals") or {},
        "stages": stages,
    }


def load_cross_skipped(data_root: Path, date: str) -> dict[str, str]:
    """{title: skip_reason} from work/<date>/cross_skipped.json (new branch only)."""
    path = Path(data_root) / "work" / date / "cross_skipped.json"
    if not path.exists():
        return {}
    try:
        arts = json.loads(path.read_text(encoding="utf-8"))
        return {a.get("title", ""): a.get("_skip_reason", "cross-day duplicate")
                for a in arts}
    except (json.JSONDecodeError, OSError):
        return {}


# ── Matching ─────────────────────────────────────────────────────────────────

def match_articles(prod: list[dict], staging: list[dict]) -> dict:
    """Pair articles across sides: canonical URL first, then title similarity.

    Returns {"shared": [(p, s)], "prod_only": [...], "staging_only": [...]}.
    """
    unmatched_staging = list(range(len(staging)))
    shared, prod_only = [], []

    for p in prod:
        p_urls = _urls_of(p)
        hit = None
        # Pass 1: URL identity
        for j in unmatched_staging:
            if p_urls & _urls_of(staging[j]):
                hit = j
                break
        # Pass 2: title similarity
        if hit is None:
            best, best_j = 0.0, None
            for j in unmatched_staging:
                sim = title_similarity(p.get("title", ""), staging[j].get("title", ""))
                if sim > best:
                    best, best_j = sim, j
            if best >= TITLE_MATCH:
                hit = best_j
        if hit is None:
            prod_only.append(p)
        else:
            shared.append((p, staging[hit]))
            unmatched_staging.remove(hit)

    staging_only = [staging[j] for j in unmatched_staging]
    return {"shared": shared, "prod_only": prod_only, "staging_only": staging_only}


def count_repeats(days: dict[str, list[dict]], date: str,
                  lookback: int = REPEAT_LOOKBACK_DAYS) -> list[dict]:
    """Articles on `date` that already appeared in that side's own prior
    `lookback` days (URL or title match) — the shipped-duplicate metric."""
    from datetime import datetime, timedelta
    cutoff = (datetime.strptime(date, "%Y-%m-%d")
              - timedelta(days=lookback)).strftime("%Y-%m-%d")
    prior = [a for d, arts in days.items() if cutoff <= d < date for a in arts]
    prior_urls = {u for a in prior for u in _urls_of(a)}
    prior_titles = [a.get("title", "") for a in prior]

    repeats = []
    for a in days.get(date, []):
        if _urls_of(a) & prior_urls:
            repeats.append({**a, "_repeat_via": "url"})
            continue
        sim, match = best_title_match(a.get("title", ""), prior_titles)
        if sim >= TITLE_MATCH:
            repeats.append({**a, "_repeat_via": f"title {sim:.2f} vs '{match}'"})
    return repeats


# ── LLM judge ────────────────────────────────────────────────────────────────

JUDGE_PROMPT = """\
You are judging two versions of the same daily news digest, produced by two
different pipelines from the same source material on the same day. You do not
know which pipeline is which — they are labelled A and B.

Digest A:
---
{digest_a}
---

Digest B:
---
{digest_b}
---

Compare them on these dimensions:
1. summary_quality — are the 2-3 sentence summaries accurate-sounding, dense, and readable?
2. why_it_matters — are the "why it matters" lines insightful or generic?
3. clarity_and_tone — headline quality, flow, consistency of tone.
4. redundancy — does either digest repeat the same story in multiple entries?

Return ONLY a JSON object:
{{
  "summary_quality": {{"winner": "A"|"B"|"tie", "note": "<one sentence with a quoted example>"}},
  "why_it_matters": {{"winner": "A"|"B"|"tie", "note": "..."}},
  "clarity_and_tone": {{"winner": "A"|"B"|"tie", "note": "..."}},
  "redundancy": {{"winner": "A"|"B"|"tie", "note": "..."}},
  "overall": {{"winner": "A"|"B"|"tie", "note": "<one sentence>"}}
}}
"""


def llm_judge(prod_md: str, staging_md: str, config: dict) -> dict:
    """Blind A/B judgment. Returns {dimension: {winner: prod|staging|tie, note}}."""
    from digest_pipeline import llm
    from digest_pipeline.util import extract_json_object

    llm.configure(config.get("llm", {}).get("provider", "openrouter"),
                  config.get("llm", {}).get("models"))
    swap = random.random() < 0.5
    a, b = (staging_md, prod_md) if swap else (prod_md, staging_md)
    prompt = JUDGE_PROMPT.format(digest_a=a, digest_b=b)
    text, usage = llm.chat([{"role": "user", "content": prompt}],
                           llm.model_for("format"), max_tokens=1500)
    verdict = extract_json_object(text)

    def unblind(label):
        if label not in ("A", "B"):
            return "tie"
        if swap:
            return "staging" if label == "A" else "prod"
        return "prod" if label == "A" else "staging"

    out = {}
    for dim, v in verdict.items():
        if isinstance(v, dict):
            out[dim] = {"winner": unblind(v.get("winner", "tie")),
                        "note": v.get("note", "")}
    out["_usage"] = usage
    return out


# ── Report ───────────────────────────────────────────────────────────────────

def _fmt_side_costs(run: dict | None) -> list[str]:
    if not run:
        return ["  (no run.json)"]
    lines = []
    for s in run["stages"]:
        lines.append(f"  {s['stage']}: {s['input'] + s['output']:,} tok · "
                     f"${s['cost']:.3f}")
    totals = run["totals"]
    if totals:
        lines.append(f"  **Total: {totals.get('input_tokens', 0) + totals.get('output_tokens', 0):,} tok · "
                     f"${totals.get('cost', 0):.3f} · {run.get('duration_s', '?')}s "
                     f"({run.get('status')})**")
    return lines


def compare_date(prod_root: Path, staging_root: Path, date: str,
                 prod_days: dict, staging_days: dict,
                 judge_config: dict = None) -> tuple[str, dict]:
    """Build the per-date markdown report. Returns (markdown, summary_dict)."""
    prod_arts = prod_days.get(date, [])
    staging_arts = staging_days.get(date, [])

    m = match_articles(prod_arts, staging_arts)
    prod_repeats = count_repeats(prod_days, date)
    staging_repeats = count_repeats(staging_days, date)
    skipped_reasons = load_cross_skipped(staging_root, date)
    prod_run = load_run_totals(prod_root, date)
    staging_run = load_run_totals(staging_root, date)

    lines = [f"# Digest comparison — {date}", ""]
    lines += [f"Prod: {len(prod_arts)} articles · Staging: {len(staging_arts)} articles · "
              f"Shared: {len(m['shared'])}", ""]

    lines += ["## Repeat stories shipped (already in that side's last "
              f"{REPEAT_LOOKBACK_DAYS} days)", "",
              f"- **Prod: {len(prod_repeats)}**"]
    for a in prod_repeats:
        lines.append(f"  - {a.get('title', '?')} ({a['_repeat_via']})")
    lines.append(f"- **Staging: {len(staging_repeats)}**")
    for a in staging_repeats:
        lines.append(f"  - {a.get('title', '?')} ({a['_repeat_via']})")
    lines.append("")

    if m["prod_only"]:
        lines += ["## Prod-only articles", ""]
        for a in m["prod_only"]:
            title = a.get("title", "?")
            why = ""
            # Was it deliberately suppressed in staging?
            for sk_title, reason in skipped_reasons.items():
                if title_similarity(title, sk_title) >= TITLE_MATCH:
                    why = f" — staging suppressed it: {reason}"
                    break
            lines.append(f"- {title}{why}")
        lines.append("")
    if m["staging_only"]:
        lines += ["## Staging-only articles", ""]
        for a in m["staging_only"]:
            lines.append(f"- {a.get('title', '?')}")
        lines.append("")

    lines += ["## Cost & stages", "", "**Prod**"]
    lines += _fmt_side_costs(prod_run)
    lines += ["", "**Staging**"]
    lines += _fmt_side_costs(staging_run)
    lines.append("")

    def _avg_desc(arts):
        descs = [len(a.get("description", "")) for a in arts]
        return sum(descs) // len(descs) if descs else 0
    lines += ["## Shape", "",
              f"- Avg summary length: prod {_avg_desc(prod_arts)} chars, "
              f"staging {_avg_desc(staging_arts)} chars", ""]

    judge = None
    if judge_config is not None:
        prod_md = (Path(prod_root) / f"{date}.md")
        staging_md = (Path(staging_root) / f"{date}.md")
        if prod_md.exists() and staging_md.exists():
            judge = llm_judge(prod_md.read_text(encoding="utf-8"),
                              staging_md.read_text(encoding="utf-8"),
                              judge_config)
            lines += ["## Blind LLM judgment", ""]
            for dim, v in judge.items():
                if dim.startswith("_"):
                    continue
                lines.append(f"- **{dim}**: {v['winner']} — {v['note']}")
            lines.append("")

    summary = {
        "date": date,
        "prod_articles": len(prod_arts),
        "staging_articles": len(staging_arts),
        "shared": len(m["shared"]),
        "prod_only": len(m["prod_only"]),
        "staging_only": len(m["staging_only"]),
        "prod_repeats": len(prod_repeats),
        "staging_repeats": len(staging_repeats),
        "prod_cost": (prod_run or {}).get("totals", {}).get("cost"),
        "staging_cost": (staging_run or {}).get("totals", {}).get("cost"),
        "judge_overall": (judge or {}).get("overall", {}).get("winner"),
    }
    return "\n".join(lines), summary


def notify_summary(summary: dict, config: dict) -> None:
    from digest_pipeline import delivery
    parts = [
        f"📊 Prod vs staging — {summary['date']}",
        f"Articles: prod {summary['prod_articles']} / staging {summary['staging_articles']} "
        f"(shared {summary['shared']})",
        f"Repeat stories shipped: prod {summary['prod_repeats']} / "
        f"staging {summary['staging_repeats']}",
    ]
    if summary["prod_cost"] is not None or summary["staging_cost"] is not None:
        parts.append(f"Cost: prod ${summary['prod_cost'] or 0:.2f} / "
                     f"staging ${summary['staging_cost'] or 0:.2f}")
    if summary["judge_overall"]:
        parts.append(f"Blind judge overall: {summary['judge_overall']}")
    delivery.send_notification("\n".join(parts), config)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prod_root", type=Path)
    ap.add_argument("staging_root", type=Path)
    ap.add_argument("--date", help="Compare one date (default: newest common date)")
    ap.add_argument("--days", type=int, help="Compare the last N common dates")
    ap.add_argument("--llm-judge", action="store_true",
                    help="Blind A/B quality judgment (one Opus call per date)")
    ap.add_argument("--notify", metavar="CONFIG",
                    help="Send a Telegram summary using this config.json's notify settings")
    ap.add_argument("--out-dir", type=Path,
                    help="Where to write compare-<date>.md (default: staging root)")
    args = ap.parse_args()

    prod_days = load_archive(args.prod_root)
    staging_days = load_archive(args.staging_root)
    common = sorted(set(prod_days) & set(staging_days))
    if not common:
        print("No common dates found between the two data roots.")
        sys.exit(2)

    if args.date:
        if args.date not in common:
            print(f"Date {args.date} not present on both sides "
                  f"(common: {common[-5:]})")
            sys.exit(2)
        dates = [args.date]
    elif args.days:
        dates = common[-args.days:]
    else:
        dates = [common[-1]]

    judge_config = None
    notify_config = None
    if args.llm_judge or args.notify:
        from digest_pipeline.config import load_config
        cfg_path = args.notify or str(Path(args.staging_root) / "config.json")
        cfg = load_config(cfg_path)
        judge_config = cfg if args.llm_judge else None
        notify_config = cfg if args.notify else None

    out_dir = args.out_dir or args.staging_root
    summaries = []
    for date in dates:
        report, summary = compare_date(args.prod_root, args.staging_root, date,
                                       prod_days, staging_days,
                                       judge_config=judge_config)
        out_path = Path(out_dir) / f"compare-{date}.md"
        out_path.write_text(report + "\n", encoding="utf-8")
        print(report)
        print(f"\n[written to {out_path}]\n")
        summaries.append(summary)
        if notify_config is not None:
            notify_summary(summary, notify_config)

    if len(summaries) > 1:
        print("## Rollup")
        print(f"{'date':<12} {'prod':>5} {'stag':>5} {'shared':>7} "
              f"{'rep-p':>6} {'rep-s':>6} {'$prod':>7} {'$stag':>7}")
        for s in summaries:
            print(f"{s['date']:<12} {s['prod_articles']:>5} {s['staging_articles']:>5} "
                  f"{s['shared']:>7} {s['prod_repeats']:>6} {s['staging_repeats']:>6} "
                  f"{(s['prod_cost'] or 0):>7.2f} {(s['staging_cost'] or 0):>7.2f}")


if __name__ == "__main__":
    main()
