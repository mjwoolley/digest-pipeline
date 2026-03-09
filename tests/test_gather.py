"""Tests for digest_pipeline.gather — date parsing, RSS, HTML parsing, email extraction."""
import email
import email.policy
from datetime import datetime, timezone
from unittest.mock import patch

from digest_pipeline.gather import (
    _parse_date,
    parse_rss_recent,
    TrendingParser,
    parse_github_trending,
    _extract_email_text,
)


# ── _parse_date ──────────────────────────────────────────────────────────────

def test_parse_date_iso8601():
    dt = _parse_date("2025-01-15T10:30:00+00:00")
    assert dt is not None
    assert dt.year == 2025
    assert dt.month == 1
    assert dt.day == 15


def test_parse_date_iso8601_z():
    dt = _parse_date("2025-01-15T10:30:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_date_rfc2822():
    dt = _parse_date("Mon, 15 Jan 2025 10:30:00 +0000")
    assert dt is not None
    assert dt.year == 2025


def test_parse_date_gmt_suffix():
    dt = _parse_date("Mon, 15 Jan 2025 10:30:00 GMT")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_date_invalid():
    assert _parse_date("not a date") is None
    assert _parse_date("") is None


def test_parse_date_with_microseconds():
    dt = _parse_date("2025-01-15T10:30:00.123456+00:00")
    assert dt is not None


# ── parse_rss_recent ─────────────────────────────────────────────────────────

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Blog</title>
    <item>
      <title>Article One</title>
      <link>https://example.com/1</link>
      <description>First article description</description>
    </item>
    <item>
      <title>Article Two</title>
      <link>https://example.com/2</link>
      <description>Second article description</description>
    </item>
  </channel>
</rss>"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Feed</title>
  <entry>
    <title>Atom Entry</title>
    <link href="https://example.com/atom1"/>
    <summary>Atom summary</summary>
  </entry>
</feed>"""


def test_parse_rss_recent_rss2():
    result = parse_rss_recent(RSS_SAMPLE)
    assert result is not None
    assert "Article One" in result
    assert "Article Two" in result
    assert "TITLE:" in result


def test_parse_rss_recent_atom():
    result = parse_rss_recent(ATOM_SAMPLE)
    assert result is not None
    assert "Atom Entry" in result
    assert "https://example.com/atom1" in result


def test_parse_rss_recent_malformed():
    result = parse_rss_recent("this is not xml at all <><>><")
    assert result is None


def test_parse_rss_recent_empty_feed():
    xml = '<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'
    result = parse_rss_recent(xml)
    assert result == ""


# ── TrendingParser ───────────────────────────────────────────────────────────

TRENDING_HTML = """
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/openai/whisper">openai / whisper</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">Speech recognition model</p>
  <span itemprop="programmingLanguage">Python</span>
  <span class="d-inline-block float-sm-right">500 stars today</span>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/some/other-repo">some / other-repo</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">A web framework</p>
  <span itemprop="programmingLanguage">JavaScript</span>
  <span class="d-inline-block float-sm-right">50 stars today</span>
</article>
"""


def test_trending_parser():
    parser = TrendingParser()
    parser.feed(TRENDING_HTML)
    assert len(parser.repos) == 2
    assert parser.repos[0]["owner"] == "openai"
    assert parser.repos[0]["repo"] == "whisper"
    assert parser.repos[0]["language"] == "Python"
    assert parser.repos[0]["stars"] == 500
    assert parser.repos[0]["url"] == "https://github.com/openai/whisper"


def test_trending_parser_description():
    parser = TrendingParser()
    parser.feed(TRENDING_HTML)
    assert parser.repos[0]["description"] == "Speech recognition model"


def test_parse_github_trending_star_filter():
    with patch("digest_pipeline.gather._fetch_readme", return_value="ai machine learning"):
        result = parse_github_trending(TRENDING_HTML, min_stars=100, keywords=["ai"])
    assert "whisper" in result
    assert "other-repo" not in result  # only 50 stars


def test_parse_github_trending_keyword_filter():
    with patch("digest_pipeline.gather._fetch_readme", return_value="no matching keywords here"):
        result = parse_github_trending(TRENDING_HTML, min_stars=0, keywords=["blockchain"])
    assert result == ""  # no keyword match


def test_parse_github_trending_keyword_match():
    with patch("digest_pipeline.gather._fetch_readme", return_value="this is about ai models"):
        result = parse_github_trending(TRENDING_HTML, min_stars=0, keywords=["ai"])
    assert "whisper" in result


# ── _extract_email_text ──────────────────────────────────────────────────────

def test_extract_email_plain_text():
    msg = email.message_from_string(
        "Content-Type: text/plain\n\nHello, this is plain text.",
        policy=email.policy.default,
    )
    result = _extract_email_text(msg)
    assert "Hello, this is plain text." in result


def test_extract_email_html_only():
    msg = email.message_from_string(
        "Content-Type: text/html\n\n<html><body><p>Hello HTML</p></body></html>",
        policy=email.policy.default,
    )
    result = _extract_email_text(msg)
    assert "Hello HTML" in result
    assert "<p>" not in result  # tags stripped


def test_extract_email_empty():
    msg = email.message_from_string(
        "Content-Type: text/plain\n\n",
        policy=email.policy.default,
    )
    result = _extract_email_text(msg)
    assert result.strip() == ""
