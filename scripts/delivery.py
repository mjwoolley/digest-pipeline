"""Pluggable delivery for digest pipeline: email + notifications + audio."""
import json
import logging
import os
import smtplib
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger("digest")

MAX_MSG_LEN = 4096


# ── Email Delivery ──────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, config: dict):
    """Send email via configured method. Raises on failure."""
    email_cfg = config.get("delivery", {}).get("email", {})
    method = email_cfg.get("method", "smtp")

    if method == "gog":
        _send_email_gog(subject, html_body, email_cfg)
    elif method == "smtp":
        _send_email_smtp(subject, html_body, email_cfg)
    elif method == "agentmail":
        _send_email_agentmail(subject, html_body, email_cfg)
    else:
        raise ValueError(f"Unknown email method: {method}")


def _send_email_gog(subject: str, html_body: str, email_cfg: dict):
    """Send email via GOG CLI (Gmail)."""
    gog_cfg = email_cfg.get("gog", {})
    account = gog_cfg.get("account", email_cfg.get("to", ""))
    pw_env = gog_cfg.get("keyring_password_env", "GOG_KEYRING_PASSWORD")
    password = os.environ.get(pw_env, "")

    env = {**os.environ, pw_env: password}
    result = subprocess.run(
        ["gog", "gmail", "send",
         "--to", email_cfg.get("to", account),
         "--subject", subject,
         "--body", "See HTML version of this email.",
         "--body-html", html_body,
         "--account", account,
         "--no-input"],
        capture_output=True, text=True, timeout=30, env=env
    )
    if result.returncode != 0:
        raise RuntimeError(f"GOG email failed: {result.stderr}")
    logger.info("[DELIVER] Email sent via GOG")


