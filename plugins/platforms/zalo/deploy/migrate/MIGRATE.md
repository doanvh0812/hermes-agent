# Moving the Zalo bot from Windows to Linux

Runbook for lifting the whole bot — source, memory, skills and conversation
history — off the Windows Server box onto a Linux host, without losing the
Zalo session and without changing the agent's behaviour.

Read "What actually moves" and "The one thing that can fail" before booking a
window. Everything else is mechanical.

---

## What actually moves

| | Lives in | How it travels |
|---|---|---|
| Zalo session (cookie, imei, UA) | `zalo/session.json` | copied — **cannot be regenerated without a phone** |
| Access control | `zalo/allowlist.json` | copied |
| Dedup cursor | `zalo/seen.json` | copied |
| Access record | `zalo/audit.jsonl` | copied |
| Slip images etc. | `zalo/attachments/` | copied (~95 MB, 328 files) |
| Conversation history | `state.db` | copied through sqlite3's backup API |
| Skills | `skills/` | copied |
| Memories | `memories/` | copied |
| The bot's persona | `SOUL.md` | copied |
| Profile config | `config.yaml` | copied, **3 paths rewritten** |
| Secrets | `.env` | copied, **CRLF normalised** |
| Agent runtime | `hermes-agent/` | rebuilt from the pinned upstream commit |
| Python deps | `venv/` | rebuilt (`uv sync --locked`) |
| Bridge deps | `bridge/node_modules/` | rebuilt (`npm ci`) |
| Odoo MCP server | uv tool | rebuilt + re-patched |
| Supervision | Task Scheduler | **replaced** by systemd units |
| Ops scripts | `ops/*.ps1` | **replaced** by the scripts in this directory |

SQLite files are architecture-neutral, so the history crosses from Windows to
Linux as-is. Verified on the real database: 9 sessions (all Zalo), 1428
messages, the main DM holding 1237 of them.

## What the new host must reproduce, and why it is not "clone the fork"

Production today is **upstream Hermes at a pinned commit, with the Zalo plugin
copied on top** — not a checkout of our fork:

```
hermes-agent  = NousResearch/hermes-agent @ 1bbb6e5bce56e721ab685af4cd87df21bbff4d35
                + tools/mcp_tool.py   patched   (MCP stdio liveness)
                + gateway/run.py      patched   (compaction status leak)
                + plugins/platforms/zalo/  from doanvh0812/hermes-agent, branch
                                            doanvh0812/feat-zalo-chatbot-spec
odoo-mcp      = 1.3.0 via `uv tool`
                + odoo_mcp/tools_write.py patched (execute_method field ACL)
bridge        = zca-js 2.1.2
python 3.11 · node >= 18 (prod runs 24.16)
```

Our fork is **not** a descendant of that pinned commit — measured, the two
trees differ by 336 files and roughly 4,400 added / 18,700 removed lines of
core code. Cloning the fork as the runtime would swap the agent core for a
different one while the stated goal is stability, so `install-linux.sh`
reproduces the arrangement above instead.

None of those versions are hardcoded twice: `export-bundle.ps1` measures them
on the live Windows box and writes `MANIFEST.json` into the bundle, and
`install-linux.sh` reads the pin from there.

## The one thing that can fail: the Zalo session

`session.json` is `{cookie, imei, userAgent}` — pure data, no machine binding,
so it copies cleanly. What cannot be promised is the **server side**: Zalo may
decide a session that suddenly appears from a new IP is worth revoking.

- There is no way to test this without doing it. A rehearsal that starts a
  second bridge on the same account from a different IP is itself a login
  event, so the rehearsal is run **without starting the bridge** (see below).
- If Zalo rejects it, the bridge comes up but `/health` reports
  `"ready": false` and `bridge.log` loops `login failed: Đăng nhập thất bại`.
  `install-services.sh` and `verify-linux.sh` both check for exactly this and
  say so rather than leaving it to be discovered by a user.
- The fix is a QR re-scan, which needs a phone with the bot's Zalo account.
  **Have that phone in the room during cutover.** See "When Zalo rejects the
  session".

Everything else in this migration is reversible in minutes. This is the part
that needs a person.

---

## Before the window

### The host

- Debian/Ubuntu or RHEL-family, 2 vCPU / 4 GB RAM is ample — the agent is
  I/O-bound on API calls, not CPU-bound.
