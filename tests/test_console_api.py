"""Tests for digest_pipeline.console_api — Flask test client, no network."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from digest_pipeline.console_api import create_app


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _write_lines(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


@pytest.fixture
def digest_dir(tmp_path):
    """Create a temp digests directory with one digest and sample data."""
    ai = tmp_path / "ai"
    ai.mkdir()

    # Config
    config = {
        "digest": {"name": "AI Daily Digest", "emoji": "⚡", "max_articles": 20},
        "categories": [{"id": "dev-tools", "label": "Dev Tools", "emoji": "🛠️",
                         "description": "Developer tools"}],
        "sources": {
            "twitter": {"accounts": ["simonw", "karpathy"], "lookback": "36h"},
            "blogs": {
                "anthropic_blog": {
                    "name": "Anthropic News",
                    "feed_url": "https://anthropic.com/feed",
                },
            },
            "newsletters": {
                "imap": {"host": "imap.gmail.com", "email": "test@test.com"},
                "sources": {
                    "rundown_ai": {"name": "The Rundown AI", "from": "a@b.com"},
                },
            },
            "github_trending": {"name": "GitHub Trending", "url": "https://github.com/trending"},
        },
        "podcast": {"enabled": True, "name": "The AI Roundup"},
        "delivery": {},
    }
    _write_json(ai / "config.json", config)

    # Work dir with run.json
    work = ai / "work" / "2026-03-24"
    work.mkdir(parents=True)
    _write_json(work / "run.json", {
        "digest": "AI Daily Digest",
        "date": "2026-03-24",
        "started_at": "2026-03-24T12:00:00Z",
        "completed_at": "2026-03-24T12:05:00Z",
        "status": "success",
        "error": None,
        "duration_s": 300.0,
        "stages": [
            {
                "stage": "Gather",
                "timestamp": "2026-03-24T12:00:05Z",
                "type": "info",
                "detail": "Sources: 4/5",
                "duration_s": 5.0,
                "tokens": {"input": 0, "output": 0, "cost": 0.0},
            },
            {
                "stage": "Extract (Batch 1/1)",
                "timestamp": "2026-03-24T12:00:30Z",
                "type": "info",
                "detail": "Articles found: 10\n  Anthropic News: 500 chars -> 3 articles",
                "duration_s": 25.0,
                "tokens": {"input": 5000, "output": 1500, "cost": 0.01},
            },
            {
                "stage": "Cluster",
                "timestamp": "2026-03-24T12:01:00Z",
                "type": "info",
                "detail": "Clusters: 8 (1 with duplicates)",
                "duration_s": 0.5,
                "tokens": {"input": 1000, "output": 0, "cost": 0.001},
            },
        ],
        "totals": {"input_tokens": 6000, "output_tokens": 1500, "cost": 0.05},
    })

    # Stage artifacts
    _write_json(work / "extracted.json", [
        {"title": f"Article {i}", "category": "dev-tools", "source_key": "blog:anthropic_blog"}
        for i in range(10)
    ])
    _write_json(work / "clusters.json", [[f"Article {i}"] for i in range(8)])
    _write_json(work / "deduped.json", [
        {"title": f"Article {i}", "category": "dev-tools"} for i in range(8)
    ])
    (work / "final-digest.md").write_text("# Digest\nContent here")

    # Raw source files
    (work / "raw-twitter-simonw.txt").write_text("tweet content")
    (work / "raw-twitter-karpathy.txt").write_text("")  # empty
    (work / "raw-blog-anthropic_blog.txt").write_text("blog post content here")

    # Source state
    _write_json(ai / ".source_state.json", {
        "twitter:simonw": {"seen_ids": ["url1", "url2"], "last_updated": "2026-03-24"},
        "blog:anthropic_blog": {"seen_ids": ["url3"], "last_updated": "2026-03-23"},
    })

    # Subscribers
    _write_json(ai / "subscribers.json", {
        "subscribers": [
            {"email": "a@b.com", "token": "tok1", "subscribed_at": "2026-03-20T00:00:00Z", "source": "cli"},
            {"email": "c@d.com", "token": "tok2", "subscribed_at": "2026-03-21T00:00:00Z", "source": "landing-page"},
        ]
    })

    # Send history
    _write_lines(ai / "send_history.jsonl", [
        {"timestamp": "2026-03-24T12:10:00Z", "digest_date": "2026-03-24", "email": "a@b.com", "status": "sent", "method": "resend"},
        {"timestamp": "2026-03-24T12:10:01Z", "digest_date": "2026-03-24", "email": "c@d.com", "status": "sent", "method": "resend"},
        {"timestamp": "2026-03-23T12:10:00Z", "digest_date": "2026-03-23", "email": "a@b.com", "status": "sent", "method": "resend"},
        {"timestamp": "2026-03-23T12:10:01Z", "digest_date": "2026-03-23", "email": "c@d.com", "status": "failed", "method": "resend", "error": "bounce"},
    ])

    # Podcast files
    podcasts = ai / "podcasts"
    podcasts.mkdir()
    (podcasts / "2026-03-24.mp3").write_bytes(b"\x00" * 1000)
    (podcasts / "2026-03-24.txt").write_text("Script content")
    (podcasts / "2026-03-23.mp3").write_bytes(b"\x00" * 2000)

    # Podcast RSS
    (ai / "podcast.xml").write_text("""<?xml version='1.0' encoding='utf-8'?>
