#!/usr/bin/env bash
# The behaviour gate for the migrated bot. Run as the bot's user.
#
# Every outage this deployment has had looked healthy from its normal
# indicator: lockdown keys that parsed but locked nothing, a restart policy
# configured and never firing, a monitor that corrupted the file it checked,
# compression enabled that never ran, an admin tab that rendered outside the
# page, and an MCP server whose `mcp test` passes while every real tool call
# fast-fails. So nothing here asks a component whether it is fine -- each
# check makes the thing happen and reads the result.
#
# Exit 0 only when every FAIL-class check passed.
set -uo pipefail

HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes}"
PROFILE_NAME="${PROFILE_NAME:-zalo-bot}"
PROFILE_HOME="${PROFILE_HOME:-$HERMES_ROOT/profiles/$PROFILE_NAME}"
AGENT_DIR="${AGENT_DIR:-$HERMES_ROOT/hermes-agent}"
VENV_PY="$AGENT_DIR/venv/bin/python"
PORT="${ZALO_BRIDGE_PORT:-8647}"
PLUGIN="$AGENT_DIR/plugins/platforms/zalo"

fails=0
warns=0
sec()  { printf '\n\033[36m== %s\033[0m\n' "$*"; }
pass() { printf '   \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '   \033[31mFAIL\033[0m  %s\n' "$*"; fails=$((fails + 1)); }
warn() { printf '   \033[33mWARN\033[0m  %s\n' "$*"; warns=$((warns + 1)); }

[ -x "$VENV_PY" ] || { echo "no venv at $VENV_PY"; exit 2; }
envval() { sed -n "s/^$1=//p" "$PROFILE_HOME/.env" 2>/dev/null | head -1 | tr -d '\r' | sed 's/^"//; s/"$//'; }
TOK="$(envval ZALO_BRIDGE_TOKEN)"

# ---------------------------------------------------------- 1. the profile
# A gateway pointed at the hermes root runs the DEFAULT profile: no SOUL, no
# Odoo tools, no allowlist -- and it reports itself as perfectly connected.
sec "the gateway is serving the right profile"
GW_PID="$(systemctl show -p MainPID --value hermes-gateway-zalo 2>/dev/null || true)"
if [ -n "${GW_PID:-}" ] && [ "$GW_PID" != "0" ] && [ -r "/proc/$GW_PID/environ" ]; then
    GW_HOME="$(tr '\0' '\n' < "/proc/$GW_PID/environ" | sed -n 's/^HERMES_HOME=//p' | head -1)"
    case "$GW_HOME" in
        */profiles/"$PROFILE_NAME") pass "HERMES_HOME=$GW_HOME" ;;
        "") fail "gateway pid $GW_PID has no HERMES_HOME -- it is running the default profile" ;;
        *)  fail "gateway HERMES_HOME=$GW_HOME (expected .../profiles/$PROFILE_NAME)" ;;
    esac
else
    fail "gateway is not running (MainPID=${GW_PID:-none})"
fi

# ------------------------------------------------------------ 2. the bridge
sec "bridge"
HEALTH="$(curl -sf --max-time 5 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
if [ -z "$HEALTH" ]; then
    fail "no answer on 127.0.0.1:$PORT/health -- the bridge is not running"
else
    pass "reachable"
    if grep -q '"ready":true' <<<"$HEALTH"; then
        pass "logged in to Zalo"
    else
        fail "bridge is up but NOT logged in -- the cookie did not survive the move; re-scan the QR"
    fi
    OWN="$(sed -n 's/.*"own_id":"\{0,1\}\([0-9]\{1,\}\).*/\1/p' <<<"$HEALTH" | head -1)"
    OWNER="$(envval ZALO_OWNER_ID)"
    if [ -n "$OWN" ]; then
        pass "own_id=$OWN"
        # A QR re-scan with the wrong phone logs the bot in as a different
        # person: allowlist, history and every group binding then refer to an
        # account that is no longer the one answering.
        EXPECT="$(sed -n 's/.*"user_id": *"\{0,1\}\([0-9]\{1,\}\).*/\1/p' "$PROFILE_HOME/config.yaml" 2>/dev/null | head -1)"
        if [ -n "$EXPECT" ] && [ "$OWN" != "$EXPECT" ] && [ "$OWN" != "${OWNER:-}" ]; then
            warn "own_id does not match home_channel.user_id ($EXPECT) -- check which account is logged in"
        fi
    else
        warn "no own_id in /health -- group mention-gating and friend access may be off"
    fi
