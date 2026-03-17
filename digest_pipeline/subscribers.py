"""Subscriber management for digest email delivery.

Stores subscribers in a JSON file at {data_root}/subscribers.json.
Each subscriber has an email and a unique unsubscribe token.
"""
import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("digest")


def _subscribers_path(data_root: Path) -> Path:
    return data_root / "subscribers.json"


def _generate_token(email: str) -> str:
    """Generate a stable unsubscribe token for an email address."""
    # Use email + a secret salt (or fallback) for token generation
    salt = os.environ.get("UNSUBSCRIBE_SALT", "digest-pipeline-unsub")
    return hashlib.sha256(f"{salt}:{email}".encode()).hexdigest()[:16]


def load_subscribers(data_root: Path) -> list[dict]:
    """Load subscriber list. Returns list of {email, token} dicts."""
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
    """Save subscriber list to disk."""
    path = _subscribers_path(data_root)
    path.write_text(json.dumps({"subscribers": subscribers}, indent=2) + "\n")


def add_subscriber(data_root: Path, email: str) -> tuple[bool, str]:
    """Add a subscriber. Returns (added: bool, message: str)."""
    email = email.strip().lower()
    subscribers = load_subscribers(data_root)

    for sub in subscribers:
        if sub["email"] == email:
            return False, f"Already subscribed: {email}"

    token = _generate_token(email)
    subscribers.append({"email": email, "token": token})
    save_subscribers(data_root, subscribers)
    return True, f"Subscribed: {email} (token: {token})"


def remove_subscriber(data_root: Path, email: str = None,
                      token: str = None) -> tuple[bool, str]:
    """Remove a subscriber by email or token. Returns (removed, message)."""
    subscribers = load_subscribers(data_root)
    original_count = len(subscribers)

    if email:
        email = email.strip().lower()
        subscribers = [s for s in subscribers if s["email"] != email]
    elif token:
        subscribers = [s for s in subscribers if s["token"] != token]
    else:
        return False, "Must provide email or token"

    if len(subscribers) == original_count:
        return False, "Subscriber not found"

    save_subscribers(data_root, subscribers)
    return True, "Unsubscribed successfully"


def list_subscribers(data_root: Path) -> list[dict]:
    """Return all subscribers."""
    return load_subscribers(data_root)


def get_subscriber_emails(data_root: Path) -> list[str]:
    """Return just the email addresses of all subscribers."""
    return [s["email"] for s in load_subscribers(data_root)]


def unsubscribe_url(base_url: str, token: str) -> str:
    """Build a mailto: unsubscribe link.

    Since this is a static-site pipeline with no web backend, unsubscribe
    uses a mailto: link to the AgentMail inbox. The CLI --process-subscriptions
    command can then handle these requests.
    """
    return f"mailto:mjw_openclaw@agentmail.to?subject=unsubscribe&body=token:{token}"
