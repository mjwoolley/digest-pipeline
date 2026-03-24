"""Flask subscription API for digest email subscriptions.

Multi-digest aware: discovers all digests under a directory and routes
requests to the correct subscriber list via the X-Digest header (injected
by Caddy) or a ?digest= query parameter.

Run via: digest-pipeline --serve [--digests-dir DIR] [--port PORT]
"""
import json
import logging
import time
from pathlib import Path

from flask import Flask, Response, g, jsonify, request

from .config import load_config
from .subscribers import (
    add_subscriber,
    load_subscribers,
    remove_subscriber,
    validate_email,
)

logger = logging.getLogger("digest")

MAX_REQUEST_BODY = 8 * 1024  # 8KB


def create_app(digests_dir: str = None, config_path: str = None) -> Flask:
    """Flask app factory for the subscription API.

    Provide either digests_dir (multi-digest) or config_path (single-digest).
    """
    # Build digest registry: {slug: {"config": dict, "data_root": Path}}
    digests = {}
    if digests_dir:
        digests_path = Path(digests_dir)
        for cfg_file in sorted(digests_path.glob("*/config.json")):
            slug = cfg_file.parent.name
            try:
                config = load_config(str(cfg_file))
                digests[slug] = {"config": config, "data_root": config["_data_root"]}
                logger.info(f"[API] Loaded digest: {slug}")
            except Exception as e:
                logger.warning(f"[API] Failed to load {cfg_file}: {e}")
    elif config_path:
        config = load_config(config_path)
        slug = config["_data_root"].name
        digests[slug] = {"config": config, "data_root": config["_data_root"]}

    if not digests:
        raise RuntimeError("No digest configs found")

    # Aggregate CORS origins from all digests
    cors_origins = set()
    for d in digests.values():
        for origin in d["config"].get("subscriptions", {}).get("cors_origins", []):
            cors_origins.add(origin)

    app = Flask(__name__)

    # In-memory rate limiter: {ip: [timestamps]}
    _rate_limits: dict[str, list[float]] = {}
    RATE_LIMIT = 5  # requests per minute
    RATE_WINDOW = 60  # seconds

    def _check_rate_limit(ip: str) -> bool:
        """Return True if request is allowed, False if rate-limited."""
        now = time.time()
        timestamps = _rate_limits.get(ip, [])
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

    def _resolve_digest():
        """Resolve digest from X-Digest header or ?digest= param.

        Returns (slug, digest_info) or (None, None).
        """
        slug = request.headers.get("X-Digest") or request.args.get("digest")
        if slug and slug in digests:
            return slug, digests[slug]
        # Fallback: if only one digest loaded, use it
        if len(digests) == 1:
            slug = next(iter(digests))
            return slug, digests[slug]
        return None, None

    def _find_digest_by_token(token: str):
        """Search all digests for a subscriber token. Returns (slug, digest_info) or (None, None)."""
        for slug, info in digests.items():
            subscribers = load_subscribers(info["data_root"])
            if any(s["token"] == token for s in subscribers):
                return slug, info
        return None, None

    @app.before_request
    def _limit_request_size():
        if request.content_length and request.content_length > MAX_REQUEST_BODY:
            return jsonify({"ok": False, "message": "Request too large"}), 413

    @app.before_request
    def _resolve_digest_context():
        """Set g.digest_slug, g.data_root, g.config from request context."""
        # Skip digest resolution for health check
        if request.path == "/health":
            return
        slug, info = _resolve_digest()
        if info:
            g.digest_slug = slug
            g.data_root = info["data_root"]
            g.config = info["config"]
        else:
            g.digest_slug = None
            g.data_root = None
            g.config = None

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

        if not g.data_root:
            return jsonify({"ok": False, "message": "Unknown digest"}), 400

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

        added, msg = add_subscriber(g.data_root, email, source="landing-page")
        if added:
            return jsonify({"ok": True, "message": "Subscribed! You'll receive the next digest."})
        else:
            # Already subscribed — don't reveal this to prevent enumeration
            return jsonify({"ok": True, "message": "Thanks! Check your inbox to confirm."})

    @app.route("/unsubscribe", methods=["GET", "POST"])
    def unsubscribe_page():
        token = request.args.get("token", "")
        if not token:
            return _render_unsubscribe_page(valid=False), 400

        # Resolve digest: try header/param first, then search all digests by token
        data_root = g.data_root
        digest_name = "this digest"
        if not data_root:
            slug, info = _find_digest_by_token(token)
            if info:
                data_root = info["data_root"]
                digest_name = info["config"].get("digest", {}).get("name", "this digest")
        else:
            digest_name = g.config.get("digest", {}).get("name", "this digest")

        if not data_root:
            return _render_unsubscribe_page(valid=False), 400

        # POST = Gmail one-click List-Unsubscribe; unsubscribe immediately
        if request.method == "POST":
            remove_subscriber(data_root, token=token, source="list-unsubscribe")
            return "", 200

        subscribers = load_subscribers(data_root)
        found = any(s["token"] == token for s in subscribers)

        if not found:
            return _render_unsubscribe_page(valid=False), 400

        return _render_unsubscribe_page(valid=True, token=token, digest_name=digest_name)

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
            data_root = g.data_root
            if not data_root:
                # Fallback: search all digests for this token
                _, info = _find_digest_by_token(token)
                if info:
                    data_root = info["data_root"]
            if data_root:
                remove_subscriber(data_root, token=token, source="landing-page")

        # Always return 200 to prevent enumeration
        return jsonify({"ok": True, "message": "You have been unsubscribed."})

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "digests": list(digests.keys())})

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


def _render_unsubscribe_page(valid: bool, token: str = "",
                              digest_name: str = "this digest") -> str:
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
<p>Click the button below to unsubscribe from {digest_name}.</p>
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