- Disk: allow **5 GB**. Measured on the current host: profile state 208 MB,
  Hermes checkout 592 MB (of which venv 287, `.git` 88), `odoo-mcp` 53 MB,
  bridge `node_modules` 10 MB — about 900 MB in use, plus uv's Python
  download and build caches during install.
- A dedicated unprivileged user (the examples use `hermes`) with a real home
  directory. Everything lives under `$HOME`; nothing is installed system-wide
  except the systemd units, one sudoers drop-in and the logrotate config.
- Outbound HTTPS to `api.openai.com`, `erp.vni.edu.vn`, Zalo's endpoints,
  github.com, pypi.org and the npm registry.
- **Confirm the new host can reach `erp.vni.edu.vn` over HTTPS before the
  window.** If the ERP restricts by source IP, that is a change someone else
  has to make, and it is better found now than at cutover.
- Ports: nothing inbound is needed for the bot itself (the bridge is loopback
  only). 80/443 inbound only if the admin panel moves too.

### Prerequisites the scripts will not install for you

`git`, `curl`, `tar`, `node >= 18` and `npm`. `install-linux.sh` checks each
one and stops with the exact command to run. It does bootstrap `uv` and a
pinned Python into `$HOME` itself, so the host does not need a system
Python 3.11.

### Rehearse it first

Strongly recommended, and nearly free. A rehearsal takes a **live** bundle
(services on Windows keep running), builds the whole Linux side, and stops
before the bridge logs in:

```bash
# on Windows — live snapshot, nothing is stopped
powershell -File ...\deploy\migrate\export-bundle.ps1

# on Linux
./install-linux.sh  ~/zalo-bot-bundle-<ts>.tar.gz
./restore-bundle.sh ~/zalo-bot-bundle-<ts>.tar.gz
```

Then stop. Do **not** run `install-services.sh` during a rehearsal — it starts
the bridge, which is a second login on the live account. What the rehearsal
proves is everything except the login: the pin resolves, `uv sync --locked`
succeeds, the patches apply, `npm ci` works, the path rewrite produces a
config that parses, and the history arrives intact.

---

## Cutover

The bot is down from step 2 to step 6. Budget 30–45 minutes, most of it
`uv sync` and `npm ci` — both of which the rehearsal has already warmed.

### 1. Freeze changes

Tell the people who use the bot. A message answered during the window is a
message answered by a host that is about to be archived.

### 2. Final bundle (Windows)

```powershell
powershell -File C:\Users\Administrator\vni_zalo_agent\plugins\platforms\zalo\deploy\migrate\export-bundle.ps1 -Final
```

`-Final` stops and **disables** `HermesZaloGateway` and `HermesZaloHealth`,
kills the detached bridge so nothing rewrites `session.json` underneath the
archive, then bundles. It prints the rollback commands on the way out.

Copy both files across:

```powershell
scp "$env:USERPROFILE\zalo-migrate\zalo-bot-bundle-<ts>.tar.gz" `
    "$env:USERPROFILE\zalo-migrate\zalo-bot-bundle-<ts>.tar.gz.sha256" `
    hermes@NEW-HOST:~/
```

### 3. Runtime (Linux, as the bot's user)

```bash
cd ~   # wherever you cloned the fork; the scripts live in
       # <fork>/plugins/platforms/zalo/deploy/migrate/
./install-linux.sh ~/zalo-bot-bundle-<ts>.tar.gz
```

Verifies the checksum, reads the manifest, clones upstream **detached at the
pin**, builds the venv from `uv.lock`, copies the plugin from the fork branch,
`npm ci`s the bridge, installs `odoo-mcp` and applies all three patches —
re-running each one with `--check` afterwards rather than trusting the apply
step's exit code.

### 4. Data

```bash
./restore-bundle.sh ~/zalo-bot-bundle-<ts>.tar.gz
```

Moves any existing profile aside, copies the state in, normalises `.env` to
LF, rewrites the three Windows paths in `config.yaml` **surgically** — a YAML
round-trip would delete the comments that document why compression and the
display gates are set the way they are — re-parses the result, chmod 600s both
credential files, and counts the history.

It refuses rather than guesses: a Windows path it does not recognise, a
policy file that does not exist, an MCP command that is not executable, or a
missing provider credential each stop it with the original config preserved as
`config.yaml.windows-original`.

### 5. Services

