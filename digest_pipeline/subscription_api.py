"""Flask subscription API for digest email subscriptions.

Provides HTTP endpoints for subscribing/unsubscribing to the digest.
Run via: digest-pipeline <config> --serve [--port PORT]
"""
import json
import logging
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request

from .config import load_config
from .subscribers import (
    add_subscriber,
    load_subscribers,
    remove_subscriber,
    validate_email,
)

logger = logging.getLogger("digest")

MAX_REQUEST_BODY = 8 * 1024  # 8KB


def create_app(config_path: str) -> Flask:
    """Flask app factory for the subscription API."""
    config = load_config(config_path)
    data_root = config["_data_root"]
    sub_cfg = config.get("subscriptions", {})
    cors_origins = sub_cfg.get("cors_origins", [])

    app = Flask(__name__)

    # In-memory rate limiter: {ip: [timestamps]}
    _rate_limits: dict[str, list[float]] = {}
    RATE_LIMIT = 5  # requests per minute
    RATE_WINDOW = 60  # seconds

    def _check_rate_limit(ip: str) -> bool:
        """Return True if request is allowed, False if rate-limited."""
        now = time.time()
        timestamps = _rate_limits.get(ip, [])
        # Prune old entries
        timestamps = [t for t in timestamps if now - t < RATE_WINDOW]
        if len(timestamps) >= RATE_LIMIT:
            _rate_limits[ip] = timestamps
            return False
        timestamps.append(now)
        _rate_limits[ip] = timestamps
        return True

    def _cleanup_rate_limits():
        """Remove expired entries to prevent memory growth."""
        now = time.time()
        expired = [ip for ip, ts in _rate_limits.items()
                   if all(now - t >= RATE_WINDOW for t in ts)]
        for ip in expired:
            del _rate_limits[ip]

    @app.before_request
    def _limit_request_size():
        if request.content_length and request.content_length > MAX_REQUEST_BODY:
            return jsonify({"ok": False, "message": "Request too large"}), 413

    @app.after_request
    def _add_cors(response: Response) -> Response:
        origin = request.headers.get("Origin", "")
        if origin in cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    @app.route("/api/subscribe", methods=["POST", "OPTIONS"])
    def subscribe():
        if request.method == "OPTIONS":
            return "", 204

        ip = request.remote_addr or "unknown"
        if not _check_rate_limit(ip):
            _cleanup_rate_limits()
            return jsonify({"ok": False, "message": "Too many requests. Try again later."}), 429

        try:
            data = request.get_json(silent=True) or {}
        except Exception:
            return jsonify({"ok": False, "message": "Invalid request"}), 400

        email = (data.get("email") or "").strip().lower()

        # Honeypot: if a hidden field is filled, silently reject
        if data.get("website") or data.get("url"):
            return jsonify({"ok": True, "message": "Thanks! Check your inbox to confirm."})

        if not validate_email(email):
            return jsonify({"ok": False, "message": "Please enter a valid email address."}), 400

        added, msg = add_subscriber(data_root, email, source="landing-page")
        if added:
            return jsonify({"ok": True, "message": "Subscribed! You'll receive the next digest."})
        else:
            # Already subscribed — don't reveal this to prevent enumeration
            return jsonify({"ok": True, "message": "Thanks! Check your inbox to confirm."})

    @app.route("/unsubscribe", methods=["GET"])
    def unsubscribe_page():
        token = request.args.get("token", "")
        if not token:
            return _render_unsubscribe_page(valid=False), 400

        subscribers = load_subscribers(data_root)
        found = any(s["token"] == token for s in subscribers)

        if not found:
            return _render_unsubscribe_page(valid=False), 400

        return _render_unsubscribe_page(valid=True, token=token)

    @app.route("/api/unsubscribe", methods=["POST", "OPTIONS"])
    def unsubscribe():
        if request.method == "OPTIONS":
            return "", 204

        try:
            data = request.get_json(silent=True) or {}
        except Exception:
            data = {}

        token = (data.get("token") or "").strip()
        if token:
            remove_subscriber(data_root, token=token, source="landing-page")

        # Always return 200 to prevent enumeration
        return jsonify({"ok": True, "message": "You have been unsubscribed."})

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"ok": False, "message": "Not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"ok": False, "message": "Method not allowed"}), 405

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"ok": False, "message": "Internal server error"}), 500

    return app


def _render_unsubscribe_page(valid: bool, token: str = "") -> str:
    """Render a simple HTML unsubscribe confirmation page."""
    if not valid:
        return """<!DOCTYPE html>
<html><head><title>Unsubscribe</title>
<style>body{font-family:sans-serif;max-width:480px;margin:60px auto;text-align:center;color:#333;}</style>
</head><body>
<h2>Link No Longer Valid</h2>
<p>This unsubscribe link is no longer valid or has already been used.</p>
</body></html>"""

    return f"""<!DOCTYPE html>
<html><head><title>Unsubscribe</title>
<style>body{{font-family:sans-serif;max-width:480px;margin:60px auto;text-align:center;color:#333;}}
button{{background:#ef4444;color:#fff;border:none;padding:12px 24px;border-radius:8px;font-size:1rem;cursor:pointer;}}
button:hover{{opacity:0.9;}}
#result{{margin-top:1rem;}}</style>
</head><body>
<h2>Unsubscribe</h2>
<p>Click the button below to unsubscribe from the AI Daily Digest.</p>
<button id="unsub-btn" onclick="doUnsub()">Confirm Unsubscribe</button>
<div id="result"></div>
<script>
async function doUnsub() {{
  const btn = document.getElementById('unsub-btn');
  const result = document.getElementById('result');
  btn.disabled = true;
  btn.textContent = 'Processing...';
  try {{
    const resp = await fetch('/api/unsubscribe', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{token: '{token}'}})
    }});
    const data = await resp.json();
    result.innerHTML = '<p style="color:#22c55e;">You have been unsubscribed.</p>';
    btn.style.display = 'none';
  }} catch(e) {{
    result.innerHTML = '<p style="color:#ef4444;">Something went wrong. Please try again.</p>';
    btn.disabled = false;
    btn.textContent = 'Confirm Unsubscribe';
  }}
}}
</script>
</body></html>"""
