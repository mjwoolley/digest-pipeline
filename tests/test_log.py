"""Tests for digest_pipeline.log — logger setup and log cleanup."""
import logging
from datetime import datetime, timedelta, timezone

from digest_pipeline.log import setup_logger, _cleanup_old_logs


# ── setup_logger ─────────────────────────────────────────────────────────────

def test_setup_logger_creates_log_file(tmp_path):
    logger = setup_logger("2099-01-15", tmp_path)
    assert isinstance(logger, logging.Logger)
    assert logger.name == "digest"
    log_file = tmp_path / "digest-2099-01-15.log"
    assert log_file.exists()
    # Clean up handlers to avoid interference with other tests
    logger.handlers.clear()


def test_setup_logger_creates_dir(tmp_path):
    logs_dir = tmp_path / "subdir" / "logs"
    logger = setup_logger("2099-01-15", logs_dir)
    assert logs_dir.exists()
    assert (logs_dir / "digest-2099-01-15.log").exists()
    logger.handlers.clear()


def test_setup_logger_writes_log(tmp_path):
    logger = setup_logger("2099-01-15", tmp_path)
    logger.info("test message")
    log_file = tmp_path / "digest-2099-01-15.log"
    content = log_file.read_text()
    assert "test message" in content
    logger.handlers.clear()


def test_setup_logger_clears_old_handlers(tmp_path):
    logger = setup_logger("2099-01-15", tmp_path)
    initial_count = len(logger.handlers)
    # Call again — should clear old handlers first
    logger = setup_logger("2099-01-15", tmp_path)
    assert len(logger.handlers) == initial_count
    logger.handlers.clear()


# ── _cleanup_old_logs ────────────────────────────────────────────────────────

def test_cleanup_old_logs_removes_old(tmp_path):
    # Create an old log (60 days ago)
    old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    old_file = tmp_path / f"digest-{old_date}.log"
    old_file.write_text("old log")

    # Create a recent log
    recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recent_file = tmp_path / f"digest-{recent_date}.log"
    recent_file.write_text("recent log")

    _cleanup_old_logs(tmp_path, 30)

    assert not old_file.exists()
    assert recent_file.exists()


def test_cleanup_old_logs_keeps_recent(tmp_path):
    recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    recent_file = tmp_path / f"digest-{recent_date}.log"
    recent_file.write_text("recent")

    _cleanup_old_logs(tmp_path, 30)
    assert recent_file.exists()


def test_cleanup_old_logs_nonexistent_dir(tmp_path):
    # Should not raise
    _cleanup_old_logs(tmp_path / "nonexistent", 30)


def test_cleanup_old_logs_ignores_non_matching(tmp_path):
    # Files that don't match the digest-*.log pattern should be left alone
    other_file = tmp_path / "other.log"
    other_file.write_text("keep me")
    _cleanup_old_logs(tmp_path, 0)
    assert other_file.exists()
