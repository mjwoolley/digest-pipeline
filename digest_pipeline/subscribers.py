"""Subscriber management for digest email delivery.

Stores subscribers in a JSON file at {data_root}/subscribers.json.
Each subscriber has an email, a unique unsubscribe token, subscription
timestamp, and source indicator.
"""
import json
import logging
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("digest")


def _subscribers_path(data_root: Path) -> Path:
    return data_root / "subscribers.json"


def _generate_token() -> str:
    """Generate a cryptographically random unsubscribe token."""
    return secrets.token_urlsafe(24)


def validate_email(email: str) -> bool:
    """Validate an email address. Returns True if valid."""
    if not email or not isinstance(email, str):
        return False
    email = email.strip()
    if len(email) < 5 or len(email) > 254:
        return False
    if re.search(r'\s', email):
        return False
    if '@' not in email:
        return False
    local, _, domain = email.rpartition('@')
    if not local or not domain:
        return False
    if '.' not in domain:
        return False
    return True


def load_subscribers(data_root: Path) -> list[dict]:
    """Load subscriber list. Returns list of subscriber dicts."""
    path = _subscribers_path(data_root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("subscribers", [])
    except (json.JSONDecodeError, KeyError):
        logger.warning(f"[SUBSCRIBERS] Corrupt subscribers file: {path}")
        return []


def save_subscribers(data_root: Path, subscribers: list[dict]) -> None:
    """Save subscriber list to disk atomically."""
    path = _subscribers_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump({"subscribers": subscribers}, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def log_subscription_event(data_root: Path, event_type: str, email: str,
                           token: str, source: str) -> None:
    """Append a subscription event to the JSONL log."""
    path = Path(data_root) / "subscription_events.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "email": email,
        "token": token,
        "source": source,
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_send(data_root: Path, digest_date: str, email: str, status: str,
             error: str = None, method: str = None) -> None:
    """Append a send-history entry to the JSONL log."""
    path = Path(data_root) / "send_history.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "digest_date": digest_date,
        "email": email,
        "status": status,
    }
    if error:
        entry["error"] = error
    if method:
        entry["method"] = method
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def add_subscriber(data_root: Path, email: str,
                   source: str = "cli") -> tuple[bool, str]:
    """Add a subscriber. Returns (added: bool, message: str)."""
    email = email.strip().lower()
    if not validate_email(email):
        return False, f"Invalid email address: {email}"

    subscribers = load_subscribers(data_root)

    for sub in subscribers:
        if sub["email"] == email:
            return False, f"Already subscribed: {email}"

    token = _generate_token()
    subscribers.append({
        "email": email,
        "token": token,
        "subscribed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    })
    save_subscribers(data_root, subscribers)
    log_subscription_event(data_root, "subscribe", email, token, source)
    return True, f"Subscribed: {email} (token: {token})"


def remove_subscriber(data_root: Path, email: str = None,
                      token: str = None,
                      source: str = "cli") -> tuple[bool, str]:
    """Remove a subscriber by email or token. Returns (removed, message)."""
    subscribers = load_subscribers(data_root)
    original_count = len(subscribers)

    removed_sub = None
    if email:
        email = email.strip().lower()
        for s in subscribers:
            if s["email"] == email:
                removed_sub = s
                break
        subscribers = [s for s in subscribers if s["email"] != email]
    elif token:
        for s in subscribers:
            if s["token"] == token:
                removed_sub = s
                break
        subscribers = [s for s in subscribers if s["token"] != token]
    else:
        return False, "Must provide email or token"

    if len(subscribers) == original_count:
        return False, "Subscriber not found"

    save_subscribers(data_root, subscribers)
    if removed_sub:
        log_subscription_event(
            data_root, "unsubscribe",
            removed_sub["email"], removed_sub["token"], source,
        )
    return True, "Unsubscribed successfully"


def list_subscribers(data_root: Path) -> list[dict]:
    """Return all subscribers."""
    return load_subscribers(data_root)


def get_subscriber_emails(data_root: Path) -> list[str]:
    """Return just the email addresses of all subscribers."""
    return [s["email"] for s in load_subscribers(data_root)]


def unsubscribe_url(base_url: str, token: str, digest: str = None) -> str:
    """Build an unsubscribe URL.

    If base_url is provided, returns an HTTP unsubscribe link.
    Otherwise falls back to mailto: for backward compatibility.
    When digest is provided, appends &digest={slug} for multi-digest routing.
    """
    if base_url:
        url = f"{base_url.rstrip('/')}/unsubscribe?token={token}"
        if digest:
            url += f"&digest={digest}"
        return url
    return f"mailto:mjw_openclaw@agentmail.to?subject=unsubscribe&body=token:{token}"
