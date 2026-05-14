"""Weekly source-health audit.

Classifies each configured source by *why* it's healthy/unhealthy,
based on disk artifacts only — no network calls. Output feeds a
Markdown report that's pushed via the existing Telegram notification
channel.

The audit reuses the same Healthy / Warning / Stale classification as
the console's Source Health page; this module adds the cause layer:
which signal explains the badge state, and what action to suggest.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .source_history import LEDGER_FILENAME
from .source_state import STATE_FILENAME

logger = logging.getLogger("digest")

# ── Tunable thresholds ──────────────────────────────────────────────────────

# How many consecutive recent runs we sample work-dir artifacts from.
RECENT_RUN_WINDOW = 3

# Grace period for newly-added sources before we call them NEVER_YIELDED.
NEW_SOURCE_GRACE_DAYS = 14

# Yield-drop sensitivity (recent_per_day < THRESHOLD * baseline_per_day).
YIELD_DROP_RATIO = 0.4

# Minimum historical baseline to consider YIELD_DROP (avoids noise on
# already-quiet sources where small swings aren't meaningful).
YIELD_DROP_MIN_BASELINE = 0.3  # articles/day over 90 days

# Recent window (days) for yield-drop detection.
YIELD_DROP_RECENT_DAYS = 14

# Historical baseline window (days) for yield-drop detection.
YIELD_DROP_BASELINE_DAYS = 90

# Bytes threshold under which a raw fetch counts as "empty result"
# (catches "No tweets found.\n" → 16 chars).
RAW_EMPTY_THRESHOLD = 50

# Bytes threshold above which a raw fetch counts as "real content".
RAW_NORMAL_THRESHOLD = 500

# Cap on Telegram message length (Telegram allows 4096; leave headroom).
TELEGRAM_LENGTH_CAP = 3800


# ── Cause codes ─────────────────────────────────────────────────────────────

CAUSE_NEVER_YIELDED = "NEVER_YIELDED"
CAUSE_NOT_FETCHED = "NOT_FETCHED"
CAUSE_FETCH_EMPTY = "FETCH_EMPTY"
CAUSE_CONTENT_NO_ARTICLES = "CONTENT_BUT_NO_ARTICLES"
CAUSE_EXTRACTED_BUT_LOST = "EXTRACTED_BUT_LOST"
CAUSE_NORMAL_LOW_CADENCE = "NORMAL_LOW_CADENCE"
CAUSE_YIELD_DROP = "YIELD_DROP"
CAUSE_HEALTHY = "HEALTHY"
CAUSE_UNCLASSIFIED = "UNCLASSIFIED"

ACTION_BUCKETS = {
    CAUSE_NEVER_YIELDED: "Action recommended",
    CAUSE_NOT_FETCHED: "Action recommended",
    CAUSE_FETCH_EMPTY: "Action recommended",
    CAUSE_CONTENT_NO_ARTICLES: "Action recommended",
    CAUSE_EXTRACTED_BUT_LOST: "Action recommended",
    CAUSE_YIELD_DROP: "Yield dropped",
    CAUSE_NORMAL_LOW_CADENCE: "Expected quiet — no action",
    CAUSE_UNCLASSIFIED: "Action recommended",
}


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class SourceFinding:
    source_key: str
    source_type: str
    name: str
    badge: str  # "healthy" | "warning" | "stale"
    cause: str  # one of CAUSE_*
    evidence: str  # 1-2 sentence human-readable explanation
    suggested_action: str
    sort_key: int  # lower = more urgent

    @property
    def bucket(self) -> str:
        return ACTION_BUCKETS.get(self.cause, "Action recommended")


@dataclass
class AuditReport:
    digest_name: str
    digest_slug: str
    date: str  # ISO date the audit ran
    findings: list[SourceFinding] = field(default_factory=list)
    total_sources: int = 0
    healthy_count: int = 0

    @property
    def actionable(self) -> list[SourceFinding]:
        return [f for f in self.findings if f.cause != CAUSE_HEALTHY]


def to_json(report: AuditReport) -> str:
    """Serialize an AuditReport to JSON for programmatic consumers."""
    payload = {
        "digest_name": report.digest_name,
        "digest_slug": report.digest_slug,
        "date": report.date,
        "total_sources": report.total_sources,
        "healthy_count": report.healthy_count,
        "actionable_count": len(report.actionable),
        "findings": [asdict(f) | {"bucket": f.bucket} for f in report.findings],
    }
    return json.dumps(payload, indent=2)


# ── Public entry point ─────────────────────────────────────────────────────

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def audit(data_root: Path, config: dict, today: Optional[str] = None) -> AuditReport:
    """Classify every configured source and return an AuditReport.

    Pure-Python; all signals come from files under ``data_root``.
    """
    today_str = today or date_cls.today().isoformat()
    digest_name = config.get("digest", {}).get("name", "Digest")
    digest_slug = data_root.name

    cfg_sources = _list_sources(config)
    state = _read_state(data_root)
    ledger_rows = _read_ledger(data_root)
    work_history = _recent_work_dirs(data_root, RECENT_RUN_WINDOW)

    findings = []
    healthy_count = 0
    for src in cfg_sources:
        finding = _classify_source(
            src=src,
            state=state.get(src["source_key"], {}),
            ledger_rows=[r for r in ledger_rows if r.get("source_key") == src["source_key"]],
            work_history=work_history,
            today=today_str,
        )
        if finding.cause == CAUSE_HEALTHY:
            healthy_count += 1
        findings.append(finding)

    findings.sort(key=lambda f: (f.sort_key, f.source_key))
    return AuditReport(
        digest_name=digest_name,
        digest_slug=digest_slug,
        date=today_str,
        findings=findings,
        total_sources=len(cfg_sources),
        healthy_count=healthy_count,
    )


# ── Classification ─────────────────────────────────────────────────────────

def _classify_source(
    src: dict,
    state: dict,
    ledger_rows: list[dict],
    work_history: list[dict],
    today: str,
) -> SourceFinding:
    """Walk the decision tree from the plan and return one finding."""
    source_key = src["source_key"]
    stale_after_days = src.get("stale_after_days") or 3
    badge = _badge_state(state, stale_after_days, today)

    # Trend regression check runs independently — even on healthy sources.
    yield_drop = _yield_drop(ledger_rows, today)

    if badge == "healthy":
        if yield_drop is not None:
            return SourceFinding(
                source_key=source_key,
                source_type=src["source_type"],
                name=src["name"],
                badge=badge,
                cause=CAUSE_YIELD_DROP,
                evidence=yield_drop,
                suggested_action="Review — content still arriving but at lower rate.",
                sort_key=20,
            )
        return SourceFinding(
            source_key=source_key,
            source_type=src["source_type"],
            name=src["name"],
            badge=badge,
            cause=CAUSE_HEALTHY,
            evidence="",
            suggested_action="",
            sort_key=99,
        )

    # Below: badge is "warning" or "stale" — diagnose the cause.

    # Step 1: Was this source EVER in the ledger?
    if not ledger_rows:
        # No ledger rows AND no state entry → the pipeline has never
        # processed this source. Most likely newly added. Treat as in
        # grace window rather than alarm.
        if not state.get("last_fetched") and not state.get("last_included"):
            return SourceFinding(
                source_key=source_key,
                source_type=src["source_type"],
                name=src["name"],
                badge=badge,
                cause=CAUSE_HEALTHY,
                evidence="No pipeline runs yet for this source — likely newly added.",
                suggested_action="",
                sort_key=99,
            )
        days_since_added = _days_since_first_state(state, today)
        if days_since_added < NEW_SOURCE_GRACE_DAYS:
            return SourceFinding(
                source_key=source_key,
                source_type=src["source_type"],
                name=src["name"],
                badge=badge,
                cause=CAUSE_HEALTHY,
                evidence=f"Recently added ({days_since_added} days ago); within grace window.",
                suggested_action="",
                sort_key=99,
            )
        return SourceFinding(
            source_key=source_key,
            source_type=src["source_type"],
            name=src["name"],
            badge=badge,
            cause=CAUSE_NEVER_YIELDED,
            evidence=f"Configured but no article ever produced ({days_since_added}d).",
            suggested_action="Verify the handle/URL resolves to a real account; consider drop/replace.",
            sort_key=0,
        )

    # Step 2: Source has historical activity. Active in recent window?
    cutoff_str = (date_cls.fromisoformat(today) - timedelta(days=stale_after_days)).isoformat()
    recent_ledger = [r for r in ledger_rows if r["date"] >= cutoff_str]
    if recent_ledger and all(r.get("in_digest", 0) > 0 for r in recent_ledger):
        # Recent activity AND every recent run included an article — actually
        # healthy (badge may lag if state file write was skipped).
        return SourceFinding(
            source_key=source_key,
            source_type=src["source_type"],
            name=src["name"],
            badge=badge,
            cause=CAUSE_HEALTHY,
            evidence="",
            suggested_action="",
            sort_key=99,
        )

    # Step 2a: Recent extracted but NONE in_digest → EXTRACTED_BUT_LOST.
    # Require a real pattern (≥2 active days AND ≥2 extracted) so a one-off
    # blip doesn't trigger.
    if (recent_ledger and len(recent_ledger) >= 2
            and all(r.get("in_digest", 0) == 0 for r in recent_ledger)
            and sum(r.get("extracted", 0) for r in recent_ledger) >= 2):
        total_extracted = sum(r.get("extracted", 0) for r in recent_ledger)
        return SourceFinding(
            source_key=source_key,
            source_type=src["source_type"],
            name=src["name"],
            badge=badge,
            cause=CAUSE_EXTRACTED_BUT_LOST,
            evidence=f"{total_extracted} articles extracted in last {stale_after_days}d, none made the digest.",
            suggested_action="Review prioritize scores / clustering; consider raising weight or dropping.",
            sort_key=10,
        )

    # Step 2b: Quiet in recent window. Check NORMAL_LOW_CADENCE first.
    cadence = _cadence_status(ledger_rows, today, stale_after_days)
    if cadence == "normal":
        last_date = max(r["date"] for r in ledger_rows)
        days_quiet = (date_cls.fromisoformat(today) - date_cls.fromisoformat(last_date)).days
        median_gap = _median_active_gap(ledger_rows)
        return SourceFinding(
            source_key=source_key,
            source_type=src["source_type"],
            name=src["name"],
            badge=badge,
            cause=CAUSE_NORMAL_LOW_CADENCE,
            evidence=f"Last article {days_quiet}d ago; historical cadence ~{median_gap}d gap.",
            suggested_action="Within expected cadence — no action.",
            sort_key=50,
        )

    # Step 3: Inspect recent raw fetches to classify the failure mode.
    recent_raws = _recent_raw_sizes(work_history, src)
    if not recent_raws:
        return SourceFinding(
            source_key=source_key,
            source_type=src["source_type"],
            name=src["name"],
            badge=badge,
            cause=CAUSE_NOT_FETCHED,
            evidence=f"No raw fetch file in last {len(work_history)} runs.",
            suggested_action="Source not being attempted — check pipeline config / source filter.",
            sort_key=2,
        )

    empty_count = sum(1 for _, sz in recent_raws if sz < RAW_EMPTY_THRESHOLD)
    normal_count = sum(1 for _, sz in recent_raws if sz >= RAW_NORMAL_THRESHOLD)
    articles_seen = _extracted_in_recent_runs(work_history, src)

    # Majority-empty fetches → broken source (matches the @cursorai pattern
    # where bird returns "No tweets found." consistently).
    if empty_count >= max(2, (len(recent_raws) + 1) // 2):
        sample = _sample_raw_content(work_history, src)
        snippet = repr(sample[:60]) if sample else "<empty>"
        return SourceFinding(
            source_key=source_key,
            source_type=src["source_type"],
            name=src["name"],
            badge=badge,
            cause=CAUSE_FETCH_EMPTY,
            evidence=f"{empty_count}/{len(recent_raws)} recent fetches ≤{RAW_EMPTY_THRESHOLD} chars (sample: {snippet}).",
            suggested_action="Verify the handle/URL still resolves; the source may be broken.",
            sort_key=1,
        )

    # Real content arriving but nothing extracted across the recent window.
    if normal_count >= 1 and articles_seen == 0:
        avg = int(sum(s for _, s in recent_raws) / len(recent_raws))
        return SourceFinding(
            source_key=source_key,
            source_type=src["source_type"],
            name=src["name"],
            badge=badge,
            cause=CAUSE_CONTENT_NO_ARTICLES,
            evidence=f"Content arrived ({len(recent_raws)} runs, avg {avg} chars) but extractor found nothing AI-relevant.",
            suggested_action="Recent posts may be off-topic; consider drop if pattern persists.",
            sort_key=15,
        )

    # Mixed sizes with at least some content + some articles getting through
    # is the normal pattern for a healthy-but-irregular source. Treat as
    # normal cadence rather than alarm.
    return SourceFinding(
        source_key=source_key,
        source_type=src["source_type"],
        name=src["name"],
        badge=badge,
        cause=CAUSE_NORMAL_LOW_CADENCE,
        evidence=f"Activity in last {len(recent_raws)} runs is intermittent ({articles_seen} articles extracted).",
        suggested_action="Within expected cadence — no action.",
        sort_key=50,
    )


# ── Helpers ────────────────────────────────────────────────────────────────

def _list_sources(config: dict) -> list[dict]:
    """Build the flat configured-source list. Mirrors console_api._list_sources
    in shape; duplicated here to avoid a console_api import (which pulls Flask)."""
    sources_cfg = config.get("sources", {})
    twitter_cfg = sources_cfg.get("twitter", {})
    twitter_default = _stale_threshold(twitter_cfg)
    result = []

    for acct in twitter_cfg.get("accounts", []):
        result.append({
            "source_key": f"twitter:{acct}",
            "source_type": "twitter",
            "name": f"@{acct}",
            "stale_after_days": twitter_default,
        })

    for key, blog in sources_cfg.get("blogs", {}).items():
        result.append({
            "source_key": f"blog:{key}",
            "source_type": "blog",
            "name": blog.get("name", key),
            "stale_after_days": _stale_threshold(blog),
        })

    for key, blog in sources_cfg.get("research", {}).items():
        result.append({
            "source_key": f"research:{key}",
            "source_type": "research",
            "name": blog.get("name", key),
            "stale_after_days": _stale_threshold(blog),
        })

    nl_cfg = sources_cfg.get("newsletters", {})
    nl_default = _stale_threshold(nl_cfg)
    for key, nl in nl_cfg.get("sources", {}).items():
        result.append({
            "source_key": f"newsletter:{key}",
            "source_type": "newsletter",
            "name": nl.get("name", key),
            "stale_after_days": _stale_threshold(nl) if "stale_after_days" in nl else nl_default,
        })

    gt = sources_cfg.get("github_trending")
    if gt:
        result.append({
            "source_key": "github_trending:trending",
            "source_type": "github_trending",
            "name": gt.get("name", "GitHub Trending"),
            "stale_after_days": _stale_threshold(gt),
        })
    return result


def _stale_threshold(block: dict) -> int:
    val = block.get("stale_after_days")
    return int(val) if isinstance(val, (int, float)) and val > 0 else 3


def _read_state(data_root: Path) -> dict:
    path = data_root / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    # Apply the same migration shim as source_state.load_state.
    for entry in raw.values():
        if "last_updated" in entry:
            if not entry.get("last_fetched"):
                entry["last_fetched"] = entry["last_updated"]
            del entry["last_updated"]
    return raw


def _read_ledger(data_root: Path) -> list[dict]:
    path = data_root / LEDGER_FILENAME
    if not path.exists():
        return []
    rows = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _recent_work_dirs(data_root: Path, n: int) -> list[dict]:
    """Return the last `n` work/<date>/ directories sorted newest-first,
    each as {"date": str, "path": Path, "run_json": dict|None}.
    """
    work_root = data_root / "work"
    if not work_root.exists():
        return []
    candidates = sorted(
        (d for d in work_root.iterdir() if d.is_dir() and DATE_RE.match(d.name)),
        reverse=True,
    )
    history = []
    for d in candidates:
        run_json = None
        run_path = d / "run.json"
        if run_path.exists():
            try:
                run_json = json.loads(run_path.read_text())
            except (json.JSONDecodeError, OSError):
                run_json = None
        history.append({"date": d.name, "path": d, "run_json": run_json})
        if len(history) >= n:
            break
    return history


def _badge_state(state: dict, stale_after_days: int, today: str) -> str:
    last_fetched = state.get("last_fetched")
    last_included = state.get("last_included")
    if not last_fetched or _days_since(last_fetched, today) > stale_after_days:
        return "stale"
    if not last_included or _days_since(last_included, today) > stale_after_days:
        return "warning"
    return "healthy"


def _days_since(iso_date: str, today: str) -> int:
    try:
        return (date_cls.fromisoformat(today) - date_cls.fromisoformat(iso_date)).days
    except ValueError:
        return 10**9  # treat unparseable dates as ancient


def _days_since_first_state(state: dict, today: str) -> int:
    """Best-effort: how long has this source been in state? Falls back to a
    large number when state has nothing to date-anchor on."""
    candidates = [state.get("last_fetched"), state.get("last_included")]
    candidates = [c for c in candidates if c]
    if not candidates:
        return 10**6
    earliest = min(candidates)
    return _days_since(earliest, today)


def _yield_drop(ledger_rows: list[dict], today: str) -> Optional[str]:
    """Return a human-readable evidence string if a yield drop is detected,
    else None."""
    if len(ledger_rows) < 5:
        return None
    today_d = date_cls.fromisoformat(today)
    recent_cutoff = (today_d - timedelta(days=YIELD_DROP_RECENT_DAYS)).isoformat()
    baseline_cutoff = (today_d - timedelta(days=YIELD_DROP_BASELINE_DAYS)).isoformat()
    recent = [r for r in ledger_rows if r["date"] >= recent_cutoff]
    baseline = [r for r in ledger_rows if r["date"] >= baseline_cutoff]
    if not baseline:
        return None
    recent_per_day = sum(r.get("extracted", 0) for r in recent) / YIELD_DROP_RECENT_DAYS
    baseline_per_day = sum(r.get("extracted", 0) for r in baseline) / YIELD_DROP_BASELINE_DAYS
    if baseline_per_day < YIELD_DROP_MIN_BASELINE:
        return None
    if recent_per_day < YIELD_DROP_RATIO * baseline_per_day:
        return (
            f"Averaging {recent_per_day:.2f} articles/day last "
            f"{YIELD_DROP_RECENT_DAYS}d vs {baseline_per_day:.2f}/day over "
            f"{YIELD_DROP_BASELINE_DAYS}d."
        )
    return None


def _median_active_gap(ledger_rows: list[dict]) -> int:
    """Median day-gap between consecutive active days in the ledger."""
    if len(ledger_rows) < 2:
        return 0
    dates = sorted({r["date"] for r in ledger_rows})
    gaps = []
    for prev, cur in zip(dates, dates[1:]):
        gaps.append(
            (date_cls.fromisoformat(cur) - date_cls.fromisoformat(prev)).days
        )
    gaps.sort()
    return gaps[len(gaps) // 2]


def _cadence_status(ledger_rows: list[dict], today: str, stale_after_days: int) -> str:
    """Return "normal" if the current quiet streak is within historical cadence,
    else "abnormal"."""
    if len(ledger_rows) < 3:
        return "abnormal"
    median_gap = _median_active_gap(ledger_rows)
    if median_gap <= stale_after_days / 2:
        # Source is normally busy; any stretch beyond stale_after_days is abnormal.
        return "abnormal"
    last_date = max(r["date"] for r in ledger_rows)
    streak = _days_since(last_date, today)
    if streak <= 1.5 * median_gap:
        return "normal"
    return "abnormal"


def _recent_raw_sizes(work_history: list[dict], src: dict) -> list[tuple[str, int]]:
    """Return [(date, byte_size)] for raw fetch files in recent work-dirs.
    Missing files are excluded; presence/absence is the signal in those cases.
    """
    out = []
    fname = _raw_filename(src)
    if not fname:
        return out
    for entry in work_history:
        path = entry["path"] / fname
        if path.exists():
            try:
                out.append((entry["date"], path.stat().st_size))
            except OSError:
                continue
    return out


def _sample_raw_content(work_history: list[dict], src: dict) -> str:
    fname = _raw_filename(src)
    if not fname:
        return ""
    for entry in work_history:
        path = entry["path"] / fname
        if path.exists():
            try:
                return path.read_text(errors="replace").strip()
            except OSError:
                continue
    return ""


def _raw_filename(src: dict) -> Optional[str]:
    source_type = src["source_type"]
    key = src["source_key"].split(":", 1)[1] if ":" in src["source_key"] else src["source_key"]
    if source_type == "twitter":
        return f"raw-twitter-{key}.txt"
    if source_type == "blog":
        return f"raw-blog-{key}.txt"
    if source_type == "research":
        return f"raw-research-{key}.txt"
    if source_type == "newsletter":
        return f"raw-newsletter-{key}.txt"
    if source_type == "github_trending":
        return f"raw-github_trending-{key}.txt"
    return None


def _extracted_in_recent_runs(work_history: list[dict], src: dict) -> int:
    """Count articles in recent extracted.json files attributed to this source_key."""
    total = 0
    sk = src["source_key"]
    for entry in work_history:
        ex_path = entry["path"] / "extracted.json"
        if not ex_path.exists():
            continue
        try:
            arts = json.loads(ex_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(arts, list):
            continue
        total += sum(1 for a in arts if a.get("source_key") == sk)
    return total


# ── Telegram formatter ─────────────────────────────────────────────────────

# Order in which buckets appear in the report (most urgent first).
BUCKET_ORDER = [
    "Action recommended",
    "Yield dropped",
    "Expected quiet — no action",
]


def format_telegram(report: AuditReport, console_url: Optional[str] = None) -> str:
    """Render an AuditReport as a Markdown message for Telegram."""
    grouped: dict[str, list[SourceFinding]] = {b: [] for b in BUCKET_ORDER}
    for f in report.actionable:
        bucket = f.bucket
        grouped.setdefault(bucket, []).append(f)

    lines = [
        f"⚠️ {report.digest_name} — Source Audit ({report.date})",
    ]

    actionable_total = sum(len(v) for v in grouped.values())
    if actionable_total == 0:
        lines.append("")
        lines.append(f"All {report.total_sources} sources healthy. ✅")
        return "\n".join(lines)

    for bucket in BUCKET_ORDER:
        items = grouped.get(bucket, [])
        if not items:
            continue
        lines.append("")
        lines.append(f"*{bucket}* ({len(items)})")
        for f in items:
            lines.append(f"• `{f.source_key}` — {f.evidence}")
            if f.suggested_action:
                lines.append(f"    {f.suggested_action}")

    lines.append("")
    lines.append(f"Healthy: {report.healthy_count} of {report.total_sources}.")
    if console_url:
        lines.append(f"Detail: {console_url}")

    msg = "\n".join(lines)
    if len(msg) > TELEGRAM_LENGTH_CAP:
        msg = msg[: TELEGRAM_LENGTH_CAP - 20] + "\n…(truncated)"
    return msg