fi

# --------------------------------------------- 3. the token the adapter uses
# Silent, total, and the one that bit us: /health answers and /send works
# while every inbound message is dropped with 401.
sec "bridge token"
if [ -z "$TOK" ]; then
    warn "ZALO_BRIDGE_TOKEN unset -- both sides derive it from session.json, so they agree, but drift cannot be detected"
else
    EV="$(curl -s --max-time 5 -H "X-Bridge-Token: $TOK" "http://127.0.0.1:$PORT/events?since=0" 2>/dev/null || true)"
    if grep -q 'bad bridge token' <<<"$EV"; then
        fail "the token in .env is NOT the token the bridge is using -- no message will ever be read"
    elif [ -z "$EV" ]; then
        fail "/events returned nothing"
    else
        pass "accepted by the bridge"
        BC="$(sed -n 's/.*"cursor": *\([0-9]\{1,\}\).*/\1/p' <<<"$EV" | head -1)"
        AC="$("$VENV_PY" -c "import json;print(json.load(open('$PROFILE_HOME/zalo/seen.json')).get('cursor',0))" 2>/dev/null || echo 0)"
        if [ -n "$BC" ] && [ "${AC:-0}" -gt "$BC" ] 2>/dev/null; then
            warn "adapter cursor ($AC) is ahead of the bridge ($BC) -- the adapter follows it back down on the next poll and logs it; first messages after a re-login can be dropped if it does not"
        else
            pass "cursor sane (adapter ${AC:-0}, bridge ${BC:-?})"
        fi
    fi
fi

# ----------------------------------------------------------- 4. the patches
sec "runtime patches still applied"
chk() {
    local out
    out="$("$VENV_PY" "$PLUGIN/deploy/patches/$1" "${@:2}" --check 2>&1 | tail -1)"
    case "$out" in
        *"already patched"*) pass "$1" ;;
        *) fail "$1 -- $out" ;;
    esac
}
chk fix-mcp-stdio-liveness.py       --hermes-agent "$AGENT_DIR"
chk fix-compaction-status-leak.py   --hermes-agent "$AGENT_DIR"
chk fix-execute-method-field-acl.py --site-packages "$HOME/.local/share/uv/tools/odoo-mcp"

# --------------------------------------------- 5. the MCP server, for real
# `hermes mcp test` connects and lists tools without calling one, so it passes
# while the liveness bug aborts every actual call. This drives a real
# tools/call over stdio, which exercises Odoo auth, the liveness fix and the
# field ACL in a single probe.
sec "Odoo MCP: a real tool call"
PROFILE_HOME="$PROFILE_HOME" "$VENV_PY" - <<'PY'
import json, os, subprocess, sys, yaml

prof = os.environ["PROFILE_HOME"]
cfg = yaml.safe_load(open(os.path.join(prof, "config.yaml"), encoding="utf-8"))
srv = (cfg.get("mcp_servers") or {}).get("odoo")
if not srv:
    print("   FAIL  no mcp_servers.odoo in config.yaml"); sys.exit(1)

env = dict(os.environ)
env.update({k: str(v) for k, v in (srv.get("env") or {}).items()})
cmd = [srv["command"]] + list(srv.get("args") or [])

try:
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True, bufsize=1)
except Exception as exc:
    print("   FAIL  cannot start the MCP server: %s" % exc); sys.exit(1)

def send(obj):
    p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()

def read(want_id, timeout=60):
    import select
    end = __import__("time").monotonic() + timeout
    while __import__("time").monotonic() < end:
        r, _, _ = select.select([p.stdout], [], [], 1.0)
        if not r:
            continue
        line = p.stdout.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == want_id:
            return msg
    return None

