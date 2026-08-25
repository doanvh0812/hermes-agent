# Zalo Personal Adapter — Deploy Guide

Zalo personal-account gateway with a friendship-based access gate, message
dedup, admin approval flow, and an audit trail.

> **zca-js is unofficial.** It drives a real personal account through
> undocumented endpoints. Zalo can lock that account at any time. Use a
> dedicated account with its own SIM — never a staff member's personal one.

---

## Contents

| Path | Role |
|---|---|
| `adapter.py` | Gateway adapter: access gate, dedup, audit, admin commands |
| `allowlist_store.py` | Hot-reloading `allowlist.json` reader/writer |
| `zalo_allow.py` | CLI: pick friends/groups by name, no uid needed |
| `bridge/index.js` | Node bridge to zca-js; loopback HTTP on 127.0.0.1:8647 |
| `plugin.yaml` | Plugin manifest |

---

## 1. Install

```bash
# plugin lives in the Hermes plugin tree
cp -r plugins/platforms/zalo "$HERMES_HOME/hermes-agent/plugins/platforms/"

cd "$HERMES_HOME/hermes-agent/plugins/platforms/zalo/bridge"
npm install --omit=dev        # or let the adapter install on first connect
```

Node.js >= 18 required.

## 2. Log in once (QR)

```bash
cd "$HERMES_HOME/hermes-agent/plugins/platforms/zalo/bridge"
node index.js --qr-login       # writes zalo-login-qr.png; scan with the Zalo app
```

Writes `$HERMES_HOME/zalo/session.json` (cookie + imei + userAgent). Cookie
login on later starts needs all three — re-run this if the bridge reports
"Missing required params".

## 3. Environment

```bash
ZALO_ENABLED=true

# REQUIRED — see "Bridge token" below. Any long random string.
#   openssl rand -hex 32
ZALO_BRIDGE_TOKEN=<random-hex>

# Bootstrap admin; allowlist.json wins once it lists admins.
ZALO_OWNER_ID=<your Zalo uid>

ZALO_REQUIRE_MENTION=1        # groups: only answer when tagged
ZALO_MENTION_ALL_COUNTS=0     # does @all count as tagging the bot
ZALO_ALLOW_ALL_USERS=0        # keep 0 in production

# optional
ZALO_BRIDGE_PORT=8647
ZALO_HOME_CHANNEL=<chat id for cron delivery>
```

`ZALO_ALLOWED_USERS` from the stock adapter is **superseded** by
`allowlist.json`; it is ignored by the gate.

## 4. `$HERMES_HOME/zalo/allowlist.json`

Auto-created on first run. Hot-reloaded within 5s of an edit — no restart.

```json
{
  "mode": "friends",
  "admins": [{"id": "1234567890", "name": "Ops lead"}],
  "notify": "all",
  "users": {
    "allow": [{"id": "222", "name": "Contractor, not a friend"}],
    "deny":  [{"id": "333", "name": "Left the company 2026-08"}]
  },
  "groups": {"allow": [{"id": "555", "name": "Accounting"}]}
}
```

| Key | Meaning |
|---|---|
| `mode` | `friends` = Zalo friends get access · `list` = allowlist only |
| `admins` | Receive stranger alerts; the only ones who can run admin commands |
| `notify` | `all` = alert every admin · `first` = alert the first one only |

### Access order

```
0. admin     -> allow   (denylist does not apply; never lock admins out)
1. denylist  -> deny    (beats friendship)
2. allowlist -> allow
3. friend    -> allow   (mode: friends only)
4. otherwise -> deny
```

Groups need **both**: the group approved **and** the sender passing 0–4.
There is no friend-gate for groups — you do not control who adds whom.

> **Operational consequence of `mode: friends`:** accepting a Zalo friend
> request becomes a permission grant. If the account owner accepts requests
> casually, run `mode: "list"` instead.

### Admin commands

