#!/usr/bin/env bash
# Build the Zalo bot's runtime on a fresh Linux host.
#
# Reproduces what the Windows box actually ran, which is NOT "clone the fork":
#
#   hermes-agent  = upstream NousResearch @ <pinned commit>
#                   + tools/mcp_tool.py   patched (MCP stdio liveness)
#                   + gateway/run.py      patched (compaction status leak)
#                   + plugins/platforms/zalo/  copied from OUR fork's branch
#   odoo-mcp      = pinned version via `uv tool`
#                   + odoo_mcp/tools_write.py patched (execute_method field ACL)
#   bridge        = npm ci against the committed package-lock.json
#
# The fork is NOT a descendant of the pinned upstream commit -- at the time of
# writing they differ by 336 files. Cloning the fork as the runtime would
# silently swap the agent core for a different one, which is the opposite of
# what a stability migration is for. So: upstream at the pin, plugin on top.
#
# Reads the pin and every version from the bundle's MANIFEST.json, measured on
# the live Windows host. Nothing here is hardcoded twice.
#
# Run as the bot's own unprivileged user (NOT root). Installs nothing system
# wide: it bootstraps uv + a pinned Python into $HOME and stops with the exact
# command to run if a real prerequisite (git, node, curl) is missing.
#
#   ./install-linux.sh ~/zalo-bot-bundle-20261030-1200.tar.gz
set -uo pipefail

BUNDLE="${1:-}"
HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes}"
AGENT_DIR="${AGENT_DIR:-$HERMES_ROOT/hermes-agent}"
FORK_DIR="${FORK_DIR:-$HOME/vni_zalo_agent}"
FORK_REMOTE="${FORK_REMOTE:-https://github.com/doanvh0812/hermes-agent.git}"
WORK="${WORK:-$HOME/zalo-migrate}"