```bash
sudo HERMES_USER=hermes <fork>/plugins/platforms/zalo/deploy/migrate/install-services.sh
```

Installs five units, a sudoers drop-in scoped to exactly
`systemctl restart hermes-gateway-zalo`, and the logrotate config; then starts
the bridge, waits for it to log in, and starts the rest.

**This is where you learn whether the Zalo cookie survived.** The script says
so explicitly either way.

### 6. The gate

```bash
./verify-linux.sh
```

Then two things only a person can check, from Zalo:

1. Ask a business question. It must answer **from Odoo** — and
   `logs/agent.log` must show tool calls, not a guess.
2. Ask it to create something ("tạo 5 liên hệ test"). It must refuse.

### 7. Admin panel (optional, not on the critical path)

The panel is loopback-only behind Caddy. Moving it means repointing
`zalo-admin.vniedu.vn` at the new host and letting Caddy re-issue the
certificate, which needs inbound 80/443 **and** DNS already pointing here.
Do it after the bot is confirmed working — a failed ACME challenge does not
affect the bot at all.

```
zalo-admin.vniedu.vn {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8648
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "no-referrer"
        -Server
    }
}
```

The panel's gateway-restart button now works on Linux (it shells out to
`sudo -n systemctl restart`). On Windows it could never restart *itself*
because the launcher held port 8648 through a detached child; under systemd
`systemctl restart hermes-zalo-admin` just works.

### 8. Decommission, not delete

Leave the Windows box **off but intact** for a week. The tasks are already
disabled; do not uninstall anything. If the move has to be reverted, that box
is the rollback.

---

## Rollback

Before step 5 has started the bridge, rollback is free — nothing on Zalo's
side has changed:

```powershell
schtasks /Change /TN HermesZaloGateway /ENABLE
schtasks /Change /TN HermesZaloHealth  /ENABLE
schtasks /Run    /TN HermesZaloGateway
```

After the Linux bridge has logged in, the Windows session cookie may already
have been invalidated by that login. Rollback still works, but it may need a
QR re-scan on the Windows side. So: **do not start the Linux bridge until you
have decided to commit.**

Messages received by the Linux host before a rollback stay in the Linux
`state.db` and do not come back to Windows. There is no merge path for that;
keep the window short instead.

## When Zalo rejects the session

Symptom: `/health` returns `"ready": false`, and `logs/bridge.log` loops
`login failed: Đăng nhập thất bại`.

`--qr-web` binds the **same `PORT`** the bridge uses (8647), so the service has
to stop first. It also *requires* `BRIDGE_TOKEN` when there is no usable
session yet, and it defaults to binding `0.0.0.0` — which would put the login
QR on every interface. Pin it to loopback and reach it through an SSH tunnel:

```bash
sudo systemctl stop hermes-zalo-bridge

P=~/.hermes/profiles/zalo-bot
cd ~/.hermes/hermes-agent/plugins/platforms/zalo/bridge
HERMES_HOME=$P \
ZALO_SESSION_FILE=$P/zalo/session.json \
BRIDGE_TOKEN=$(sed -n 's/^ZALO_BRIDGE_TOKEN=//p' $P/.env | tr -d '\r"') \
PORT=8647 ZALO_QR_BIND=127.0.0.1 ZALO_QR_HOST=127.0.0.1 \
node index.js --qr-web

# from your laptop, then open the printed URL and scan with the bot's phone:
#   ssh -L 8647:127.0.0.1:8647 hermes@NEW-HOST

sudo systemctl start hermes-zalo-bridge
```

Two traps, both already paid for once:

- **Scan with the bot's phone, not yours.** A re-scan with a different account
  logs the bot in as a different person; the allowlist, the history and every
  group binding then refer to an account that is no longer answering.
  `verify-linux.sh` compares `own_id` against `home_channel.user_id` and warns.
- **The listener can die quietly after a web QR login.** Check `/health` again
  after restarting the bridge, and send one real message — not just a `/send`
  curl, which works even when nothing inbound is being read.

## Why these choices

**systemd system units, not `systemctl --user`.** User units need
`loginctl enable-linger` or they stop with the last login session and never
return at boot. That is a quiet way to lose the bot to a reboot — the exact
failure class this migration exists to end.

