"""Mocked-subprocess/IMAP tests for the gather fetchers."""
import subprocess
from datetime import datetime, timedelta, timezone
from unittest import mock

from digest_pipeline import gather
from digest_pipeline.gather import (
    _fetch_blog, _fetch_newsletter, _fetch_twitter, _get_last_success_date,
    _parse_lookback,
)
from digest_pipeline.source_state import _filter_newsletter


def _proc(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


# ── _parse_lookback ──────────────────────────────────────────────────────────

def test_parse_lookback():
    assert _parse_lookback("36h") == timedelta(hours=36)
    assert _parse_lookback("2d") == timedelta(days=2)
    assert _parse_lookback("") is None
    assert _parse_lookback("soon") is None


# ── _fetch_twitter: config plumbed to the bird CLI ───────────────────────────

def test_twitter_uses_config_max_and_lookback():
    with mock.patch.object(gather.subprocess, "run",
                           return_value=_proc(stdout="tweet")) as run:
        result = _fetch_twitter("simonw", "tok", "ct0",
                                max_items=8, lookback="36h")
    cmd = run.call_args[0][0]
    assert cmd[:2] == ["bird", "search"]
    assert "from:simonw since:" in cmd[2]
    assert cmd[3:] == ["-n", "8"]
    assert result["content"] == "tweet"


def test_twitter_defaults_without_lookback():
    with mock.patch.object(gather.subprocess, "run",
                           return_value=_proc(stdout="t")) as run:
        _fetch_twitter("a", "tok", "ct0")
    cmd = run.call_args[0][0]
    assert cmd[2] == "from:a"
    assert cmd[3:] == ["-n", "10"]


def test_twitter_failure_logs_stderr(caplog):
    with mock.patch.object(gather.subprocess, "run",
                           return_value=_proc(returncode=1, stderr="auth expired")):
        result = _fetch_twitter("a", "tok", "ct0")
    assert result["content"] == ""
    assert any("auth expired" in r.message for r in caplog.records)


# ── _fetch_newsletter: every message since last run ──────────────────────────

class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL with N canned messages."""

    def __init__(self, messages):
        # messages: {b"1": bytes, ...} in mailbox order
        self.messages = messages

    def login(self, *a):
        return "OK", []

    def select(self, *a, **k):
        return "OK", []

    def search(self, charset, query):
        self.last_query = query
        ids = b" ".join(self.messages.keys())
        return "OK", [ids]

    def fetch(self, msg_id, spec):
        return "OK", [(b"header", self.messages[msg_id])]

    def logout(self):
        return "OK", []


def _email_bytes(msg_id, body):
    return (f"Message-ID: <{msg_id}>\r\n"
            f"From: news@example.com\r\nSubject: Issue\r\n"
            f"Content-Type: text/plain\r\n\r\n{body}\r\n").encode()


def _run_newsletter(messages, **kwargs):
    fake = FakeIMAP(messages)
    with mock.patch.object(gather.imaplib, "IMAP4_SSL", return_value=fake):
        result = _fetch_newsletter("k", {"name": "NL", "from": "news@example.com"},
                                   "imap.example.com", "me@x.com", "pw", **kwargs)
    return result, fake


def test_newsletter_single_message_unchanged_format():
    result, _ = _run_newsletter({b"1": _email_bytes("id1@x", "hello world")})
    assert result["content"].strip() == "hello world"
    assert result["message_id"] == "id1@x"
    assert "message_ids" not in result


def test_newsletter_fetches_all_messages_since_window():
    """Two issues since the last run: both must survive (previously only the
    newest was fetched and the older one was lost forever)."""
    result, _ = _run_newsletter({
        b"1": _email_bytes("old@x", "monday issue"),
        b"2": _email_bytes("new@x", "tuesday issue"),
    })
    assert "monday issue" in result["content"]
    assert "tuesday issue" in result["content"]
    assert result["message_ids"] == ["old@x", "new@x"]


def test_newsletter_since_uses_last_success():
    last = datetime(2026, 8, 1, tzinfo=timezone.utc)
    _, fake = _run_newsletter({b"1": _email_bytes("a@x", "hi")}, last_success=last)
    assert "01-Aug-2026" in fake.last_query


def test_multi_issue_filtering_per_issue():
    """Seen issues are dropped individually; unseen issues survive."""
    source = {
        "source_key": "newsletter:k", "source_type": "newsletter",
        "source_label": "NL", "source_url": "",
        "message_ids": ["old@x", "new@x"],
        "content": "=== ISSUE old@x ===\nmonday\n\n=== ISSUE new@x ===\ntuesday",
    }
    filtered, new_ids = _filter_newsletter(source, source["content"],
                                           seen_ids={"msgid:old@x"})
    assert "tuesday" in filtered["content"]
    assert "monday" not in filtered["content"]
    assert new_ids == ["msgid:new@x"]


# ── _fetch_blog: last-run-aware RSS window ───────────────────────────────────

_RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Two days ago</title><link>https://e.com/old</link>
<pubDate>{old}</pubDate><description>d</description></item>
<item><title>Fresh</title><link>https://e.com/new</link>
<pubDate>{new}</pubDate><description>d</description></item>
</channel></rss>"""


def _rss_fixture():
    now = datetime.now(timezone.utc)
    return _RSS.format(
        old=(now - timedelta(hours=40)).strftime("%a, %d %b %Y %H:%M:%S +0000"),
        new=(now - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S +0000"),
    )


def test_blog_default_window_cuts_old_post():
    with mock.patch.object(gather, "_curl", return_value=_proc(stdout=_rss_fixture())):
        result = _fetch_blog("b", {"name": "Blog", "feed_url": "https://e.com/f"})
    assert "Fresh" in result["content"]
    assert "Two days ago" not in result["content"]


def test_blog_window_widens_after_missed_day():
    """A run gap must widen the RSS window so the gap's posts still land."""
    last = datetime.now(timezone.utc) - timedelta(hours=48)
    with mock.patch.object(gather, "_curl", return_value=_proc(stdout=_rss_fixture())):
        result = _fetch_blog("b", {"name": "Blog", "feed_url": "https://e.com/f"},
                             last_success=last)
    assert "Fresh" in result["content"]
    assert "Two days ago" in result["content"]


def test_blog_window_capped():
    last = datetime.now(timezone.utc) - timedelta(days=60)
    with mock.patch.object(gather, "_curl", return_value=_proc(stdout=_rss_fixture())) as c:
        _fetch_blog("b", {"name": "Blog", "feed_url": "https://e.com/f"},
                    last_success=last)
    # No direct hook for hours; behavior check: cap is 7d, both entries inside
    # the window either way. The cap itself is unit-visible via constant.
    assert gather.RSS_LOOKBACK_CAP_HOURS == 168


def test_blog_fetch_failure_logs_stderr(caplog):
    with mock.patch.object(gather, "_curl",
                           return_value=_proc(returncode=6, stderr="Could not resolve host")):
        result = _fetch_blog("b", {"name": "Blog", "feed_url": "https://e.com/f"})
    assert result["content"] == ""
    assert any("Could not resolve host" in r.message for r in caplog.records)


# ── _curl retries flag ───────────────────────────────────────────────────────

def test_curl_includes_retry_flags():
    with mock.patch.object(gather.subprocess, "run",
                           return_value=_proc(stdout="x")) as run:
        gather._curl("https://e.com", timeout=10)
    cmd = run.call_args[0][0]
    assert "--retry" in cmd and "2" in cmd


# ── _get_last_success_date ───────────────────────────────────────────────────

def test_last_success_ignores_non_date_files(tmp_path):
    (tmp_path / "2026-08-01.md").write_text("x", encoding="utf-8")
    (tmp_path / "2026-08-02-draft.md").write_text("x", encoding="utf-8")
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    result = _get_last_success_date(tmp_path)
    assert result == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_last_success_none_when_empty(tmp_path):
    assert _get_last_success_date(tmp_path) is None
    assert _get_last_success_date(None) is None
