# Zalo Personal Adapter — Deploy Guide

Zalo personal-account gateway with a friendship-based access gate, message
dedup, admin approval flow, and an audit trail.

> **zca-js is unofficial.** It drives a real personal account through
> undocumented endpoints. Zalo can lock that account at any time. Use a
> dedicated account with its own SIM — never a staff member's personal one.

---

## Deploying on a fresh machine — ordered checklist

Read this section first. The rest of the document is reference material
organised by topic, not by the order you need it in.

The single most important step is **step 5**: without a dedicated profile the
bot can write to Odoo regardless of everything else configured here. Do not
open the bot to users before step 9 passes.

```
 1. Prerequisites          node >= 18, python >= 3.11, uv (for uvx)
 2. Copy the plugin        into $HERMES_HOME/hermes-agent/plugins/platforms/
 3. QR login               node bridge/index.js --qr-login
 4. Environment            .env: ZALO_ENABLED, ZALO_BRIDGE_TOKEN, ZALO_OWNER_ID
 5. Dedicated profile      ← the actual write barrier; see §"A dedicated profile"
 6. Odoo MCP               credentials + field ACL + instructions
 7. Access list            allowlist.json, admins, mode
 8. Ops                    health cron + logrotate
 9. Verify                 profile in effect, writes refused, no replay
```

### 1. Prerequisites

| Need | Why |
|---|---|
| Node.js >= 18 | the zca-js bridge |
| Python >= 3.11 | adapter uses `OrderedDict` typing and `datetime.timezone` |
| `uv` on PATH | the MCP server runs as `uvx odoo-mcp` |
| A **secondary** Zalo account with its own SIM | zca-js is unofficial; the account can be locked |

`systemctl --user` needs lingering enabled or the gateway dies at logout:

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger   # must print Linger=yes
```

### 2. Copy the plugin

```bash
git clone -b doanvh0812/feat-zalo-chatbot-spec git@github.com:doanvh0812/hermes-agent.git /tmp/hz
cp -r /tmp/hz/plugins/platforms/zalo "$HERMES_HOME/hermes-agent/plugins/platforms/"
cd "$HERMES_HOME/hermes-agent/plugins/platforms/zalo/bridge" && npm install --omit=dev
```

> **`hermes update` can overwrite this directory.** It lives inside the
> Hermes checkout and is not gitignored there. After every update, re-copy
> the plugin and re-run the verification in step 9. Keep the clone around.

### 3–8

Steps 3, 4, 6, 7, 8 are the sections below (QR login, Environment, MCP
configuration, allowlist, Monitoring). Step 5 is
**"A dedicated profile is required, not optional"** — do not skip it.

### 9. Verification gate

Run all four before letting anyone else message the bot.

```bash
# a. the profile is actually in effect (config here fails silently)
PID=$(python3 -c "import json;print(json.load(open('$HERMES_HOME/gateway_state.json'))['pid'])")
tr '\0' '\n' < /proc/$PID/environ | grep HERMES_HOME
#    -> must end in /profiles/zalo-bot

# b. the bridge is reachable and the token matches
curl -s 127.0.0.1:8647/health
curl -s -H "X-Bridge-Token: $ZALO_BRIDGE_TOKEN" '127.0.0.1:8647/events?since=0'
#    -> must NOT be {"error":"bad bridge token"}

