#!/usr/bin/env bash
# Restore the bundled profile state into the Linux profile home, and rewrite
# the handful of values that named the Windows box.
#
# Four things here are not a plain file copy, and each one has bitten this
# deployment before:
#
#   1. .env arrives with CRLF. The bash health check reads it with
#      `grep | cut`, so a trailing \r rides along inside the value and the
#      bridge token sent as an HTTP header never matches. Normalised here.
#   2. config.yaml carries three absolute Windows paths (the MCP interpreter
#      and two policy files). Rewritten surgically, line by line -- NOT via a
#      YAML round-trip, which would delete the Vietnamese comments that
#      document why compression and the display gates are set the way they are.
#   3. config.yaml holds the Odoo API key inline (profile configs do not
#      interpolate ${VAR}), so it is as sensitive as .env. chmod 600 both.
#   4. The bot refuses to answer, rather than failing loudly, when the model
#      credential did not come across. Checked before the services start.
#
# Run as the bot's user, after install-linux.sh.
set -uo pipefail

BUNDLE="${1:-}"
HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes}"
PROFILE_NAME="${PROFILE_NAME:-zalo-bot}"
PROFILE_HOME="${PROFILE_HOME:-$HERMES_ROOT/profiles/$PROFILE_NAME}"
AGENT_DIR="${AGENT_DIR:-$HERMES_ROOT/hermes-agent}"
WORK="${WORK:-$HOME/zalo-migrate}"
UNPACK="$WORK/unpacked"