Non-admins running these get **silence** — the bot must not reveal the
commands exist. Denials are recorded as `cmd_denied` in the audit log.

```
/duyet <code>     approve the stranger from an alert
/chan <code>      deny-list them instead
/duyet-nhom       approve the current group (run inside it)
/ai               show who currently has access
```

## 4b. Managing access from the terminal

`zalo_allow.py` lists friends and groups **by name and phone number** and
writes the allowlist for you — no uid archaeology, no log reading. The
bridge must be running; the gateway need not be.

```bash
python3 zalo_allow.py           # interactive picker
python3 zalo_allow.py --list    # who currently has access
python3 zalo_allow.py --json    # machine-readable
```

```
--- NHÓM ---
    1. [ ] Kho Hà Nội                 12 thành viên    chưa duyệt
--- BẠN BÈ ---
    2. [★] Đoàn                       —                admin
    3. [◦] Nguyễn Văn A               090xxx1234       bạn bè

Chọn> 1        approve the group
Chọn> !3       deny-list a user (beats friendship)
Chọn> +3       make admin      ·  -+3 revoke admin
Chọn> -1       revoke          ·  r   re-fetch directory
```

The directory is fetched once per run and cached: Zalo throttles these
upstream calls and the bridge reports a throttle as HTTP 500, so re-listing
after every edit would trip it. Only local markers are recomputed; press
`r` after befriending someone new.

Admins live in a **flat** `admins` list, unlike `users`/`groups` which nest
a bucket under a section. `add()`/`remove()` raise on `"admins"` for that
reason — use `add_admin()`/`remove_admin()`. `remove_admin()` also refuses
to remove the last admin, since an empty list falls back to
`ZALO_OWNER_ID` and, if that is unset, locks everyone out of approvals.

## 5. Bootstrap the first admin

Chicken-and-egg: no admin means nobody can approve anyone.

```bash
echo '{"mode":"list","admins":[]}' > "$HERMES_HOME/zalo/allowlist.json"
# message the bot from the account that should be admin, then:
tail -1 "$HERMES_HOME/zalo/audit.jsonl" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['sender'])"
```

Put that uid in `admins`, set `mode` back to `friends` if wanted.

## 6. Start

```bash
systemctl --user restart hermes-gateway
```

The adapter attaches to a running bridge when one is healthy, and spawns
one otherwise. The bridge outlives the gateway on purpose — Zalo kicks
accounts that re-login frequently.

---

## Verifying it actually works

`gateway_state.json` reporting `zalo: connected` **is not proof**. It is a
cached value and stays "connected" while the adapter is dead. Check these:

```bash
# 1. bridge alive
curl -s 127.0.0.1:8647/health          # {"ok":true,"ready":true,"own_id":"..."}

# 2. token matches (THE failure mode — see below)
curl -s -H "X-Bridge-Token: $ZALO_BRIDGE_TOKEN" \
     '127.0.0.1:8647/events?since=0'   # must NOT be {"error":"bad bridge token"}

# 3. adapter really loaded (pyc mtime should track the last restart)
stat -c '%y' .../plugins/platforms/zalo/__pycache__/adapter.cpython-311.pyc

# 4. traffic is flowing
tail -f "$HERMES_HOME/zalo/audit.jsonl"
```

---

## Failure modes worth knowing

### Bridge token drift — silent, total, and the one that bit us

The stock token is `sha256(session.json)`. The bridge **rewrites that file**
right after login and every 30 minutes to rotate cookies. The bridge keeps
the hash from startup; the adapter recomputes a different one. Every
authenticated call then returns 401.

It fails invisibly: `/health` takes no token, so the adapter sees "ready",
logs "attached to running bridge", and the gateway shows `connected` —
while `/events` has never once returned data. Symptom: the bot goes quiet
mid-session with nothing in any log.

**Fix:** always set `ZALO_BRIDGE_TOKEN`. `bridge_token_for_session()`
prefers it and only falls back to the racy hash when it is unset.