say()  { printf '\n\033[36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mFATAL\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && die "run as the bot's user, not root (the data lives in \$HOME)"
[ -n "$BUNDLE" ] || die "usage: $0 <bundle.tar.gz>"
[ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"

# ------------------------------------------------------- 0. verify + unpack
say "bundle"
if [ -f "$BUNDLE.sha256" ]; then
    # tr -d '\r': a .sha256 written on Windows carries CRLF, and sha256sum
    # then reads the \r as part of the filename and fails to open it -- which
    # looks identical to a corrupt transfer.
    ( cd "$(dirname "$BUNDLE")" && tr -d '\r' < "$(basename "$BUNDLE").sha256" | sha256sum -c - ) \
        || die "checksum mismatch -- re-copy the bundle"
    ok "sha256 verified"
else
    warn "no $BUNDLE.sha256 beside the bundle; skipping integrity check"
fi

mkdir -p "$WORK"
UNPACK="$WORK/unpacked"
rm -rf "$UNPACK" && mkdir -p "$UNPACK"
tar -xzf "$BUNDLE" -C "$UNPACK" || die "tar extract failed"
MANIFEST="$UNPACK/MANIFEST.json"
[ -f "$MANIFEST" ] || die "bundle has no MANIFEST.json"
ok "unpacked to $UNPACK"

# jq is not on every host; python3 is, and we need it anyway.
command -v python3 >/dev/null 2>&1 || die "python3 is required to read the manifest"
mval() { python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$MANIFEST" "$1"; }

AGENT_UPSTREAM="$(mval agent_upstream)"
AGENT_COMMIT="$(mval agent_commit)"
FORK_BRANCH="$(mval fork_branch)"
FORK_COMMIT="$(mval fork_commit)"
ODOO_MCP_VER="$(mval odoo_mcp)"
PY_WANT="$(mval python | grep -oE '[0-9]+\.[0-9]+' | head -1)"
UV_WANT="$(mval uv | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
CORE_MODS="$(mval agent_core_modified)"

[ -n "$AGENT_COMMIT" ] || die "manifest has no agent_commit"
[ -n "$FORK_BRANCH" ]  || die "manifest has no fork_branch"
PY_WANT="${PY_WANT:-3.11}"

say "reproducing"
printf '   upstream  %s\n             @ %s\n' "$AGENT_UPSTREAM" "$AGENT_COMMIT"
printf '   fork      %s @ %s\n' "$FORK_BRANCH" "${FORK_COMMIT:0:12}"
printf '   python    %s\n   odoo-mcp  %s\n' "$PY_WANT" "${ODOO_MCP_VER:-<unpinned>}"
printf '   core patches on the source host:\n%s\n' "$(echo "$CORE_MODS" | sed 's/^/     /')"

# ------------------------------------------------------- 1. prerequisites
say "prerequisites"
missing=0
need() {
    if command -v "$1" >/dev/null 2>&1; then ok "$1 $(${1} --version 2>&1 | head -1)"
    else printf '   \033[31mmissing\033[0m %s -- %s\n' "$1" "$2"; missing=1; fi
}
need git  "apt install git   |  dnf install git"
need curl "apt install curl  |  dnf install curl"
need tar  "apt install tar   |  dnf install tar"
need node "nodesource: curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs"
need npm  "ships with nodejs"
[ "$missing" -eq 0 ] || die "install the missing prerequisites above, then re-run"

NODE_MAJOR="$(node --version | sed 's/^v//; s/\..*//')"
[ "$NODE_MAJOR" -ge 18 ] || die "node $NODE_MAJOR is too old; zca-js needs >= 18"

# uv bootstraps itself into ~/.local/bin and brings its own Python, so the host
# does not need a system python3.11.
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    say "installing uv ${UV_WANT:-latest}"
    if [ -n "$UV_WANT" ]; then
        curl -LsSf "https://astral.sh/uv/$UV_WANT/install.sh" | sh || die "uv install failed"
    else
        curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv install failed"
    fi
    command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; add ~/.local/bin"
fi
ok "uv $(uv --version)"

# ------------------------------------------------- 2. upstream, at the pin
say "hermes-agent @ $AGENT_COMMIT"
mkdir -p "$HERMES_ROOT"
if [ -d "$AGENT_DIR/.git" ]; then
    ok "checkout exists; fetching the pin"
    git -C "$AGENT_DIR" fetch --quiet origin || die "git fetch failed"
else
    git clone --quiet "${AGENT_UPSTREAM:-https://github.com/NousResearch/hermes-agent.git}" "$AGENT_DIR" \
        || die "git clone failed"
fi
# Detached at the exact commit: a branch name would drift on the next fetch,
# and "stable" has to mean the same code tomorrow.
git -C "$AGENT_DIR" checkout --quiet --detach "$AGENT_COMMIT" \
    || die "cannot check out $AGENT_COMMIT -- is the pin reachable from origin?"
ok "detached HEAD at $(git -C "$AGENT_DIR" rev-parse --short HEAD)"

# -------------------------------------------------------------- 3. the venv
say "python deps (uv sync --locked)"
uv python install "$PY_WANT" 2>/dev/null || true
if [ ! -x "$AGENT_DIR/venv/bin/python" ]; then
    uv venv "$AGENT_DIR/venv" --python "$PY_WANT" || die "uv venv failed"
fi
# --locked installs exactly uv.lock with hashes. --extra all is what
# setup-hermes.sh uses, and the admin panel needs fastapi/uvicorn from it.
( cd "$AGENT_DIR" && UV_PROJECT_ENVIRONMENT="$AGENT_DIR/venv" uv sync --extra all --locked ) \
    || die "uv sync failed"
VENV_PY="$AGENT_DIR/venv/bin/python"
ok "$("$VENV_PY" --version)"
for m in fastapi uvicorn httpx jinja2 yaml psutil; do
    "$VENV_PY" -c "import $m" 2>/dev/null && ok "import $m" || warn "import $m FAILED (admin panel needs it)"
done

# ---------------------------------------------------- 4. the plugin, on top
say "zalo plugin from the fork"
if [ -d "$FORK_DIR/.git" ]; then
    git -C "$FORK_DIR" fetch --quiet origin || warn "fork fetch failed; using what is on disk"
else
    git clone --quiet "$FORK_REMOTE" "$FORK_DIR" || die "cannot clone the fork"
fi
git -C "$FORK_DIR" checkout --quiet "$FORK_BRANCH" || die "no such branch: $FORK_BRANCH"
git -C "$FORK_DIR" pull --quiet --ff-only origin "$FORK_BRANCH" 2>/dev/null || true
FORK_HAVE="$(git -C "$FORK_DIR" rev-parse HEAD)"
[ "$FORK_HAVE" = "$FORK_COMMIT" ] || warn "fork is at ${FORK_HAVE:0:12}, bundle recorded ${FORK_COMMIT:0:12}"

SRC="$FORK_DIR/plugins/platforms/zalo"
DST="$AGENT_DIR/plugins/platforms/zalo"
[ -d "$SRC" ] || die "no plugin at $SRC"
mkdir -p "$DST"
# Deliberately NOT --delete: node_modules lives under DST and must survive.
rsync -a --exclude '__pycache__' --exclude 'node_modules' --exclude '*.bak*' \
      "$SRC"/ "$DST"/ 2>/dev/null \
  || tar -C "$SRC" --exclude='__pycache__' --exclude='node_modules' --exclude='*.bak*' -cf - . \
     | tar -C "$DST" -xf - \
  || die "plugin copy failed"
ok "plugin -> $DST"

# The runtime imports the copy under hermes-agent. A stale .pyc from a
# previous version wins over a newer .py when timestamps go backwards, which
# is exactly what a restore from an archive does.
find "$DST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
ok "cleared stale bytecode"

# ------------------------------------------------------------ 5. the bridge
say "zalo bridge (zca-js)"
( cd "$DST/bridge" && npm ci --omit=dev ) || ( cd "$DST/bridge" && npm install --omit=dev ) \
    || die "npm install failed"
ok "zca-js $(node -e "process.stdout.write(require('$DST/bridge/node_modules/zca-js/package.json').version)" 2>/dev/null || echo '?')"

# ----------------------------------------------------------- 6. the MCP tool
say "odoo-mcp ${ODOO_MCP_VER:-latest}"
if [ -n "$ODOO_MCP_VER" ]; then
    uv tool install --force "odoo-mcp==$ODOO_MCP_VER" || die "uv tool install odoo-mcp failed"
else
    uv tool install --force odoo-mcp || die "uv tool install odoo-mcp failed"
fi
MCP_PY="$(ls -d "$HOME/.local/share/uv/tools/odoo-mcp/bin/python" 2>/dev/null || true)"
[ -x "$MCP_PY" ] || die "odoo-mcp python not found where expected"
ok "$MCP_PY"

# ----------------------------------------------------------- 7. the patches
# Every one of these fixes a failure that looked healthy from its normal
# indicator, so each is verified by re-running with --check afterwards rather
# than trusting the apply step's exit code.
say "patches"
PATCHES="$DST/deploy/patches"
apply() {
    local script="$1"; shift
    [ -f "$PATCHES/$script" ] || die "patch missing from the plugin: $script"
    "$VENV_PY" "$PATCHES/$script" "$@" >/dev/null 2>&1
    local out
    out="$("$VENV_PY" "$PATCHES/$script" "$@" --check 2>&1 | tail -1)"
    case "$out" in
        *"already patched"*) ok "$script  ($out)" ;;
        *) die "$script did not take: $out" ;;
    esac
}
apply fix-mcp-stdio-liveness.py       --hermes-agent "$AGENT_DIR"
apply fix-compaction-status-leak.py   --hermes-agent "$AGENT_DIR"
apply fix-execute-method-field-acl.py --site-packages "$HOME/.local/share/uv/tools/odoo-mcp"

say "runtime ready"
cat <<EOF
   agent     $AGENT_DIR  ($(git -C "$AGENT_DIR" rev-parse --short HEAD), detached)
   venv      $VENV_PY
   plugin    $DST
   odoo-mcp  $MCP_PY

Next:
   ./restore-bundle.sh "$BUNDLE"          # the data + path rewrite
   sudo ./install-services.sh             # systemd units
   ./verify-linux.sh                      # the behaviour gate
EOF
