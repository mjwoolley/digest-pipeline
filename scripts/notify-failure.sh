#!/usr/bin/env bash
# Called by systemd OnFailure= when digest-subscriptions crashes.
# Sends a Telegram alert.

set -euo pipefail

SECRETS_FILE="/home/clawdbot/digest-pipeline/secrets.env"
eval "$(grep -E '^TELEGRAM_(BOT_TOKEN|CHAT_ID)=' "$SECRETS_FILE")"

MSG="🔴 digest-subscriptions.service crashed

The subscription API service has failed and systemd is restarting it.
Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')

Check: sudo journalctl -u digest-subscriptions --no-pager -n 30"

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": $(printf '%s' "$MSG" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}" \
    > /dev/null 2>&1