rc = 0
try:
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "verify-linux", "version": "1"}}})
    if not read(1):
        print("   FAIL  the MCP server never answered initialize"); sys.exit(1)
    print("   PASS  initialize")
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    # A read that the field policy is supposed to redact: if redaction is off,
    # mobile_phone comes back in full.
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "search_records",
                     "arguments": {"model": "res.partner", "domain": [],
                                   "fields": ["name", "mobile_phone"], "limit": 1}}})
    msg = read(2)
    if msg is None:
        print("   FAIL  tools/call never returned -- this is the liveness bug's signature")
        rc = 1
    elif "error" in msg:
        print("   FAIL  tools/call error: %s" % str(msg["error"])[:200]); rc = 1
    else:
        blob = json.dumps(msg)
        if "has exited; failing the call fast" in blob:
            print("   FAIL  fast-fail path hit -- the stdio liveness patch is not in effect"); rc = 1
        else:
            print("   PASS  search_records returned (Odoo credential works)")
            if "redacted_fields" in blob:
                print("   PASS  field_policy redaction is active")
            else:
                print("   WARN  no redacted_fields in the reply -- confirm field_policy.json is loaded")
except Exception as exc:
    print("   FAIL  MCP probe blew up: %s" % exc); rc = 1
finally:
    try:
        p.stdin.close(); p.terminate(); p.wait(timeout=5)
    except Exception:
        p.kill()
sys.exit(rc)
PY
if [ $? -eq 0 ]; then pass "MCP probe completed"; else fail "MCP probe failed (see above)"; fi

# ------------------------------------------------------- 6. the model's key
# This fails LAST in normal operation -- only once a user messages the bot --
# so it is checked before they do. One token, costs nothing.
sec "the model credential actually works"
PROFILE_HOME="$PROFILE_HOME" "$VENV_PY" - <<'PY'
import json, os, sys, urllib.request, yaml

prof = os.environ["PROFILE_HOME"]
cfg = yaml.safe_load(open(os.path.join(prof, "config.yaml"), encoding="utf-8"))
m = cfg.get("model") or {}
base = (m.get("base_url") or "https://api.openai.com/v1").rstrip("/")
model = m.get("default") or ""
key = ""
for line in open(os.path.join(prof, ".env"), encoding="utf-8", errors="replace"):
    line = line.strip().replace("\r", "")
    if line.startswith("OPENAI_API_KEY="):
        key = line.split("=", 1)[1].strip().strip('"')
        break
if not key:
    print("   FAIL  no OPENAI_API_KEY in the profile .env"); sys.exit(1)

req = urllib.request.Request(
    base + "/chat/completions",
    data=json.dumps({"model": model,
                     "messages": [{"role": "user", "content": "ping"}],
                     "max_completion_tokens": 1}).encode(),
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=45) as r:
        json.load(r)
    print("   PASS  %s answered on %s" % (model, base))
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", "replace")[:300]
    # A model-name rejection still proves the credential is good.
    if e.code in (400, 404) and ("model" in body.lower()):
        print("   WARN  credential is valid but the model was rejected: %s" % body)
    else:
        print("   FAIL  HTTP %s: %s" % (e.code, body)); sys.exit(1)
except Exception as exc:
    print("   FAIL  %s" % exc); sys.exit(1)
PY
[ $? -eq 0 ] || fail "model credential check failed"

# ------------------------------------------------------------ 7. the Odoo key
sec "Odoo XML-RPC credential"
PROFILE_HOME="$PROFILE_HOME" "$VENV_PY" - <<'PY'
import os, sys, xmlrpc.client, yaml
prof = os.environ["PROFILE_HOME"]
env = ((yaml.safe_load(open(os.path.join(prof, "config.yaml"), encoding="utf-8"))
        .get("mcp_servers") or {}).get("odoo") or {}).get("env") or {}
url, db = env.get("ODOO_URL"), env.get("ODOO_DB")
user, key = env.get("ODOO_USERNAME"), env.get("ODOO_PASSWORD")
if not (url and key):
    print("   WARN  no Odoo credential in config.yaml"); sys.exit(0)
try:
    uid = xmlrpc.client.ServerProxy(url + "/xmlrpc/2/common").authenticate(db, user, key, {})
