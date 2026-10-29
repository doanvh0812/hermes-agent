#!/usr/bin/env bash
# Install and start the systemd units. Run with sudo, AFTER install-linux.sh
# and restore-bundle.sh have run as the bot's user.
#
#   sudo HERMES_USER=hermes ./install-services.sh
#
# System units, not `systemctl --user`, on purpose: user units need
# `loginctl enable-linger` or they stop with the last session and never come
# back at boot. That is a quiet way to lose the bot to a reboot, which is the
# failure this whole migration is meant to end.
set -uo pipefail

say()  { printf '\n\033[36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mFATAL\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"

HERMES_USER="${HERMES_USER:-${SUDO_USER:-}}"
[ -n "$HERMES_USER" ] || die "set HERMES_USER=<the bot's unix user>"
id "$HERMES_USER" >/dev/null 2>&1 || die "no such user: $HERMES_USER"

HERMES_GROUP="${HERMES_GROUP:-$(id -gn "$HERMES_USER")}"
USER_HOME="$(getent passwd "$HERMES_USER" | cut -d: -f6)"
[ -d "$USER_HOME" ] || die "no home directory for $HERMES_USER"

HERMES_ROOT="${HERMES_ROOT:-$USER_HOME/.hermes}"
PROFILE_NAME="${PROFILE_NAME:-zalo-bot}"
PROFILE_HOME="${PROFILE_HOME:-$HERMES_ROOT/profiles/$PROFILE_NAME}"
AGENT_DIR="${AGENT_DIR:-$HERMES_ROOT/hermes-agent}"
MIGRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system

[ -d "$PROFILE_HOME" ] || die "no profile at $PROFILE_HOME -- run restore-bundle.sh first"
[ -x "$AGENT_DIR/venv/bin/python" ] || die "no venv at $AGENT_DIR -- run install-linux.sh first"

NODE_BIN="$(command -v node || true)"
[ -n "$NODE_BIN" ] || die "node not on root's PATH; install it system-wide"
NODE_BIN_DIR="$(dirname "$NODE_BIN")"
SYSTEMCTL="$(command -v systemctl)" || die "systemctl not found"

say "target"
printf '   user      %s:%s  (%s)\n' "$HERMES_USER" "$HERMES_GROUP" "$USER_HOME"
printf '   agent     %s\n   profile   %s\n   node      %s\n' "$AGENT_DIR" "$PROFILE_HOME" "$NODE_BIN_DIR"

# The units append to files in logs/; systemd creates the file but not the
# directory, and a missing directory fails the unit with a bare 238/EXEC.
install -d -o "$HERMES_USER" -g "$HERMES_GROUP" -m 750 "$PROFILE_HOME/logs"
ok "logs/ present and owned by $HERMES_USER"

chmod +x "$MIGRATE_DIR/run-bridge.sh" \
         "$AGENT_DIR/plugins/platforms/zalo/deploy/zalo-health.sh" 2>/dev/null || true

# -------------------------------------------------------------- 1. the units
say "units -> $UNIT_DIR"
render() {
    sed -e "s|__USER__|$HERMES_USER|g" \
        -e "s|__GROUP__|$HERMES_GROUP|g" \
        -e "s|__HOME__|$USER_HOME|g" \
        -e "s|__HERMES_ROOT__|$HERMES_ROOT|g" \
        -e "s|__PROFILE_HOME__|$PROFILE_HOME|g" \
        -e "s|__AGENT_DIR__|$AGENT_DIR|g" \
        -e "s|__MIGRATE_DIR__|$MIGRATE_DIR|g" \
        -e "s|__NODE_BIN_DIR__|$NODE_BIN_DIR|g" \
        "$1"
}
for u in hermes-zalo-bridge.service hermes-gateway-zalo.service \
         hermes-zalo-admin.service hermes-zalo-health.service \
         hermes-zalo-health.timer; do
    src="$MIGRATE_DIR/systemd/$u"
    [ -f "$src" ] || die "template missing: $src"
    render "$src" > "$UNIT_DIR/$u"
    chmod 644 "$UNIT_DIR/$u"
    # An unsubstituted placeholder produces a unit that starts a path called
    # literally "__AGENT_DIR__/..." and fails with a message about a missing
    # file, which reads like a broken install rather than a broken template.
    if grep -q '__[A-Z_]*__' "$UNIT_DIR/$u"; then
        grep -n '__[A-Z_]*__' "$UNIT_DIR/$u" >&2
        die "$u still has unsubstituted placeholders"
    fi
    ok "$u"
done

# --------------------------------------------------- 2. the panel's one verb
# The panel changes the model and then has to restart the gateway for it to
# take effect. It runs unprivileged, so grant exactly that one command --
# not systemctl, not the unit's full lifecycle.
say "sudoers drop-in for the panel"
SUDO_FILE=/etc/sudoers.d/hermes-zalo-panel
cat > "$SUDO_FILE" <<EOF
# Lets the Zalo admin panel restart the gateway after a model change.
# Deliberately one verb on one unit: a compromise of the panel buys a bot
# restart, nothing more.
$HERMES_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart hermes-gateway-zalo
EOF
chmod 440 "$SUDO_FILE"
if visudo -c -f "$SUDO_FILE" >/dev/null 2>&1; then
    ok "$SUDO_FILE validated"
else
    rm -f "$SUDO_FILE"
    die "sudoers drop-in failed validation and was removed"
fi

# ---------------------------------------------------------- 3. log rotation
say "log rotation"
LOGROTATE_SRC="$AGENT_DIR/plugins/platforms/zalo/deploy/logrotate.conf"
if [ -f "$LOGROTATE_SRC" ] && [ -d /etc/logrotate.d ]; then
    render "$LOGROTATE_SRC" > /etc/logrotate.d/hermes-zalo
    chmod 644 /etc/logrotate.d/hermes-zalo
    if grep -q '__[A-Z_]*__' /etc/logrotate.d/hermes-zalo; then
        die "logrotate template still has unsubstituted placeholders"
    fi
    if logrotate -d /etc/logrotate.d/hermes-zalo >/dev/null 2>&1; then
        ok "/etc/logrotate.d/hermes-zalo validated"
    else
        warn "logrotate config installed but failed a dry run -- check the paths inside it"
    fi
else
    warn "no logrotate.conf or no /etc/logrotate.d; logs will grow unbounded"
fi

# ------------------------------------------------------------- 4. start them
say "enable + start"
systemctl daemon-reload
# Bridge first: the gateway attaching to a live bridge is the path production
# has actually been running, and it avoids an immediate cookie login.
systemctl enable --now hermes-zalo-bridge.service || warn "bridge failed to start"
for i in 1 2 3 4 5 6 7 8 9 10; do
    curl -sf --max-time 2 http://127.0.0.1:8647/health >/dev/null 2>&1 && break
    sleep 2
done
if curl -sf --max-time 3 http://127.0.0.1:8647/health 2>/dev/null | grep -q '"ready":true'; then
    ok "bridge is logged in"
else
    warn "bridge is up but NOT logged in -- the Zalo cookie did not survive the move."
    warn "Expect to re-scan the QR; see MIGRATE.md 'When Zalo rejects the session'."
fi

systemctl enable --now hermes-gateway-zalo.service || warn "gateway failed to start"
systemctl enable --now hermes-zalo-admin.service   || warn "admin panel failed to start"
systemctl enable --now hermes-zalo-health.timer    || warn "health timer failed to start"

say "status"
for u in hermes-zalo-bridge hermes-gateway-zalo hermes-zalo-admin; do
    printf '   %-24s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || echo unknown)"
done
systemctl list-timers --no-pager hermes-zalo-health.timer 2>/dev/null | head -3

cat <<EOF

Services installed. Now run the behaviour gate as $HERMES_USER:

   sudo -u $HERMES_USER $MIGRATE_DIR/verify-linux.sh

"Active: running" is not the same as working -- every outage this deployment
has had looked healthy from its normal indicator. Run the gate.
EOF
