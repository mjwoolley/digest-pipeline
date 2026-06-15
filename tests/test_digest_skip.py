"""Regression tests for the digest skip paths.

When a run has no new content the pipeline exits with a skip code (10/11) and
must send a Telegram skip notification. This used to crash with a NameError
because the notification f-strings referenced ``tagline``, which was only bound
much later in the delivery branch — so the notification silently never sent.
These tests pin the skip paths to: send exactly one notification (whose message
carries the brand name) and exit with the right skip code.
"""
import logging
from unittest import mock

import pytest

from digest_pipeline import digest


def _run_main_with_skip(tmp_path, gather_return):
    """Invoke digest.main() with collaborators stubbed; return (exit_code, messages)."""
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    config = {
        "_data_root": tmp_path,
        "digest": {"name": "Test Digest", "emoji": "🧪"},
        "llm": {"provider": "openrouter"},
        "delivery": {"notify": {"method": "telegram"}},
        "sources": {},
    }
    messages = []
    with mock.patch.object(digest, "load_config", return_value=config), \
         mock.patch.object(digest, "gather_all", return_value=gather_return), \
         mock.patch.object(digest, "setup_logger", return_value=logging.getLogger("test")), \
         mock.patch.object(digest, "RunLog", return_value=mock.MagicMock()), \
         mock.patch.object(digest.llm, "configure", return_value=None), \
         mock.patch.object(
             digest.delivery, "send_notification",
             side_effect=lambda msg, cfg: messages.append(msg) or True):
        with mock.patch("sys.argv", ["digest-pipeline", "/fake/config.json"]):
            with pytest.raises(SystemExit) as exc:
                digest.main()
    return exc.value.code, messages


def test_skip_all_sources_empty_notifies(tmp_path):
    code, messages = _run_main_with_skip(tmp_path, gather_return=[])
    assert code == 10
    assert len(messages) == 1
    assert "Test Digest" in messages[0]
    assert "skipped" in messages[0].lower()
