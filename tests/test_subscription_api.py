"""Tests for digest_pipeline.subscription_api — Flask test client, no network."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from digest_pipeline.subscription_api import create_app


@pytest.fixture
def app_client(tmp_path):
    """Create a Flask test client with a temp config."""
    # Write a minimal config file
    config = {
        "digest": {"name": "Test Digest"},
        "subscriptions": {
            "public_base_url": "https://example.com",
            "cors_origins": ["https://allowed.com"],
            "port": 5100,
        },
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    # Patch load_config to inject _data_root
    from digest_pipeline.config import load_config as _real_load
    def mock_load(path):
        cfg = json.loads(Path(path).read_text())
        cfg["_data_root"] = tmp_path
        return cfg

    with patch("digest_pipeline.subscription_api.load_config", side_effect=mock_load):
        app = create_app(str(config_path))

    app.testing = True
    return app.test_client(), tmp_path


# ── Subscribe ───────────────────────────────────────────────────────────────

def test_subscribe_success(app_client):
    client, data_root = app_client
    resp = client.post("/api/subscribe",
                       json={"email": "test@example.com"},
                       content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True

    # Verify subscriber was created
    subs = json.loads((data_root / "subscribers.json").read_text())
    assert len(subs["subscribers"]) == 1
    assert subs["subscribers"][0]["email"] == "test@example.com"
    assert subs["subscribers"][0]["source"] == "landing-page"


def test_subscribe_invalid_email(app_client):
    client, _ = app_client
    resp = client.post("/api/subscribe",
                       json={"email": "bademail"},
                       content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_subscribe_duplicate(app_client):
    client, _ = app_client
    client.post("/api/subscribe", json={"email": "a@b.com"},
                content_type="application/json")
    resp = client.post("/api/subscribe", json={"email": "a@b.com"},
                       content_type="application/json")
    # Should return 200 (doesn't reveal duplicate)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_subscribe_honeypot(app_client):
    client, data_root = app_client
    resp = client.post("/api/subscribe",
                       json={"email": "bot@spam.com", "website": "http://spam.com"},
                       content_type="application/json")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True  # silently accepted

    # But no subscriber should have been created
    assert not (data_root / "subscribers.json").exists()


# ── Unsubscribe GET ─────────────────────────────────────────────────────────

def test_unsubscribe_page_valid(app_client):
    client, data_root = app_client
    # First subscribe
    client.post("/api/subscribe", json={"email": "a@b.com"},
                content_type="application/json")
    subs = json.loads((data_root / "subscribers.json").read_text())
    token = subs["subscribers"][0]["token"]

    resp = client.get(f"/unsubscribe?token={token}")
    assert resp.status_code == 200
    assert b"Confirm Unsubscribe" in resp.data


def test_unsubscribe_page_invalid_token(app_client):
    client, _ = app_client
    resp = client.get("/unsubscribe?token=bogus")
    assert resp.status_code == 400
    assert b"No Longer Valid" in resp.data


def test_unsubscribe_page_no_token(app_client):
    client, _ = app_client
    resp = client.get("/unsubscribe")
    assert resp.status_code == 400


# ── Unsubscribe POST ───────────────────────────────────────────────────────

def test_unsubscribe_post(app_client):
    client, data_root = app_client
    client.post("/api/subscribe", json={"email": "a@b.com"},
                content_type="application/json")
    subs = json.loads((data_root / "subscribers.json").read_text())
    token = subs["subscribers"][0]["token"]

    resp = client.post("/api/unsubscribe", json={"token": token},
                       content_type="application/json")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    # Verify subscriber was removed
    subs = json.loads((data_root / "subscribers.json").read_text())
    assert len(subs["subscribers"]) == 0


def test_unsubscribe_post_bogus_token(app_client):
    client, _ = app_client
    resp = client.post("/api/unsubscribe", json={"token": "bogus"},
                       content_type="application/json")
    # Always 200 to prevent enumeration
    assert resp.status_code == 200


# ── CORS ────────────────────────────────────────────────────────────────────

def test_cors_allowed_origin(app_client):
    client, _ = app_client
    resp = client.post("/api/subscribe",
                       json={"email": "a@b.com"},
                       content_type="application/json",
                       headers={"Origin": "https://allowed.com"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://allowed.com"


def test_cors_disallowed_origin(app_client):
    client, _ = app_client
    resp = client.post("/api/subscribe",
                       json={"email": "a@b.com"},
                       content_type="application/json",
                       headers={"Origin": "https://evil.com"})
    assert "Access-Control-Allow-Origin" not in resp.headers


# ── Rate limiting ───────────────────────────────────────────────────────────

def test_rate_limit(app_client):
    client, _ = app_client
    for i in range(5):
        resp = client.post("/api/subscribe",
                           json={"email": f"user{i}@example.com"},
                           content_type="application/json")
        assert resp.status_code == 200

    # 6th request should be rate-limited
    resp = client.post("/api/subscribe",
                       json={"email": "overflow@example.com"},
                       content_type="application/json")
    assert resp.status_code == 429


# ── Health ──────────────────────────────────────────────────────────────────

def test_health(app_client):
    client, _ = app_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# ── Oversized body ──────────────────────────────────────────────────────────

def test_oversized_body(app_client):
    client, _ = app_client
    resp = client.post("/api/subscribe",
                       data="x" * 9000,
                       content_type="application/json")
    assert resp.status_code == 413


# ── Error handlers ──────────────────────────────────────────────────────────

def test_404(app_client):
    client, _ = app_client
    resp = client.get("/nonexistent")
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_405(app_client):
    client, _ = app_client
    resp = client.delete("/api/subscribe")
    assert resp.status_code == 405
