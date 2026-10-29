#!/usr/bin/env bash
# Launch the Zalo bridge as a long-lived service, with the SAME token the
# adapter will use.
#
# WHY THIS WRAPPER EXISTS, AND WHY IT IS LOAD-BEARING
#
# The bridge reads BRIDGE_TOKEN and PORT. The profile's .env sets
# ZALO_BRIDGE_TOKEN and ZALO_BRIDGE_PORT -- different names. The adapter does
# that mapping in Python when it spawns the bridge itself; a systemd unit with
# `EnvironmentFile=.env` does not, so the bridge would fall back to
# `deriveToken()` (sha256 of session.json) while the adapter authenticates
# with the .env value.
#
# That mismatch is the failure DEPLOY.md calls "silent, total, and the one
# that bit us": /health answers, /send works, cron delivery works, and every
# inbound message is dropped with 401 behind the scenes. The bot looks alive
# and simply stops hearing anyone.
#
# So the mapping lives here, once, next to the reason.
set -uo pipefail

HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes}"
PROFILE_NAME="${PROFILE_NAME:-zalo-bot}"
PROFILE_HOME="${PROFILE_HOME:-$HERMES_ROOT/profiles/$PROFILE_NAME}"
BRIDGE_DIR="${BRIDGE_DIR:-$HERMES_ROOT/hermes-agent/plugins/platforms/zalo/bridge}"
ENV_FILE="$PROFILE_HOME/.env"

[ -d "$BRIDGE_DIR" ] || { echo "no bridge at $BRIDGE_DIR" >&2; exit 2; }
[ -f "$ENV_FILE" ]   || { echo "no .env at $ENV_FILE" >&2; exit 2; }

# Read values without sourcing: .env is not guaranteed to be shell-safe, and a
# stray backtick or $( in a secret would execute.
envval() {
    sed -n "s/^$1=//p" "$ENV_FILE" | head -1 | tr -d '\r' | sed 's/^"//; s/"$//'
}

TOKEN="$(envval ZALO_BRIDGE_TOKEN)"
PORT_V="$(envval ZALO_BRIDGE_PORT)"

if [ -z "$TOKEN" ]; then
    # Not fatal: the bridge then derives the token from session.json, and so
    # does the adapter. They still agree. Say so, because the health check's
    # token-drift probe cannot run without an explicit token.
    echo "WARN: ZALO_BRIDGE_TOKEN not set; bridge and adapter will both derive it" >&2
fi

export HERMES_HOME="$PROFILE_HOME"
export ZALO_SESSION_FILE="$PROFILE_HOME/zalo/session.json"
export BRIDGE_TOKEN="$TOKEN"
export PORT="${PORT_V:-8647}"

cd "$BRIDGE_DIR" || exit 2
exec node index.js