def _send_email_smtp(subject: str, html_body: str, email_cfg: dict):
    """Send email via SMTP."""
    smtp_cfg = email_cfg.get("smtp", {})
    host = smtp_cfg.get("host", "smtp.gmail.com")
    port = smtp_cfg.get("port", 587)
    user = smtp_cfg.get("user", "")
    pw_env = smtp_cfg.get("password_env", "SMTP_PASSWORD")
    password = os.environ.get(pw_env, "")
    from_addr = smtp_cfg.get("from", user)
    to_addr = email_cfg.get("to", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText("See HTML version of this email.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    logger.info("[DELIVER] Email sent via SMTP")


def _send_email_agentmail(subject: str, html_body: str, email_cfg: dict):
    """Send email via AgentMail API.

    Requires an AgentMail inbox. Config:
      agentmail.api_key_env: env var name for API key
      agentmail.inbox_id: sender inbox (e.g. "ai-digest@agentmail.to")
    """
    am_cfg = email_cfg.get("agentmail", {})
    api_key_env = am_cfg.get("api_key_env", "AGENTMAIL_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    inbox_id = am_cfg.get("inbox_id", "")
    to_addr = email_cfg.get("to", "")

    if not api_key:
        raise RuntimeError(f"AgentMail API key not set (env: {api_key_env})")
    if not inbox_id:
        raise RuntimeError("AgentMail inbox_id not configured")

    # URL-encode the inbox_id (contains @)
    encoded_inbox = urllib.parse.quote(inbox_id, safe="")
    url = f"https://api.agentmail.to/v0/inboxes/{encoded_inbox}/messages/send"

    payload = json.dumps({
        "to": to_addr,
        "subject": subject,
        "text": "See HTML version of this email.",
        "html": html_body,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            logger.info(f"[DELIVER] Email sent via AgentMail (message_id: {result.get('message_id', 'unknown')})")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"AgentMail API error {e.code}: {err_body[:500]}") from e


# ── Notification Delivery ───────────────────────────────────────────────────

def send_notification(message: str, config: dict) -> bool:
    """Send notification via configured method. Returns success."""
    notify_cfg = config.get("delivery", {}).get("notify", {})
    method = notify_cfg.get("method", "none")

    if method == "telegram":
        return _send_telegram(message, notify_cfg)
    elif method == "slack":
        return _send_slack(message, notify_cfg)
    elif method == "none":
        return True
    else:
        logger.warning(f"[DELIVER] Unknown notify method: {method}")
        return False


def send_progress(stage: str, detail: str, config: dict,
                  usage: dict = None) -> bool:
    """Send stage completion update with token usage."""
    digest_name = config.get("digest", {}).get("name", "Digest")
    lines = [f"\U0001f4ca {digest_name} \u2014 Stage Complete",
             f"Stage: {stage}", detail]
    if usage:
        inp = usage.get("input_tokens", usage.get("prompt_tokens", 0))
        out = usage.get("output_tokens", usage.get("completion_tokens", 0))
        cost = usage.get("cost", 0)
        lines.append(f"Tokens: {_fmt_tokens(inp)} in / {_fmt_tokens(out)} out (${cost:.2f})")
    if "duration" in (usage or {}):
        lines.append(f"Duration: {usage['duration']:.1f}s")
    return send_notification("\n".join(lines), config)


def send_alert(stage: str, error: str, config: dict) -> bool:
    """Send failure alert with stage + error details."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    digest_name = config.get("digest", {}).get("name", "Digest")
    msg = (
        f"\u26a0\ufe0f {digest_name} Pipeline Failed\n\n"
        f"Stage: {stage}\n"
        f"Error: {error}\n"
        f"Time: {now}"
    )
    return send_notification(msg, config)


def send_audio(file_path: str, caption: str, config: dict) -> bool:
    """Send audio file via Telegram (if configured). Returns success."""
    notify_cfg = config.get("delivery", {}).get("notify", {})
    method = notify_cfg.get("method", "none")

    if method == "telegram":
        tg_cfg = notify_cfg.get("telegram", {})
        bot_token = _resolve_env(tg_cfg, "bot_token_env", "TELEGRAM_BOT_TOKEN")
        chat_id = _resolve_env(tg_cfg, "chat_id_env", "TELEGRAM_CHAT_ID")
        return _send_telegram_audio(file_path, caption, bot_token, chat_id)
    else:
        logger.info("[DELIVER] Audio delivery skipped (no Telegram configured)")
        return True


# ── Telegram ────────────────────────────────────────────────────────────────

def _send_telegram(message: str, notify_cfg: dict) -> bool:
    """Send message via Telegram Bot API. Splits long messages."""
    tg_cfg = notify_cfg.get("telegram", {})
    bot_token = _resolve_env(tg_cfg, "bot_token_env", "TELEGRAM_BOT_TOKEN")
    chat_id = _resolve_env(tg_cfg, "chat_id_env", "TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.warning("[TELEGRAM] Missing bot_token or chat_id")
        return False

    chunks = _split_message(message)
    for chunk in chunks:
        if not _post_telegram_message(chunk, bot_token, chat_id):
            return False
    return True


def _post_telegram_message(text: str, bot_token: str, chat_id: str) -> bool:
    """POST a single message to Telegram."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"[TELEGRAM] Send failed: {e}")
        return False


def _send_telegram_audio(file_path: str, caption: str,
                         bot_token: str, chat_id: str) -> bool:
    """Send an audio file via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendAudio"
    boundary = "----FormBoundary7MA4YWxkTrZu0gW"

    audio_path = Path(file_path)
    audio_data = audio_path.read_bytes()
    filename = audio_path.name

    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'.encode())
    body.extend(f"{chat_id}\r\n".encode())
    if caption:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="caption"\r\n\r\n'.encode())
        body.extend(f"{caption}\r\n".encode())
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="audio"; filename="{filename}"\r\n'.encode())
    body.extend(f"Content-Type: audio/mpeg\r\n\r\n".encode())
    body.extend(audio_data)
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(url, data=bytes(body),
                                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"[TELEGRAM] Audio send failed: {e}")
        return False


# ── Slack ───────────────────────────────────────────────────────────────────

def _send_slack(message: str, notify_cfg: dict) -> bool:
    """Send message via Slack webhook."""
    slack_cfg = notify_cfg.get("slack", {})
    webhook_url_env = slack_cfg.get("webhook_url_env", "SLACK_WEBHOOK_URL")
    webhook_url = os.environ.get(webhook_url_env, "")

    if not webhook_url:
        logger.warning("[SLACK] No webhook URL configured")
        return False

    payload = json.dumps({"text": message}).encode()
    req = urllib.request.Request(webhook_url, data=payload,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"[SLACK] Send failed: {e}")
        return False


# ── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_env(cfg: dict, env_key: str, default_env: str) -> str:
    """Resolve a value from an env var name stored in config."""
    env_name = cfg.get(env_key, default_env)
    return os.environ.get(env_name, "")


def _split_message(text: str) -> list[str]:
    """Split text into chunks of MAX_MSG_LEN, breaking on newlines."""
    if len(text) <= MAX_MSG_LEN:
        return [text]
    chunks = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut == -1:
            cut = MAX_MSG_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    elif n < 10000:
        return f"{n/1000:.1f}K"
    else:
        return f"{n//1000}K"
