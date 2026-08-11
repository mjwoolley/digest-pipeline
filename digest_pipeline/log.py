"""Structured logging for the digest pipeline."""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path


def setup_logger(date: str, logs_dir: Path = None) -> logging.Logger:
    """Configure logger with file + console handlers.
    File: {logs_dir}/digest-YYYY-MM-DD.log
    Format: [TIMESTAMP] [LEVEL] [STAGE] message
    """
    if logs_dir is None:
        logs_dir = Path.cwd() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("digest")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    # File handler — explicit UTF-8 so emoji in titles/log lines can't raise
    # UnicodeEncodeError under a C/POSIX locale (cron, systemd without LANG=)
    fh = logging.FileHandler(logs_dir / f"digest-{date}.log",
                             encoding="utf-8", errors="replace")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Clean old logs (keep 30 days)
    _cleanup_old_logs(logs_dir, 30)

    return logger


def _cleanup_old_logs(logs_dir: Path, keep_days: int):
    """Delete log files older than keep_days."""
    if not logs_dir.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    for f in logs_dir.glob("digest-*.log"):
        try:
            date_str = f.stem.replace("digest-", "")
            log_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if log_date < cutoff:
                f.unlink()
        except (ValueError, OSError):
            pass