### Session expiry

Cookies die (Zalo-side revocation, ~days). The bridge logs
`login failed: Đăng nhập thất bại` in a loop and nothing else happens.
Re-run `node index.js --qr-login`.

### Two connect paths

`connect()` either **spawns** a bridge or **attaches** to a live one. Since
the bridge is long-lived, *attach* is the common path in production. Any
per-connection state (own_id, friend cache) must be initialised in **both**
— missing it in the attach path silently disables group mention-gating and
friend-based access.

### Session keys

The gateway logs `'zalo' is not a valid Platform` and drops stored session
entries, because `zalo` is a plugin platform outside the core `Platform`
enum. Conversation history does not survive a restart. Cosmetic for access
control, but it means no cross-restart context.

---

## Monitoring (do not skip)

Nothing alerts when the bridge dies or the token drifts. Both are invisible
without an external check.

```bash
#!/usr/bin/env bash
# /usr/local/bin/zalo-health.sh   —   */10 * * * *
TOK="${ZALO_BRIDGE_TOKEN:?}"
if ! curl -sf --max-time 5 127.0.0.1:8647/health | grep -q '"ready":true'; then
    logger -t zalo-bridge "ALERT: bridge not ready"; exit 1
fi
if curl -s --max-time 5 -H "X-Bridge-Token: $TOK" \
        '127.0.0.1:8647/events?since=0' | grep -q 'bad bridge token'; then
    logger -t zalo-bridge "ALERT: bridge token drift"; exit 1
fi
```

Route `logger` output to a real channel (ntfy, Telegram, email).

### Log rotation

```
# /etc/logrotate.d/hermes-zalo
/home/USER/.hermes/zalo/audit.jsonl /home/USER/.hermes/zalo/bridge.log {
    weekly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

`copytruncate` is required — both processes hold the fd open and neither
reopens on SIGHUP.

---

## Audit log

`$HERMES_HOME/zalo/audit.jsonl`, one JSON object per line.

```json
{"ts":"...","dir":"in","verdict":"allowed","sender":"701...","thread":"701...",
 "thread_type":"user","msg_id":"...","text":"stock level for SP-001"}
```

| verdict | meaning |
|---|---|
| `allowed` | passed the gate, dispatched to the agent |
| `denied` | blocked by the gate |
| `dup` | duplicate `msg_id` (replay suppressed) |
| `ambient` | group message with no @mention |
| `cmd` / `cmd_denied` | admin command run / attempted by a non-admin |
| `approved` / `blocked` / `approved_group` | allowlist mutations, with `by_admin` |
| `sent` / `send_failed` | outbound |

**Scope limit:** this records *what was asked and answered*. It does **not**
record which MCP tools ran or with what arguments. `mcp-odoo` only audits
its write path, so read queries leave no trace on either side. If you need
"what data did the bot actually read", add a tool-call hook at the agent
layer.

---

## Why dedup exists

`_poll_loop` advances `_event_cursor` only after a whole batch is handled,
and the cursor resets to 0 on adapter start. Restarting the gateway against
a still-running bridge therefore replays its entire 500-event ring buffer —
the bot answers 500 old messages. The seen-set is capped at 2000 (> 500) so
one full replay is always absorbed. It is in-memory: a *bridge* restart
clears both sides, which is fine because the ring buffer is empty then too.

---

## Tests

`test_gate.py` covers the store, mention parsing, and dedup with no gateway
or bridge running (37 assertions):

```bash
python3 test_gate.py
```

Cases that matter most, verified against a live account:

1. Non-admin runs `/duyet` → **silence**, `cmd_denied` logged
2. Gateway restart, bridge alive → old messages logged `dup`, none answered
3. Untagged group message → `ambient`, no reply
4. Admin runs `/duyet-nhom` in an unapproved group → works (bypass path)
5. Corrupt `allowlist.json` → last good copy retained, no new access granted