**`StartLimitIntervalSec=0` on every unit.** systemd's default gives up after
5 restarts in 10 seconds. On Windows a restart policy was configured, present
in the task XML, looked correct — and never fired, which is how the bot stayed
dead for four days in October. A supervisor that gives up is not supervision.

**The bridge is its own unit.** On Windows it was a detached process that
outlived gateway restarts, so the adapter took the *attach* path, which is
what production has actually been running. Under systemd's default
`KillMode=control-group` a gateway-spawned bridge dies with the gateway, and
every restart (model change, config edit, deploy, crash) becomes a fresh Zalo
cookie login — and Zalo kicks accounts that re-login often. A separate unit
keeps the attach path and lets systemd supervise the long-lived half properly.

**`run-bridge.sh` exists for one mapping.** The bridge reads `BRIDGE_TOKEN`
and `PORT`; the profile `.env` sets `ZALO_BRIDGE_TOKEN` and
`ZALO_BRIDGE_PORT`. The adapter does that translation in Python; a systemd
`EnvironmentFile=` does not, so the bridge would derive its token from
`sha256(session.json)` while the adapter authenticates with the `.env` value.
That mismatch is the failure DEPLOY.md calls "silent, total, and the one that
bit us": `/health` answers, `/send` works, cron delivery works, and every
inbound message is dropped with a 401 behind the scenes.

**`.env` is normalised to LF.** The health check reads it with `grep | cut`.
GNU grep on Linux is byte-exact, so a Windows CRLF leaves a trailing `\r`
*inside* the token value. (This cannot be reproduced from Git Bash on the
Windows box — MSYS grep does text-mode translation and strips the `\r` — which
is precisely why it would first appear on the new host.)

What a `\r` in the token does was measured directly against the live bridge,
and it is **not** a token mismatch: node's HTTP parser rejects the malformed
request with `400 Bad Request` and an **empty body**, before the token is ever
compared. A wrong token returns `401 {"error":"bad bridge token"}`; a correct
one returns `200`. The old check only grepped the body for
`bad bridge token`, so the 400 would have passed it and the monitor would
report `ok` while the adapter read nothing.

Both ends are now covered: `envval` strips `\r`, `restore-bundle.sh`
normalises the file, and the token check reads the **status code** rather than
grepping the body.

## What this migration fixes for free

- A supervisor that actually retries forever, instead of one that gave up.
- The admin panel can restart itself (`systemctl restart hermes-zalo-admin`),
  which the Windows launcher could not do because it held port 8648 through a
  detached child.
- Log rotation that covers the profile's logs. The old `logrotate.conf`
  pointed at `$HERMES_HOME/zalo/`, which is the **default** profile's
  directory — it would have rotated nothing here while looking correctly
  installed.
- `journalctl -u hermes-gateway-zalo` for history that survives a crash,
  instead of redirected stdout files.
- One tree, no copy step: on Windows the install's copy of `deploy/` had
  drifted from the repo (missing `patches/`, two newer skills). On Linux the
  plugin is copied from the fork by `install-linux.sh`, every time.

## Still open after the move

- **Alerts go nowhere.** The health check now runs correctly and the bridge
  respawns itself within ~67 seconds, but if recovery keeps failing (an
  expired cookie needing a QR, say) the only record is a line in a log file.
  `zalo-health.sh` already speaks Telegram — deliberately a different
  transport, so a dead Zalo cannot swallow the notice about itself. It needs a
  bot token and a chat id in the profile `.env`:
  `ZALO_ALERT_TG_TOKEN` and `ZALO_ALERT_TG_CHAT`. Until those are set, the
  25-hour outage pattern is reported by a human noticing, same as last time.
- **Credentials unchanged by the move.** The GitHub PAT and the admin-panel
  password come across as-is. Both are worth rotating while touching the
  deployment anyway; the panel password changes with
  `python server.py --set-password`.
- **`home_channel.user_id` is a stale uid** (`7019109989188354203`) that is
  not in the bot's friend list or allowlist. Harmless, but it means
  `verify-linux.sh`'s own_id comparison has nothing good to compare against.
- **Hermes is 72 commits behind** (0.20.5 → 0.21.3). Deliberately **not**
  bundled into this migration: moving hosts and changing the agent core in one
  window makes any regression impossible to attribute. Upstream has since
  fixed the MCP stdio liveness bug with a regression test, so an upgrade later
  retires `fix-mcp-stdio-liveness.py` — remove that patch first or the pull
  conflicts.
