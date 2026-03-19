#!/usr/bin/env bash
# Health check for the subscription API.
# Hits the public HTTPS endpoint and alerts via Telegram on failure.
# Intended to run via cron every 5 minutes.

set -euo pipefail

HEALTH_URL="https://ai-digest.duckdns.org/health"
SECRETS_FILE="/home/clawdbot/digest-pipeline/secrets.env"

# Load Telegram credentials from secrets.env
eval "$(grep -E '^TELEGRAM_(BOT_TOKEN|CHAT_ID)=' "$SECRETS_FILE")"

# Prevent alert storms: skip if we alerted in the last 30 minutes
ALERT_LOCKFILE="/tmp/digest-health-alert.lock"
if [[ -f "$ALERT_LOCKFILE" ]]; then
    lock_age=$(( $(date +%s) - $(stat -c %Y "$ALERT_LOCKFILE") ))
    if (( lock_age < 1800 )); then
        exit 0
    fi
fi

# Check health endpoint (5s timeout)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$HEALTH_URL" 2>/dev/null || echo "000")

if [[ "$HTTP_CODE" == "200" ]]; then
    # Healthy — clear lockfile if it exists
    rm -f "$ALERT_LOCKFILE"
    exit 0
fi

# Unhealthy — send Telegram alert
touch "$ALERT_LOCKFILE"

MSG="⚠️ Subscription API Health Check Failed

URL: $HEALTH_URL
HTTP status: $HTTP_CODE
Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')

Check: sudo systemctl status digest-subscriptions"

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": $(printf '%s' "$MSG" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}" \
    > /dev/null 2>&1

exit 1