except Exception as exc:
    print("   FAIL  cannot reach %s: %s" % (url, exc)); sys.exit(1)
if uid:
    print("   PASS  %s@%s -> uid %s" % (user, db, uid))
else:
    print("   FAIL  Odoo rejected the API key (a password change revokes it)"); sys.exit(1)
PY
[ $? -eq 0 ] || fail "Odoo credential check failed"

# -------------------------------------------------------- 8. tools + history
sec "toolsets and history"
# This block must set its own exit status: a printed "FAIL" that does not
# reach the counter gives a green gate over a bot with no Odoo tools and no
# history, which is the whole thing being verified.
PROFILE_HOME="$PROFILE_HOME" "$VENV_PY" - <<'PY'
import os, sqlite3, sys, yaml

prof = os.environ["PROFILE_HOME"]
rc = 0
cfg = yaml.safe_load(open(os.path.join(prof, "config.yaml"), encoding="utf-8"))
ts = set((cfg.get("platform_toolsets") or {}).get("zalo") or [])
need = {"odoo", "skills", "file", "memory"}
if need <= ts:
    print("   PASS  zalo toolsets: %s" % ", ".join(sorted(ts)))
else:
    print("   FAIL  zalo toolsets missing %s (have: %s)"
          % (sorted(need - ts), sorted(ts) or "none"))
    rc = 1

c = sqlite3.connect("file:%s/state.db?mode=ro" % prof, uri=True)
s, m = list(c.execute(
    "SELECT (SELECT COUNT(*) FROM sessions),(SELECT COUNT(*) FROM messages)"))[0]
z = list(c.execute(
    "SELECT COUNT(*) FROM sessions WHERE session_key LIKE '%zalo%'"))[0][0]
if z and m:
    print("   PASS  history: %d sessions (%d zalo), %d messages" % (s, z, m))
else:
    print("   FAIL  history did not arrive: %d sessions (%d zalo), %d messages"
          % (s, z, m))
    rc = 1
sys.exit(rc)
PY
[ $? -eq 0 ] || fail "toolsets/history check failed (see above)"
for f in zalo/session.json zalo/allowlist.json zalo/audit.jsonl SOUL.md; do
    [ -s "$PROFILE_HOME/$f" ] && pass "$f present" || fail "$f missing or empty"
done
if [ "$(stat -c '%a' "$PROFILE_HOME/config.yaml" 2>/dev/null)" = "600" ]; then
    pass "config.yaml is 600 (it holds the Odoo key inline)"
else
    warn "config.yaml is $(stat -c '%a' "$PROFILE_HOME/config.yaml" 2>/dev/null) -- it contains a credential; chmod 600"
fi

# -------------------------------------------------------------- 9. the units
sec "supervision"
for u in hermes-zalo-bridge hermes-gateway-zalo hermes-zalo-admin; do
    st="$(systemctl is-active "$u" 2>/dev/null || true)"
    [ "$st" = "active" ] && pass "$u active" || fail "$u is $st"
    # The give-up default is what turned a crash into a 4-day outage.
    lim="$(systemctl show -p StartLimitIntervalUSec --value "$u" 2>/dev/null || true)"
    case "$lim" in 0|"0"|infinity) : ;; *) warn "$u StartLimitIntervalUSec=$lim -- systemd may stop retrying after repeated failures" ;; esac
done
systemctl is-enabled hermes-zalo-health.timer >/dev/null 2>&1 \
    && pass "health timer enabled" || warn "health timer not enabled -- nothing will notice an outage"

sec "result"
if [ "$fails" -eq 0 ]; then
    printf '   \033[32mall behaviour checks passed\033[0m (%d warning(s))\n' "$warns"
else
    printf '   \033[31m%d check(s) FAILED\033[0m, %d warning(s)\n' "$fails" "$warns"
fi
cat <<'EOF'

   Two things only a human can check, from Zalo itself:
     1. send the bot a business question -- it must answer from Odoo, and
        logs/agent.log must show tool calls, not a guess
     2. ask it to CREATE something ("tạo 5 liên hệ test") -- it must refuse

EOF
exit $([ "$fails" -eq 0 ] && echo 0 || echo 1)
