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
# itself. Falls back to `logger` when Telegram is not configured -- which is
# NOT monitoring (gap #9 in DEPLOY.md): a log file on the broken box is read
# only by someone who already knows something is wrong. Configure it with
# `deploy/setup-telegram.py --token <from @BotFather>`.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PORT="${ZALO_BRIDGE_PORT:-8647}"
ENV_FILE="$HERMES_HOME/.env"
STATE="$HERMES_HOME/zalo/.health-state"

# Gateway unit to check. Empty disables that check (e.g. non-systemd hosts).
GATEWAY_UNIT="${ZALO_GATEWAY_UNIT-hermes-gateway-zalo}"
# System units by default. `systemctl --user` needs loginctl enable-linger or
# the units stop with the last login session and never return at boot, so the
# migrated deployment installs SYSTEM units. Set ZALO_SYSTEMCTL_SCOPE=--user
# for a host that really does run them per-user.
SYSTEMCTL_SCOPE="${ZALO_SYSTEMCTL_SCOPE-}"

# \r is deliberate: a profile .env that came from Windows has CRLF, and
# without this the token carries a trailing carriage return into the HTTP
# header. Measured against the live bridge: that is NOT a token mismatch --
# node's HTTP parser rejects the malformed request with 400 and an EMPTY
# body, well before the token is ever compared. So the symptom is "no reply",
# which is why step 4 below checks the status code and not just the
# "bad bridge token" string.
envval() {
    grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r"'"'"''
}

TOK="$(envval ZALO_BRIDGE_TOKEN)"
TG_TOKEN="${ZALO_ALERT_TG_TOKEN:-$(envval TELEGRAM_BOT_TOKEN)}"
TG_CHAT="${ZALO_ALERT_TG_CHAT:-$(envval TELEGRAM_HOME_CHANNEL)}"

notify() {
    # Name the host: during the Windows -> Linux cutover both boxes can be
    # alive and alerting into the SAME chat, and an alert you cannot attribute
    # to a machine is an alert you cannot act on.
    local text="$1
Host: $(hostname 2>/dev/null || echo unknown)"
    logger -t zalo-bridge "$text"
    if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
        # --data-urlencode for the text, NOT -d: curl sends -d data verbatim,
        # so a reason containing & or + would be truncated or silently altered
        # by form decoding on the way in. These strings are multi-line and
        # carry remediation commands, so they get encoded properly.
        # ZALO_ALERT_TG_API redirects at a local listener so this path can be
        # exercised without a live token. It had never run once before
        # 2026-11-17 -- no token was ever configured -- so "it is written" was
        # not evidence it worked. Same override exists in zalo-health.ps1.
        curl -sf --max-time 10 \
            "${ZALO_ALERT_TG_API:-https://api.telegram.org}/bot${TG_TOKEN}/sendMessage" \
            -d "chat_id=${TG_CHAT}" \
            --data-urlencode "text=${text}" \
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

Kiểm tra: systemctl ${SYSTEMCTL_SCOPE} status ${GATEWAY_UNIT:-hermes-gateway-zalo}"
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
    # shellcheck disable=SC2086  # SYSTEMCTL_SCOPE is one optional flag or empty
    if ! systemctl $SYSTEMCTL_SCOPE is-active --quiet "$GATEWAY_UNIT" 2>/dev/null; then
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
    # Measured status codes on the live bridge: 200 = accepted,
    # 401 = {"error":"bad bridge token"}, 400 = the request itself was
    # malformed (a stray \r in the token does this) and never reached the
    # comparison. Only the 401 carries a body, so a string match alone lets
    # the 400 through and the monitor reports ok while the adapter reads
    # nothing.
    EV="$(curl -s --max-time 5 -H "X-Bridge-Token: $TOK" \
          -w $'\n%{http_code}' "http://127.0.0.1:$PORT/events?since=0" 2>/dev/null)"
    CODE="${EV##*$'\n'}"
    BODY="${EV%$'\n'*}"
    case "$CODE" in
        200)
            grep -q 'bad bridge token' <<<"$BODY" \
                && fail "token không khớp" \
                        "Adapter không đọc được tin nhắn. Đặt ZALO_BRIDGE_TOKEN giống nhau rồi khởi động lại cả hai."
            ;;
        401)
            fail "token không khớp" \
                 "Adapter không đọc được tin nhắn. Đặt ZALO_BRIDGE_TOKEN giống nhau rồi khởi động lại cả hai."
            ;;
        *)
            fail "/events trả HTTP ${CODE:-rỗng}" \
                 "Request tới bridge bị từ chối trước cả bước kiểm token. Kiểm tra ZALO_BRIDGE_TOKEN có ký tự lạ (\\r, khoảng trắng) không."
            ;;
    esac
else
    logger -t zalo-bridge "WARN: ZALO_BRIDGE_TOKEN chưa đặt; không phát hiện được token drift"
fi

