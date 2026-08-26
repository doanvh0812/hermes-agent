#!/usr/bin/env bash
# Zalo bridge health check — cron: 7,17,27,37,47,57 * * * *
#
# Catches the failure modes that surface nowhere else:
#   1. bridge dead / port gone           -> /health unreachable
#   2. bridge up but not logged in       -> cookie expired
#   3. bridge token drift                -> /events returns 401
#   4. gateway process down              -> nothing polls the bridge
#
# None of these produce a visible symptom: the gateway keeps reporting
# "connected" and /send keeps working while inbound messages stop arriving.
#
# Alerts go to Telegram, deliberately: it is a different transport from the
# one being monitored, so a dead Zalo cannot swallow the notification about
# itself. Falls back to `logger` when Telegram is not configured.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PORT="${ZALO_BRIDGE_PORT:-8647}"
ENV_FILE="$HERMES_HOME/.env"
STATE="$HERMES_HOME/zalo/.health-state"

# Gateway unit to check. Empty disables that check (e.g. non-systemd hosts).
GATEWAY_UNIT="${ZALO_GATEWAY_UNIT-hermes-gateway-zalo}"

envval() {
    grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"''
}

TOK="$(envval ZALO_BRIDGE_TOKEN)"
TG_TOKEN="${ZALO_ALERT_TG_TOKEN:-$(envval TELEGRAM_BOT_TOKEN)}"
TG_CHAT="${ZALO_ALERT_TG_CHAT:-$(envval TELEGRAM_HOME_CHANNEL)}"

notify() {
    local text="$1"
    logger -t zalo-bridge "$text"
    if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
        curl -sf --max-time 10 \
            "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
            -d "chat_id=${TG_CHAT}" \
            -d "text=${text}" \
            -d "disable_notification=false" >/dev/null 2>&1 \
            || logger -t zalo-bridge "WARN: Telegram alert delivery failed"
    fi
}

fail() {
    local reason="$1" detail="${2:-}"
    local prev=""
    [ -f "$STATE" ] && prev="$(cat "$STATE" 2>/dev/null)"
    echo "fail:$reason" > "$STATE"
    # Alert on the transition into failure only. A bridge that stays broken
    # must not page every ten minutes — that trains people to ignore it.
    [ "$prev" = "fail:$reason" ] && exit 1
    notify "🔴 Zalo bot DOWN — ${reason}${detail:+
${detail}}

Kiểm tra: systemctl --user status ${GATEWAY_UNIT:-hermes-gateway-zalo}"
    exit 1
}

recovered() {
    if [ -f "$STATE" ] && grep -q '^fail:' "$STATE" 2>/dev/null; then
        notify "🟢 Zalo bot đã hoạt động trở lại."
    fi
    echo ok > "$STATE"
}

# ---- 1. gateway process ---------------------------------------------------
if [ -n "$GATEWAY_UNIT" ] && command -v systemctl >/dev/null 2>&1; then
    if ! systemctl --user is-active --quiet "$GATEWAY_UNIT" 2>/dev/null; then
        fail "gateway không chạy" "Không có tiến trình nào đọc tin nhắn từ bridge."
    fi
fi

# ---- 2. bridge reachable --------------------------------------------------
HEALTH="$(curl -sf --max-time 5 "http://127.0.0.1:$PORT/health" 2>/dev/null)" \
    || fail "bridge không phản hồi (cổng $PORT)" \
            "Tiến trình node đã chết hoặc cổng bị chiếm."

# ---- 3. logged in ---------------------------------------------------------
grep -q '"ready":true' <<<"$HEALTH" \
    || fail "bridge chưa đăng nhập" \
            "Cookie Zalo hết hạn. Chạy: node index.js --qr-web"

# ---- 4. token matches -----------------------------------------------------
if [ -n "$TOK" ]; then
    EV="$(curl -s --max-time 5 -H "X-Bridge-Token: $TOK" \
          "http://127.0.0.1:$PORT/events?since=0" 2>/dev/null)"
    grep -q 'bad bridge token' <<<"$EV" \
        && fail "token không khớp" \
                "Adapter không đọc được tin nhắn. Đặt ZALO_BRIDGE_TOKEN giống nhau rồi khởi động lại cả hai."
else
    logger -t zalo-bridge "WARN: ZALO_BRIDGE_TOKEN chưa đặt; không phát hiện được token drift"
fi

recovered