say()  { printf '\n\033[36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mFATAL\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && die "run as the bot's user, not root"

if [ -n "$BUNDLE" ]; then
    [ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"
    rm -rf "$UNPACK" && mkdir -p "$UNPACK"
    tar -xzf "$BUNDLE" -C "$UNPACK" || die "tar extract failed"
fi
SRC="$UNPACK/profile"
[ -d "$SRC" ] || die "no unpacked profile at $SRC -- pass the bundle path"

VENV_PY="$AGENT_DIR/venv/bin/python"
[ -x "$VENV_PY" ] || die "no venv at $VENV_PY -- run install-linux.sh first"

# ------------------------------------------------------------- 1. the copy
say "profile -> $PROFILE_HOME"
if [ -e "$PROFILE_HOME" ]; then
    BK="$PROFILE_HOME.before-restore-$(date +%Y%m%d-%H%M%S)"
    mv "$PROFILE_HOME" "$BK" || die "cannot move the existing profile aside"
    warn "existing profile moved to $BK"
fi
mkdir -p "$(dirname "$PROFILE_HOME")"
cp -a "$SRC" "$PROFILE_HOME" || die "copy failed"
# A profile home holds credentials; nothing outside the owner needs to read it.
chmod 700 "$PROFILE_HOME"
ok "$(find "$PROFILE_HOME" -type f | wc -l) files"

# ------------------------------------------------------- 2. line endings
say "line endings"
for f in .env; do
    p="$PROFILE_HOME/$f"
    [ -f "$p" ] || continue
    if grep -qU $'\r' "$p" 2>/dev/null; then
        sed -i 's/\r$//' "$p"
        ok "$f normalised CRLF -> LF"
    else
        ok "$f already LF"
    fi
done

# --------------------------------------------------- 3. the Windows paths
say "rewriting host-specific paths in config.yaml"
MCP_PY="$HOME/.local/share/uv/tools/odoo-mcp/bin/python"
[ -x "$MCP_PY" ] || die "odoo-mcp interpreter missing: $MCP_PY (run install-linux.sh)"

CFG="$PROFILE_HOME/config.yaml"
[ -f "$CFG" ] || die "no config.yaml in the bundle"
cp -a "$CFG" "$CFG.windows-original"

MCP_PY="$MCP_PY" PROFILE_HOME="$PROFILE_HOME" CFG="$CFG" "$VENV_PY" - <<'PY'
import os, re, sys

cfg = os.environ["CFG"]
mcp_py = os.environ["MCP_PY"]
prof = os.environ["PROFILE_HOME"]

text = open(cfg, encoding="utf-8").read()
before = text

# The interpreter: any Windows path ending in the odoo-mcp Scripts python.
text = re.sub(
    r"(?im)^(\s*command:\s*)[A-Za-z]:[\\/][^\r\n]*odoo-mcp[\\/]Scripts[\\/]python\.exe\s*$",
    lambda m: m.group(1) + mcp_py,
    text,
)
# The two policy files, and anything else under the old profile home.
text = re.sub(
    r"(?i)[A-Za-z]:[\\/]Users[\\/][^\s\"']*?[\\/]profiles[\\/]zalo-bot",
    prof.replace("\\", "/"),
    text,
)

leftover = re.findall(r"(?im)^.*[A-Za-z]:[\\/](?:Users|Windows|Program).*$", text)
if leftover:
    print("REMAINING WINDOWS PATHS -- not rewriting blindly:", file=sys.stderr)
    for line in leftover:
        print("   " + line.strip(), file=sys.stderr)
    sys.exit(3)

if text != before:
    open(cfg, "w", encoding="utf-8", newline="\n").write(text)

# Re-parse: a surgical edit that produces invalid YAML must not reach a
# gateway start, where it surfaces as "profile has no model" three steps later.
import yaml
d = yaml.safe_load(open(cfg, encoding="utf-8"))
mcp = (d.get("mcp_servers") or {}).get("odoo") or {}
env = mcp.get("env") or {}
print("   command   :", mcp.get("command"))
for k in ("ODOO_MCP_FIELD_POLICY_FILE", "ODOO_MCP_INSTRUCTIONS_FILE"):
    v = env.get(k, "")
    print("   %-10s: %s" % (k.replace("ODOO_MCP_", "").replace("_FILE", "").lower(), v))
    if v and not os.path.isfile(v):
        print("   MISSING FILE for " + k, file=sys.stderr)
        sys.exit(4)
if not mcp.get("command") or not os.access(mcp["command"], os.X_OK):
    print("   MCP command is not executable", file=sys.stderr)
    sys.exit(5)
print("   model     :", (d.get("model") or {}).get("default"))
PY
rc=$?
[ "$rc" -eq 0 ] || die "config.yaml rewrite failed (exit $rc) -- original kept at $CFG.windows-original"
ok "config.yaml rewritten and re-parsed"

# ------------------------------------------------------------- 4. the keys
say "permissions"
chmod 600 "$CFG" "$CFG.windows-original" 2>/dev/null || true
[ -f "$PROFILE_HOME/.env" ] && chmod 600 "$PROFILE_HOME/.env"
ok "config.yaml + .env are 600"

say "credentials carried over"
# A grep that matches nothing exits quietly and leaves a bot that receives
# messages and cannot answer -- count, do not just look.
n=$(grep -cE '^(ANTHROPIC|OPENAI|OPENROUTER|GOOGLE|GEMINI|GROQ|XAI|MISTRAL|DEEPSEEK|AZURE|LITELLM|NOUS)[A-Z_]*=.' \
      "$PROFILE_HOME/.env" 2>/dev/null || true)
[ "${n:-0}" -ge 1 ] && ok "$n provider credential(s) in .env" \
                    || die "no provider credential in .env -- the bot would answer nothing"
grep -qE '^ZALO_BRIDGE_TOKEN=.' "$PROFILE_HOME/.env" \
    && ok "ZALO_BRIDGE_TOKEN present" \
    || die "ZALO_BRIDGE_TOKEN missing -- the adapter cannot read events"
grep -qE 'ODOO_PASSWORD:' "$CFG" && ok "Odoo key inline in config.yaml" \
    || warn "no ODOO_PASSWORD in config.yaml -- check the MCP env block"

# -------------------------------------------------------------- 5. the state
say "state that cannot be rebuilt"
for f in zalo/session.json zalo/allowlist.json zalo/seen.json zalo/audit.jsonl; do
    [ -f "$PROFILE_HOME/$f" ] && ok "$f ($(wc -c <"$PROFILE_HOME/$f") bytes)" || warn "$f MISSING"
done
if [ -d "$PROFILE_HOME/zalo/attachments" ]; then
    ok "attachments: $(find "$PROFILE_HOME/zalo/attachments" -type f | wc -l) files"
else
    warn "no attachments/ (bundled with -NoAttachments?)"
fi
"$VENV_PY" - <<PY
import sqlite3
c = sqlite3.connect("file:$PROFILE_HOME/state.db?mode=ro", uri=True)
s, m = list(c.execute("SELECT (SELECT COUNT(*) FROM sessions),(SELECT COUNT(*) FROM messages)"))[0]
z = list(c.execute("SELECT COUNT(*) FROM sessions WHERE session_key LIKE '%zalo%'"))[0][0]
print("   ok   state.db: %d sessions (%d zalo), %d messages" % (s, z, m))
PY
[ $? -eq 0 ] || die "state.db is unreadable -- the history did not survive the copy"

say "data restored"
cat <<EOF
   profile   $PROFILE_HOME
   history   preserved (see the counts above -- compare with the Windows box)
   note      ops/ in the profile is the old Windows PowerShell set; the Linux
             equivalents are the units installed by install-services.sh.

Next:
   sudo HERMES_USER=\$(id -un) $(dirname "$0")/install-services.sh
   $(dirname "$0")/verify-linux.sh
EOF
