"""Tests for digest_pipeline.gather — date parsing, RSS, HTML parsing, email extraction."""
import email
import email.policy
from datetime import datetime, timezone
from unittest.mock import patch

from digest_pipeline.gather import (
    _parse_date,
    _is_bad_payload,
    _fetch_blog,
    parse_rss_recent,
    parse_anthropic_news,
    TrendingParser,
    parse_github_trending,
    _extract_email_text,
    _extract_message_id,
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


# ── _extract_message_id ─────────────────────────────────────────────────────

def test_extract_message_id():
    msg = email.message_from_string(
        "Message-ID: <abc123@example.com>\nContent-Type: text/plain\n\nBody",
        policy=email.policy.default,
    )
    assert _extract_message_id(msg) == "abc123@example.com"


def test_extract_message_id_no_brackets():
    msg = email.message_from_string(
        "Message-ID: abc123@example.com\nContent-Type: text/plain\n\nBody",
        policy=email.policy.default,
    )
    assert _extract_message_id(msg) == "abc123@example.com"


def test_extract_message_id_missing():
    msg = email.message_from_string(
        "Content-Type: text/plain\n\nBody",
        policy=email.policy.default,
    )
    assert _extract_message_id(msg) is None


# ── _is_bad_payload ──────────────────────────────────────────────────────────

def test_bad_payload_next_error():
    assert _is_bad_payload('<html><body>NEXT_HTTP_ERROR_FALLBACK</body></html>')


def test_bad_payload_next_error_flag():
    assert _is_bad_payload('<html __next_error__><body>error</body></html>')


def test_next_static_alone_is_not_bad():
    """_next/static by itself is legitimate — real Next.js pages have it."""
    html = '<script src="/_next/static/chunks/main.js"></script><p>Real content</p>'
    assert not _is_bad_payload(html)


def test_next_static_with_error_is_bad():
    """_next/static combined with an error marker should still reject."""
    html = '<html __next_error__><script src="/_next/static/chunks/main.js"></script></html>'
    assert _is_bad_payload(html)


def test_bad_payload_not_found_title():
    assert _is_bad_payload('<html><head><title>Not Found</title></head></html>')


def test_bad_payload_not_found_h1():
    assert _is_bad_payload('<html><body><h1>Not Found</h1></body></html>')


def test_bad_payload_valid_rss():
    assert not _is_bad_payload('<?xml version="1.0"?><rss><channel></channel></rss>')


def test_bad_payload_valid_html():
    assert not _is_bad_payload('<html><body><p>Real content here</p></body></html>')


# ── parse_anthropic_news ─────────────────────────────────────────────────────

ANTHROPIC_NEWS_HTML = """
<html>
<body>
  <!-- Curated/featured section (should be skipped) -->
  <section>
    <a href="/news/featured-article">Featured Article Title</a>
    <a href="/news/another-featured">Another Featured</a>
  </section>
  <!-- News heading marks start of chronological list -->
  <h2>News</h2>
  <a href="/news/claude-4-release">Claude 4 Release</a>
  <a href="/news/safety-research-update">Safety Research Update</a>
  <a href="/news/">All News</a>
  <a href="/careers">Careers</a>
  <a href="/news/claude-4-release">Claude 4 Release</a>
</body>
</html>
"""


def test_parse_anthropic_news_extracts_articles():
    result = parse_anthropic_news(ANTHROPIC_NEWS_HTML)
    assert "Claude 4 Release" in result
    assert "Safety Research Update" in result
    assert "https://www.anthropic.com/news/claude-4-release" in result
    assert "https://www.anthropic.com/news/safety-research-update" in result


def test_parse_anthropic_news_skips_featured_section():
    """Articles before the <h2>News</h2> heading should be skipped."""
    result = parse_anthropic_news(ANTHROPIC_NEWS_HTML)
    assert "Featured Article Title" not in result
    assert "Another Featured" not in result
    assert "featured-article" not in result


def test_parse_anthropic_news_skips_non_article_links():
    result = parse_anthropic_news(ANTHROPIC_NEWS_HTML)
    assert "/careers" not in result
    # /news/ and /news are skipped (index pages)
    lines = result.split("\n")
    link_lines = [l for l in lines if l.startswith("LINK:")]
    assert all("/news/" in l and l.strip() != "LINK: https://www.anthropic.com/news/" for l in link_lines)


def test_parse_anthropic_news_deduplicates():
    result = parse_anthropic_news(ANTHROPIC_NEWS_HTML)
    assert result.count("claude-4-release") == 1


def test_parse_anthropic_news_empty():
    result = parse_anthropic_news("<html><body><p>No links</p></body></html>")
    assert result == ""


def test_parse_anthropic_news_no_news_heading():
    """If there's no <h2>News</h2> heading, no articles are extracted."""
    html = '<html><body><a href="/news/some-article">Some Article</a></body></html>'
    result = parse_anthropic_news(html)
    assert result == ""


# ── Strategy dispatch ────────────────────────────────────────────────────────

def test_fetch_blog_strategy_dispatch_rss(tmp_path):
    """Default strategy uses RSS path (and rejects bad payloads)."""
    bad_html = '<html __next_error__><body>Not Found</body></html>'
    blog = {"name": "Test Blog", "feed_url": "http://example.com/rss"}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = bad_html
        result = _fetch_blog("test", blog)
    assert result["content"] == ""
    assert result["source_key"] == "blog:test"
    assert result["source_type"] == "blog"
    assert result["source_label"] == "Test Blog"


def test_fetch_blog_strategy_dispatch_html_scrape(tmp_path):
    """html_scrape strategy dispatches to scrape path."""
    blog = {
        "name": "Anthropic News",
        "strategy": "html_scrape",
        "url": "https://www.anthropic.com/news",
        "scrape_parser": "anthropic_news",
    }
    html = '<html><body><h2>News</h2><a href="/news/test-article">Test Article</a></body></html>'
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = html
        result = _fetch_blog("anthropic_blog", blog)
    assert "Test Article" in result["content"]
    assert "https://www.anthropic.com/news/test-article" in result["content"]
    assert result["source_key"] == "blog:anthropic_blog"
    assert result["source_type"] == "blog"
    assert result["source_label"] == "Anthropic News"
    assert result["source_url"] == "https://www.anthropic.com/news"


def test_fetch_blog_html_scrape_accepts_nextjs_page(tmp_path):
    """Real Next.js pages with _next/static should be accepted, not rejected."""
    blog = {
        "name": "Anthropic News",
        "strategy": "html_scrape",
        "url": "https://www.anthropic.com/news",
        "scrape_parser": "anthropic_news",
    }
    html = (
        '<html><head><script src="/_next/static/chunks/main.js"></script></head>'
        '<body><h2>News</h2><a href="/news/some-article">Some Article</a></body></html>'
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = html
        result = _fetch_blog("anthropic_blog", blog)
    assert "Some Article" in result["content"]


# ── Source provenance schema ─────────────────────────────────────────────────

def _assert_provenance_fields(result: dict):
    """Assert that a gather result has all required provenance fields."""
    assert "source_key" in result
    assert "source_type" in result
    assert "source_label" in result
    assert "source_url" in result
    assert "content" in result
    # source_key format: type:identifier
    assert ":" in result["source_key"]
    assert result["source_type"] == result["source_key"].split(":")[0]


def test_fetch_blog_provenance_schema():
    """Blog gather records have correct provenance fields."""
    blog = {"name": "Simon Willison", "feed_url": "https://simonwillison.net/atom/everything/"}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = RSS_SAMPLE
        result = _fetch_blog("simon_willison", blog)
    _assert_provenance_fields(result)
    assert result["source_key"] == "blog:simon_willison"
    assert result["source_type"] == "blog"
    assert result["source_label"] == "Simon Willison"
    assert result["source_url"] == "https://simonwillison.net/atom/everything/"


def test_fetch_blog_research_source_type():
    """Research sources get source_type='research'."""
    blog = {"name": "ArXiv Papers", "feed_url": "https://arxiv.org/feed"}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = RSS_SAMPLE
        result = _fetch_blog("arxiv", blog, source_type="research")
    _assert_provenance_fields(result)
    assert result["source_key"] == "research:arxiv"
    assert result["source_type"] == "research"