<rss version="2.0">
  <channel>
    <title>Test Podcast</title>
    <item><title>Ep 2026-03-24</title><pubDate>Tue, 24 Mar 2026</pubDate>
      <enclosure url="https://example.com/2026-03-24.mp3" length="1000" type="audio/mpeg" /></item>
    <item><title>Ep 2026-03-23</title><pubDate>Mon, 23 Mar 2026</pubDate>
      <enclosure url="https://example.com/2026-03-23.mp3" length="2000" type="audio/mpeg" /></item>
  </channel>
</rss>""")

    return tmp_path


@pytest.fixture
def client(digest_dir):
    """Create a Flask test client."""
    def mock_load(path):
        cfg = json.loads(Path(path).read_text())
        cfg["_data_root"] = Path(path).parent
        cfg["_pipeline_dir"] = Path(__file__).parent.parent / "digest_pipeline"
        return cfg

    with patch("digest_pipeline.console_api.load_config", side_effect=mock_load):
        app = create_app(digests_dir=str(digest_dir))

    app.testing = True
    return app.test_client()


# ── Health ──────────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert "ai" in data["digests"]


# ── Digests list ────────────────────────────────────────────────────────────

def test_list_digests(client):
    resp = client.get("/api/digests")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    d = data[0]
    assert d["slug"] == "ai"
    assert d["name"] == "AI Daily Digest"
    assert d["emoji"] == "⚡"
    assert d["source_count"] == 5  # 2 twitter + 1 blog + 1 newsletter + 1 github
    assert d["subscriber_count"] == 2
    assert d["last_run"]["date"] == "2026-03-24"
    assert d["last_run"]["status"] == "success"
    assert d["last_run"]["article_count"] == 10
    assert d["last_run"]["cost"] == 0.05


# ── Runs ────────────────────────────────────────────────────────────────────

def test_list_runs(client):
    resp = client.get("/api/digests/ai/runs")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["date"] == "2026-03-24"
    assert data[0]["article_count"] == 10


def test_list_runs_unknown_digest(client):
    resp = client.get("/api/digests/nope/runs")
    assert resp.status_code == 404


def test_get_run(client):
    resp = client.get("/api/digests/ai/runs/2026-03-24")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["digest"] == "AI Daily Digest"
    assert data["status"] == "success"
    assert len(data["stages"]) == 3


def test_get_run_not_found(client):
    resp = client.get("/api/digests/ai/runs/2026-01-01")
    assert resp.status_code == 404


def test_get_run_invalid_date(client):
    resp = client.get("/api/digests/ai/runs/bad-date")
    assert resp.status_code == 400


# ── Funnel ──────────────────────────────────────────────────────────────────

def test_get_funnel(client):
    resp = client.get("/api/digests/ai/runs/2026-03-24/funnel")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["extracted"] == 10
    assert data["clustered"] == 8
    assert data["deduped"] == 8
    assert data["prioritized"] == 8  # no prioritized.json -> falls back to deduped
    assert data["formatted"] == 8  # same as prioritized when final-digest.md exists


# ── Run sources ─────────────────────────────────────────────────────────────

def test_get_run_sources(client):
    resp = client.get("/api/digests/ai/runs/2026-03-24/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 3  # simonw, karpathy, anthropic_blog
    keys = {s["source_key"] for s in data}
    assert "twitter:simonw" in keys
    assert "twitter:karpathy" in keys

    # karpathy was empty
    karpathy = next(s for s in data if s["source_key"] == "twitter:karpathy")
    assert karpathy["has_content"] is False

    simonw = next(s for s in data if s["source_key"] == "twitter:simonw")
    assert simonw["has_content"] is True


# ── Sources ─────────────────────────────────────────────────────────────────

def test_list_sources(client):
    resp = client.get("/api/digests/ai/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 5  # 2 twitter + 1 blog + 1 newsletter + 1 github

    simonw = next(s for s in data if s["source_key"] == "twitter:simonw")
    assert simonw["last_updated"] == "2026-03-24"
    assert simonw["seen_count"] == 2

    anthropic = next(s for s in data if s["source_key"] == "blog:anthropic_blog")
    assert anthropic["last_updated"] == "2026-03-23"
    assert anthropic["seen_count"] == 1

    # Newsletter should have no state
    rundown = next(s for s in data if s["source_key"] == "newsletter:rundown_ai")
    assert rundown["last_updated"] is None
    assert rundown["seen_count"] == 0


# ── Delivery ────────────────────────────────────────────────────────────────

def test_get_delivery(client):
    resp = client.get("/api/digests/ai/delivery")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["subscriber_count"] == 2
    assert len(data["recent_sends"]) == 2  # 2 dates

    mar24 = next(s for s in data["recent_sends"] if s["date"] == "2026-03-24")
    assert mar24["total"] == 2
    assert mar24["sent"] == 2
    assert mar24["failed"] == 0

    mar23 = next(s for s in data["recent_sends"] if s["date"] == "2026-03-23")
    assert mar23["sent"] == 1
    assert mar23["failed"] == 1


def test_get_delivery_sends(client):
    resp = client.get("/api/digests/ai/delivery/sends")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 4
    # Newest first
    assert data[0]["digest_date"] == "2026-03-23"  # last line in file = newest after reverse


# ── Podcast ─────────────────────────────────────────────────────────────────

def test_get_podcast(client):
    resp = client.get("/api/digests/ai/podcast")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["name"] == "The AI Roundup"
    assert len(data["episodes"]) == 2
    assert data["rss_item_count"] == 2

    ep1 = data["episodes"][0]  # newest first
    assert ep1["date"] == "2026-03-24"
    assert ep1["has_mp3"] is True
    assert ep1["mp3_size"] == 1000
    assert ep1["has_script"] is True

    ep2 = data["episodes"][1]
    assert ep2["date"] == "2026-03-23"
    assert ep2["has_script"] is False  # no .txt for this date


# ── Config ──────────────────────────────────────────────────────────────────

def test_get_config(client):
    resp = client.get("/api/digests/ai/config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["digest"]["name"] == "AI Daily Digest"
    # Internal keys should be stripped
    assert "_data_root" not in data
    assert "_pipeline_dir" not in data


# ── Error handling ──────────────────────────────────────────────────────────

def test_404(client):
    resp = client.get("/api/nonexistent")
    assert resp.status_code == 404


def test_index_serves_frontend(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # Should serve either the built frontend HTML or the "not built" JSON
    if resp.content_type.startswith("text/html"):
        assert b"<!DOCTYPE" in resp.data or b"<html" in resp.data
    else:
        data = resp.get_json()
        assert "not built" in data["message"]