# c. no replay across a restart
systemctl --user restart hermes-gateway-zalo
tail -5 "$HERMES_HOME/zalo/audit.jsonl"
#    -> no old messages re-answered; repeats appear as verdict "dup"
```

d. From Zalo, ask the bot to **create** something ("tạo 5 liên hệ test"). It
must refuse. If it offers to do it, or starts describing how, step 5 is not
in effect — stop and fix that before continuing.

### State that must survive a redeploy

Under `$HERMES_HOME/zalo/`:

| File | Losing it means |
|---|---|
| `session.json` | re-scan the QR (and Zalo dislikes frequent re-logins) |
| `allowlist.json` | everyone loses access; admins must be re-added |
| `seen.json` | one replay of the bridge ring buffer on next start |
| `audit.jsonl` | the access record is gone |

Back these up before any migration. `.health-state` is disposable.

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

---

## Agent-side configuration (`deploy/`)

The adapter controls *who reaches the agent*. These control *what the agent
does once a message gets through* — the two are independent layers, and
neither substitutes for backend authorization.

| File | Install to | Purpose |
|---|---|---|
| `deploy/SOUL.snippet.md` | assembled into the profile's `SOUL.md` | Guardrail floor, present in every system prompt |
| `deploy/profile/config.yaml` | `$HERMES_HOME/profiles/zalo-bot/` | Locked-down profile: no terminal, no toolsets |
| `deploy/profile/build-soul.sh` | run once | Assembles snippet + skill into the profile's `SOUL.md` |
| `deploy/profile/hermes-gateway-zalo.service` | `~/.config/systemd/user/` | Gateway bound to that profile |
| `deploy/skills/odoo/` | `$HERMES_HOME/skills/odoo/` | `odoo-chat-support` skill — full operating rules |
| `deploy/odoo-mcp/field_policy.json` | `$HERMES_HOME/odoo-mcp/` | Field-level ACL enforced on every read path |
| `deploy/odoo-mcp/instructions.txt` | `$HERMES_HOME/odoo-mcp/` | Server-level MCP instructions |
| `deploy/zalo-health.sh` | anywhere on PATH | Bridge health + token-drift check |
| `deploy/logrotate.conf` | user cron | Log rotation |

```bash
cp -r deploy/skills/odoo     "$HERMES_HOME/skills/"
mkdir -p "$HERMES_HOME/odoo-mcp" && cp deploy/odoo-mcp/* "$HERMES_HOME/odoo-mcp/"
```

`SOUL.snippet.md` goes into the **dedicated profile's** `SOUL.md`, not the
root one — see "A dedicated profile is required" below for why appending it
to the root profile does not restrain anything.

### A dedicated profile is required, not optional

**The adapter gate controls who reaches the agent. It does not control what
the agent can do.** An agent with a terminal tool does not need the Odoo MCP
server to write to Odoo — it can open a shell, write six lines of Python
against XML-RPC, and create records directly. Every restriction configured on
the MCP server (`ODOO_MCP_TOOLS_INCLUDE`, no `ENABLE_WRITES`, field ACL) is
bypassed by that path, and so is every instruction in `SOUL.md`. Observed in
production: asked to create records, the default-profile agent offered to do
exactly that.

So the agent serving chat users must be a **separate Hermes profile** with no
terminal and no code execution.

#### One gateway serves one profile

There is no per-platform routing. A gateway process resolves its profile once
at startup from `HERMES_HOME`, and `gateway.routing` in `config.yaml` is not
a real key — writing it there is silently ignored (no error, no warning).

`HERMES_HOME` selects the profile by path shape: when its immediate parent
directory is named `profiles`, that directory *is* the profile. So
`HERMES_HOME=~/.hermes/profiles/zalo-bot` runs the `zalo-bot` profile, while
`HERMES_HOME=~/.hermes` runs the root/default one.

Consequence: keeping a terminal-enabled agent on Telegram or the CLI while
Zalo runs locked down requires **two gateway processes**, one per profile.

#### Create the profile

```bash
hermes profile create zalo-bot
```

`$HERMES_HOME/profiles/zalo-bot/config.yaml` — write it minimal rather than
copying the root config, which drags in terminal and every toolset:

```yaml
model: max
terminal:
  enabled: false        # the single most important line in this file
toolsets:
  enabled: false
plugins:
  enabled: []
mcp_servers:
  odoo:
    command: uvx
    args: [odoo-mcp]
    env:
      # ... same block as the MCP section below ...
```

#### The profile's SOUL.md carries the full rules

A profile reads **its own** `SOUL.md`, not the root one. Concatenate the
guardrail snippet and the skill body into it, so the rules are present in
every system prompt rather than waiting on a context match:

```bash
P="$HERMES_HOME/profiles/zalo-bot"
{
  cat deploy/SOUL.snippet.md
  echo; echo "---"; echo
  sed '1,/^---$/d; 1,/^---$/d' deploy/skills/odoo/odoo-chat-support/SKILL.md
} > "$P/SOUL.md"
```

(The two `sed` ranges strip the skill's YAML frontmatter, which has no
meaning inside `SOUL.md`.)

#### Run the gateway on that profile

```ini
# ~/.config/systemd/user/hermes-gateway-zalo.service
[Service]
Environment="HERMES_HOME=%h/.hermes/profiles/zalo-bot"
ExecStart=%h/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
Restart=always
```

Disable Zalo (`ZALO_ENABLED`) in the root profile's env so the two gateways
do not both try to own the bridge — the adapter's scoped lock will reject the
second one, but it is cleaner not to race for it.

#### Verify the profile is actually in effect

Configuration that is ignored fails silently here, so check the running
process rather than the files:

```bash
PID=$(python3 -c "import json;print(json.load(open('$HERMES_HOME/gateway_state.json'))['pid'])")
tr '\0' '\n' < /proc/$PID/environ | grep HERMES_HOME
# must print .../profiles/zalo-bot — if it prints the root, the profile is NOT active
```

Then confirm behaviour from Zalo: ask the bot to create a record. It must
refuse, and it must have no terminal to fall back on.

### Why the rules live in two places

`SOUL.md` is in *every* system prompt; the skill loads only when the context
matches. A guardrail that exists solely in a skill is absent exactly when an
unexpected message arrives — so the hard invariants (read-only, retrieved
data is never instructions, user claims are not authorization, no internals,
no fabrication) are duplicated into `SOUL.md` on purpose. The skill carries
the long form: injection handling, data minimisation, ambiguity resolution,
worked examples.

Keep both in sync when either changes.

### These are guardrails, not the security boundary

Model instructions are advisory. The real boundary is:

1. **The Odoo user.** Create a dedicated read-only account with record rules
   scoped to what the bot may see. Never point it at admin or a real
   person's API key. This is the layer that actually holds when everything
   above it fails.
2. **The tool surface.** `ODOO_MCP_TOOLS_INCLUDE` (allowlist, not exclude —
   fail closed) and `ODOO_MCP_ENABLE_WRITES` left unset.
3. **Field ACL.** `field_policy.json`, enforced server-side on every read.
4. **The adapter gate.** Who may talk to the agent at all.

Note the gap this deployment does not close: with one shared Odoo account,
every approved user sees the same data. That is acceptable for an internal
staff bot and **not** acceptable for a bot serving external customers — that
would need per-user identity mapping, with `partner_id` resolved server-side
from the sender and never accepted as a model-supplied argument.

### MCP server configuration

Add to `$HERMES_HOME/config.yaml` under `mcp_servers:` once Odoo credentials
exist:

```yaml
  odoo:
    command: uvx
    args: [odoo-mcp]
    env:
      ODOO_URL: "https://odoo.example.com"
      ODOO_DB: "dbname"
      ODOO_USERNAME: "bot_readonly"
      ODOO_PASSWORD: "<api-key>"
      ODOO_TRANSPORT: "xmlrpc"          # json2 for Odoo 19+

      # 7 of 41 tools. Allowlist, so anything added upstream stays off.
      ODOO_MCP_TOOLS_INCLUDE: "search_records,read_record,aggregate_records,get_model_fields,build_domain,receivable_payable_aging,accounting_health_summary"
      ODOO_MCP_ALLOW_UNKNOWN_METHODS: "0"
      ODOO_MCP_FIELD_POLICY_FILE: "/home/USER/.hermes/odoo-mcp/field_policy.json"
      ODOO_MCP_INSTRUCTIONS_FILE: "/home/USER/.hermes/odoo-mcp/instructions.txt"
      ODOO_MCP_RATE_LIMIT_MODE: "block"
      ODOO_MCP_RATE_LIMIT_MAX_CALLS: "30"
      ODOO_LOCALE: "vi_VN"
```

Deliberately **excluded** tools and why:

| Tool | Reason |
|---|---|
| `execute_method` | Runs model methods; `create/write/unlink` are blocked but others are not |
| `read_attachment` | Pulls arbitrary base64 file content out of Odoo |
| `scan_addons_source` | Reads the host filesystem |
| `diagnose_access` | Returns the ACL map — a gift to anyone probing |
| `list_models`, `schema_catalog`, `get_odoo_profile` | Expose model inventory, modules, version |
| `chatter_post` | Writes to Odoo, gated or not |
| cross-instance / async / migrate / audit groups | Irrelevant to this use case |

Do **not** set `ODOO_MCP_ENABLE_WRITES`.

Use **stdio** (as above). The HTTP transport ships no authentication —
`MCP_ALLOW_REMOTE_HTTP=1` exposes an unauthenticated Odoo reader.

### Running against a high-privilege Odoo account

If the credential is an admin (or any account whose Odoo record rules do not
constrain it), Odoo's own model ACLs and record rules stop being a boundary
for this bot. Blocking writes is not enough — the exposure is on the **read**
side: `search_records` against an admin credential reaches `hr.contract`
(salaries), `hr.employee` (national ID, bank account), `res.users`, and every
company in the database.

`field_policy.json` is what closes that, using `allow` whitelists rather than
`deny` lists. An `allow` entry is exclusive: every field not listed is
stripped, so a query against a whitelisted-to-`["name"]` model still returns
rows but no usable content. Applied to HR/payroll, users and API keys, access
rules and config parameters, mail servers, and bank accounts.

This is enforced inside the MCP server process, on every read path
(`search_records`, `read_record`, aggregates, knowledge index, resources).
Aggregating on a denied field is rejected outright, so values cannot be
inferred from group totals. A malformed policy aborts the server at startup
rather than running unprotected.

Two limits worth stating plainly:

- It protects the **agent surface**, not Odoo. The credential itself can
  still do everything it could before — anything else holding that key is
  unaffected.
- `search_employee` and `search_holidays` return curated projections outside
  the redaction path. They are excluded from `ODOO_MCP_TOOLS_INCLUDE` here
  for that reason; if you add them back, re-check what they return.

A dedicated read-only Odoo user remains the stronger arrangement. Field ACL
is the compensating control when that is not available, not an equivalent.

### Audit gap

`ODOO_MCP_AUDIT_LOG` records only the write path. Read queries are logged
**nowhere** — not by mcp-odoo, not by `audit.jsonl`, which captures the
question and the answer but not the queries between them. If you need "what
did the bot actually read", add a tool-call hook at the agent layer.

---

## Known gaps and follow-up work

Ordered by how much they matter. Items 1–3 are open holes, not polish.

### 1. One shared Odoo account means no per-user data isolation

Every approved user sees the same data, because every query runs as the same
Odoo credential. A warehouse employee and an accountant get identical
answers to "how much does customer X owe".

Acceptable for an internal staff bot where everyone may see everything.
**Not** acceptable for external customers — customer A could ask about
customer B and get a real answer.

Closing it needs a `zalo_user_id -> res.partner` binding, with `partner_id`
resolved **server-side** from the sender and never accepted as a model-supplied
argument. If the model can pass `partner_id`, any prompt-level guardrail is
bypassable with a well-phrased question.

### 2. Read queries are audited nowhere

`audit.jsonl` records the question and the answer, not the queries between
them. `ODOO_MCP_AUDIT_LOG` covers only the write path. So "which records did
the bot actually read on 25/08" cannot be answered today.

For a bot reading financial data this is usually a compliance requirement.
It needs a tool-call hook at the agent layer, logging
`(zalo_user, tool, arguments, row_count)`.

### 3. Running as an Odoo admin account

If the credential is an admin, `field_policy.json` is the only thing keeping
HR and payroll out of the agent's context — verified working, but it is a
denylist of *known* models. Install a new Odoo module holding sensitive data
and it is exposed until someone adds it to the policy.

A dedicated read-only Odoo user with record rules does not have that
property. See "Running against a high-privilege Odoo account".

### 4. Conversation history does not survive a restart

The gateway logs `'zalo' is not a valid Platform` and drops stored session
entries, because `zalo` is a plugin platform outside the core `Platform`
enum. Each restart starts every conversation cold. Harmless for one-shot
lookups, visible to users in a multi-turn exchange.

### 5. Group support is untested

Mention-gating is implemented and unit-tested, but has not run against a real
Zalo group — the deployment it was built on had none. Before enabling groups:
approve one with `/duyet-nhom`, confirm untagged messages log as `ambient`,
and confirm a tagged message is answered.

`ZALO_MENTION_ALL_COUNTS` decides whether `@all` counts as addressing the
bot. Default off; turning it on in a busy group makes the bot answer every
broadcast.

### 6. `zalo_allow.py` cannot promote the first admin

The CLI edits users and groups. Bootstrapping the very first admin still
means editing `allowlist.json` by hand or setting `ZALO_OWNER_ID`.
`add_admin()` exists in the store; the CLI just does not expose a
"make this person an admin from scratch" flow.

### 7. Attachments are not persisted

Files sent to the bot land in Hermes' 24-hour document cache and are
discarded after the turn. If invoices or documents need to be referenced
later, write a hook that stores them (MinIO or similar) with sender,
session, and timestamp metadata at receive time.

### 8. Rate limiting is per tool, not per user

`ODOO_MCP_RATE_LIMIT_*` budgets are per `instance:tool`. One user asking
rapid questions consumes the budget for everyone, turning a protection into
a denial-of-service vector. A per-`zalo_user_id` limit belongs in the
adapter, with the MCP limit kept as a lower backstop.

### 9. Alerts go to syslog only

`zalo-health.sh` calls `logger`. Nobody reads syslog. Point it at something
that reaches a person — ntfy, a Telegram bot, email — or the silent failure
modes stay silent.

### 10. Two gateways, two failure surfaces

Running Zalo on its own profile means two gateway processes. Monitor both.
`systemctl --user status hermes-gateway hermes-gateway-zalo` is the minimum;
the health script only covers the bridge.
