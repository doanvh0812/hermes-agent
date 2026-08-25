#!/usr/bin/env bash
# Zalo bridge health check — cron: 7,17,27,37,47,57 * * * *
#
# Catches BOTH silent failure modes:
#   1. bridge dead / not logged in  -> /health not ready
#   2. bridge token drift           -> /events returns 401
# Neither surfaces anywhere else; the gateway keeps reporting "connected".
set -uo pipefail

PORT="${ZALO_BRIDGE_PORT:-8647}"
ENV_FILE="${HERMES_HOME:-$HOME/.hermes}/.env"
STATE="${HERMES_HOME:-$HOME/.hermes}/zalo/.health-state"

TOK="$(grep -m1 '^ZALO_BRIDGE_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"

fail() {
    # Only alert on transition into failure; a broken bridge must not
    # produce one message every 10 minutes forever.
    local prev=""
    [ -f "$STATE" ] && prev="$(cat "$STATE")"
    echo "fail:$1" > "$STATE"
    [ "$prev" = "fail:$1" ] && exit 1
    logger -t zalo-bridge "ALERT: $1"
    # TODO: route somewhere the operator actually sees:
    #   curl -s -d "Zalo bridge: $1" ntfy.sh/<your-topic>
    exit 1
}

HEALTH="$(curl -sf --max-time 5 "http://127.0.0.1:$PORT/health" 2>/dev/null)" \
    || fail "bridge unreachable on port $PORT"

grep -q '"ready":true' <<<"$HEALTH" \
    || fail "bridge up but not logged in (re-run: node index.js --qr-login)"

if [ -n "$TOK" ]; then
    EV="$(curl -s --max-time 5 -H "X-Bridge-Token: $TOK" \
          "http://127.0.0.1:$PORT/events?since=0" 2>/dev/null)"
    grep -q 'bad bridge token' <<<"$EV" \
        && fail "token drift — adapter cannot read events (set ZALO_BRIDGE_TOKEN)"
else
    logger -t zalo-bridge "WARN: ZALO_BRIDGE_TOKEN unset; token drift will go undetected"
fi

# Recovered?
if [ -f "$STATE" ] && grep -q '^fail:' "$STATE" 2>/dev/null; then
    logger -t zalo-bridge "RECOVERED: bridge healthy again"
fi
echo ok > "$STATE"
