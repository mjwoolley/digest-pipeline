"""Structured JSON run log for digest and podcast pipelines.

Each run writes a single ``run.json`` file that the management console reads
to render run history, stage timelines, and totals. The file is rewritten
after every stage so a crashed run still has a partial record up to the
failure point.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path


class RunLog:
    """Writes a structured run.json to the work directory, updated after each stage."""

    def __init__(self, digest_name: str, date: str, work_dir: Path,
                 path: Path = None, pipeline_type: str = "digest"):
        self._path = path if path else work_dir / "run.json"
        self._start = time.time()
        self._data = {
            "digest": digest_name,
            "date": date,
            "pipeline_type": pipeline_type,
            "started_at": _now(),
            "completed_at": None,
            "status": "running",
            "error": None,
            "duration_s": None,
            "stages": [],
            "totals": None,
        }
        self._flush()

    def log_stage(self, stage: str, detail: str, usage: dict = None):
        """Append an info entry for ``stage`` and flush to disk.

        ``usage`` is the raw provider response dict (with ``input_tokens`` /
        ``output_tokens`` / ``cost`` / ``duration``) — both OpenAI- and
        Anthropic-shaped keys are accepted.
        """
        entry = {
            "stage": stage,
            "timestamp": _now(),
            "type": "info",
            "detail": detail,
        }
        if usage:
            entry["duration_s"] = usage.get("duration")
            entry["tokens"] = {
                "input": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                "output": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                "cost": usage.get("cost", 0.0),
            }
        self._data["stages"].append(entry)
        self._flush()

    def log_error(self, stage: str, error: str):
        """Append an error entry for ``stage``. Does not mark the run failed."""
        self._data["stages"].append({
            "stage": stage,
            "timestamp": _now(),
            "type": "error",
            "detail": error,
        })
        self._flush()

    def complete(self, tracker):
        """Mark the run successful and aggregate totals from a TokenTracker."""
        self._data["status"] = "success"
        self._data["completed_at"] = _now()
        self._data["duration_s"] = round(time.time() - self._start, 1)
        self._data["totals"] = {
            "input_tokens": tracker.total_input,
            "output_tokens": tracker.total_output,
            "cost": round(tracker.total_cost, 4),
        }
        self._flush()

    def complete_raw(self, totals: dict = None):
        """Mark run as success with explicit totals dict (no TokenTracker needed)."""
        self._data["status"] = "success"
        self._data["completed_at"] = _now()
        self._data["duration_s"] = round(time.time() - self._start, 1)
        self._data["totals"] = totals
        self._flush()

    def fail(self, error: str):
        """Mark the run failed with ``error`` as the top-level reason."""
        self._data["status"] = "failure"
        self._data["completed_at"] = _now()
        self._data["duration_s"] = round(time.time() - self._start, 1)
        self._data["error"] = error
        self._flush()

    def _flush(self):
        self._path.write_text(json.dumps(self._data, indent=2))


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