# ---- 5. Odoo credential ---------------------------------------------------
# Odoo API keys are revoked by a password change and can be deleted from the
# user's profile — neither of which reaches this host. The bot keeps running
# and simply tells everyone it cannot look anything up, which reads like a
# data problem rather than an expired key. Verified: this exact failure went
# unnoticed until a user asked why answers had stopped.
ODOO_URL="$(envval ODOO_URL)"
ODOO_DB="$(envval ODOO_DB)"
ODOO_USER="$(envval ODOO_USERNAME)"
ODOO_KEY="$(envval ODOO_API_KEY)"
# Profile configs carry the credential inline; fall back to the profile file.
# HERMES_HOME is usually ALREADY the profile home (.../profiles/zalo-bot), in
# which case appending profiles/zalo-bot again silently finds nothing and the
# Odoo check is skipped without saying so.
if [ -n "${ZALO_PROFILE_CONFIG:-}" ]; then
    PROFILE_CFG="$ZALO_PROFILE_CONFIG"
elif [ -f "$HERMES_HOME/config.yaml" ]; then
    PROFILE_CFG="$HERMES_HOME/config.yaml"
else
    PROFILE_CFG="$HERMES_HOME/profiles/zalo-bot/config.yaml"
fi
if [ -z "$ODOO_URL" ] && [ -f "$PROFILE_CFG" ]; then
    # Split on the FIRST colon only — the value itself contains one
    # ("https://..."), and a greedy match silently produces "//host".
    cfgval() {
        grep -m1 "^ *$1:" "$PROFILE_CFG" \
            | sed "s/^ *$1: *//; s/^[\"']//; s/[\"']$//"
    }
    ODOO_URL="$(cfgval ODOO_URL)"
    ODOO_DB="$(cfgval ODOO_DB)"
    ODOO_USER="$(cfgval ODOO_USERNAME)"
    ODOO_KEY="$(cfgval ODOO_PASSWORD)"
fi

if [ -n "$ODOO_URL" ] && [ -n "$ODOO_KEY" ]; then
    AUTH_XML="<?xml version=\"1.0\"?><methodCall><methodName>authenticate</methodName><params><param><value><string>${ODOO_DB}</string></value></param><param><value><string>${ODOO_USER}</string></value></param><param><value><string>${ODOO_KEY}</string></value></param><param><value><struct/></value></param></params></methodCall>"
    RESP="$(curl -s --max-time 15 -H 'Content-Type: text/xml' \
            -d "$AUTH_XML" "${ODOO_URL}/xmlrpc/2/common" 2>/dev/null)"
    if [ -z "$RESP" ]; then
        fail "Odoo không phản hồi" "Không kết nối được ${ODOO_URL}."
    fi
    # A successful authenticate returns the uid as <int>; a failed one returns
    # boolean false.
    if ! grep -qE '<int>[0-9]+</int>' <<<"$RESP"; then
        fail "Odoo API key không dùng được" \
             "Bot vẫn trả lời nhưng không tra được dữ liệu.
Tạo key mới: My Profile → Account Security → New API Key"
    fi
fi

# ---- 6. gateway có thật sự nắm được tool MCP ------------------------------
# Mục 5 chứng minh CREDENTIAL Odoo còn dùng được. Nó không chứng minh gateway
# DÙNG được: tool MCP nằm trong tiến trình của chính gateway, discovery chỉ
# chạy MỘT lần lúc khởi động, và một lần connect thất bại thì không bao giờ
# được thử lại -- chỉ /reload-mcp hoặc restart mới lặp lại nó.
#
# Đo ngày 2026-11-17: bốn ngày không có một tool Odoo nào, trong khi unit báo
# active, bridge báo ready, mục 5 pass, và file state ghi "ok" mỗi 10 phút.
# check-mcp-tools.py ghi đầy đủ cơ chế.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_CHECK="$HERE/check-mcp-tools.py"
# HERMES_HOME là profile home, nên log của gateway nằm ngay cạnh.
AGENT_LOG="$HERMES_HOME/logs/agent.log"
# venv của agent khi script nằm đúng chỗ trong cây agent; nếu không thì bất kỳ
# python3 nào cũng được -- checker chỉ dùng stdlib.
MCP_PY=""
for cand in "${ZALO_HEALTH_PYTHON:-}" \
            "$HERE/../../../../venv/bin/python" \
            "$(command -v python3 2>/dev/null)" \
            "$(command -v python 2>/dev/null)"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then MCP_PY="$cand"; break; fi
done
if [ -f "$MCP_CHECK" ] && [ -n "$MCP_PY" ] && [ -f "$AGENT_LOG" ]; then
    MCP_OUT="$("$MCP_PY" "$MCP_CHECK" --log "$AGENT_LOG" 2>/dev/null)"
    case "$?" in
        0) ;;
        1) fail "gateway thiếu tool MCP" "$MCP_OUT

Bot vẫn trả lời nhưng không tra được dữ liệu. Khởi động lại gateway:
systemctl ${SYSTEMCTL_SCOPE} restart ${GATEWAY_UNIT:-hermes-gateway-zalo}" ;;
        # Không xác định được (log đã rotate, không đọc được) thì chỉ WARN: một
        # monitor báo động vì chính helper của nó không đọc được file sẽ dạy
        # người ta bỏ qua nó.
        *) logger -t zalo-bridge "WARN: không xác định được trạng thái MCP: $MCP_OUT" ;;
    esac
else
    logger -t zalo-bridge "WARN: bỏ qua kiểm tra MCP (thiếu checker, python hoặc agent.log)"
fi

recovered
