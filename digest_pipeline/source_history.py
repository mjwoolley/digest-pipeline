"""Per-source historical ledger.

Appends one row per (date, source_key) to ``source_history.jsonl`` at the
end of each successful run, so the console (and any future audit job)
can aggregate over arbitrary windows without re-walking ``work/<date>/``
artifacts every time.

Row shape:
    {"date": "YYYY-MM-DD", "source_key": "...",
     "extracted": int, "in_digest": int, "dropped": int,
     "exclusive": int, "avg_score": float|null}

Only sources that produced at least one extracted article on the given
day get a row — joining against the configured sources list answers
"was this source silent today?" without bloating the file.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("digest")

LEDGER_FILENAME = "source_history.jsonl"


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[SOURCE-HISTORY] Failed to read {path}: {e}")
        return None


def compute_daily_rows(work_dir: Path, date: str) -> list[dict]:
    """Read the day's pipeline artifacts and produce per-source ledger rows.

    Mirrors ``_compute_source_metrics`` in console_api.py but scoped to a
    single day. Prefers ``prioritized.json`` for the kept list; falls back
    to ``deduped.json`` on runs where prioritize was skipped.
    """
    extracted = _read_json(work_dir / "extracted.json") or []

    kept_path = work_dir / "prioritized.json"
    if not kept_path.exists():
        kept_path = work_dir / "deduped.json"
    kept = _read_json(kept_path) or []

    dropped = _read_json(work_dir / "prioritized_dropped.json") or []
    final_list = _read_json(work_dir / "deduped.json") or []

    metrics: dict[str, dict] = {}

    def _ensure(sk: str) -> dict:
        if sk not in metrics:
            metrics[sk] = {
                "extracted": 0,
                "in_digest": 0,
                "dropped": 0,
                "exclusive": 0,
                "scores": [],
            }
        return metrics[sk]

    for art in extracted:
        sk = art.get("source_key")
        if sk:
            _ensure(sk)["extracted"] += 1

    for art in kept:
        score = art.get("_priority_score")
        for sk in art.get("source_keys", []):
            m = _ensure(sk)
            m["in_digest"] += 1
            if score is not None:
                m["scores"].append(score)

    for art in dropped:
        score = art.get("_priority_score")
        for sk in art.get("source_keys", []):
            m = _ensure(sk)
            m["dropped"] += 1
            if score is not None:
                m["scores"].append(score)

    # Exclusive: sole source on a final-list article. Use the kept list when
    # prioritize ran, deduped otherwise. (When prioritize ran, deduped.json
    # holds both kept + dropped, which would overcount.)
    for art in kept if kept_path.name == "prioritized.json" else final_list:
        skeys = art.get("source_keys", [])
        if len(skeys) == 1:
            _ensure(skeys[0])["exclusive"] += 1

    rows = []
    for sk, m in metrics.items():
        if m["extracted"] == 0:
            continue
        avg = round(sum(m["scores"]) / len(m["scores"]), 2) if m["scores"] else None
        rows.append({
            "date": date,
            "source_key": sk,
            "extracted": m["extracted"],
            "in_digest": m["in_digest"],
            "dropped": m["dropped"],
            "exclusive": m["exclusive"],
            "avg_score": avg,
        })
    rows.sort(key=lambda r: r["source_key"])
    return rows


def _last_date_in_ledger(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            try:
                f.seek(-2, 2)
                while f.read(1) != b"\n":
                    f.seek(-2, 1)
            except OSError:
                f.seek(0)
            last_line = f.readline().decode()
        return json.loads(last_line).get("date") if last_line.strip() else None
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[SOURCE-HISTORY] Failed to read last line of {path}: {e}")
        return None


def append_daily(data_root: Path, work_dir: Path, date: str) -> int:
    """Append one row per active source for ``date`` to the ledger.

    Idempotent: if the last line already records ``date``, returns 0
    without writing anything. Returns the number of rows written.
    """
    ledger_path = data_root / LEDGER_FILENAME
    if _last_date_in_ledger(ledger_path) == date:
        logger.info(f"[SOURCE-HISTORY] Ledger already has rows for {date}; skipping")
        return 0

    rows = compute_daily_rows(work_dir, date)
    if not rows:
        return 0

    with ledger_path.open("a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    logger.info(f"[SOURCE-HISTORY] Appended {len(rows)} rows for {date}")
    return len(rows)


def rebuild(data_root: Path, date_re) -> int:
    """Backfill the ledger from scratch by walking ``data_root/work/<date>/``.

    Overwrites any existing ledger. Returns the total number of rows written.
    """
    ledger_path = data_root / LEDGER_FILENAME
    work_root = data_root / "work"
    if not work_root.exists():
        logger.warning(f"[SOURCE-HISTORY] No work/ dir under {data_root}; nothing to backfill")
        ledger_path.unlink(missing_ok=True)
        return 0

    day_dirs = sorted(
        d for d in work_root.iterdir()
        if d.is_dir() and date_re.match(d.name)
    )

    total = 0
    with ledger_path.open("w") as f:
        for day_dir in day_dirs:
            rows = compute_daily_rows(day_dir, day_dir.name)
            for row in rows:
                f.write(json.dumps(row) + "\n")
            total += len(rows)
    logger.info(f"[SOURCE-HISTORY] Rebuilt {ledger_path} with {total} rows across {len(day_dirs)} days")
    return total
