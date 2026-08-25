"""Zalo personal-account platform adapter for Hermes Agent.

A bundled platform plugin that talks to a Zalo PERSONAL account through a
local loopback Node.js bridge running the unofficial ``zca-js`` library
(``plugins/platforms/zalo/bridge/``), and relays messages between Zalo
DMs/groups and the agent via the standard ``BasePlatformAdapter`` interface.

Design highlights
-----------------

**Unofficial API — account risk is real.** zca-js drives Zalo's private web
endpoints with a personal-account session cookie. Zalo may suspend or lock
that account at any time. The setup wizard and platform hint both say this
out loud; users should attach a secondary account, never their primary one.

**Bridge lifecycle.** The adapter spawns ``node index.js`` itself (after a
one-time ``npm install``), hands it a per-spawn random token, waits for
``GET /health``, then polls ``GET /events?since=<cursor>`` every half
second. The bridge binds 127.0.0.1 only, validates the Host header
(DNS-rebinding defense, mirrors scripts/whatsapp-bridge) and requires the
token on every non-health request.

**Chat-id prefixes.** Zalo user ids and group ids are both plain digit
strings, so the thread type cannot be recovered from the id alone — but
every outbound path (send tool, cron delivery, home channel) only carries
the id string. We therefore persist prefixed ids: ``u<digits>`` for users,
``g<digits>`` for groups. Prefixes are stripped before hitting the bridge.

**Session persistence.** Credentials live ONLY in ``~/.hermes/.env``
(``ZALO_COOKIE_JSON``/``ZALO_IMEI``/``ZALO_USER_AGENT`` — secrets). On
connect they are composed into ``$HERMES_HOME/zalo/session.json`` (mode
0600); the bridge thereafter refreshes that file with rotated cookies via
``getCookie()`` so restarts survive without re-exporting cookies. A one-shot
QR login (``node index.js --qr-login``) is the friendlier alternative and
writes the same file.

**Media.** Inbound media messages are classified from zca-js content
objects and any https URLs found inside are surfaced on ``media_urls``
(vision/web tools can fetch them directly). Outbound media sends local file
paths to the bridge, which attaches them via ``sendMessage({attachments})``
— so the bridge must run on the same host as the files (true for the
default spawn-in-place design).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Same contract as the LINE plugin's wrapper (#59739): under profile
    multiplexing a secondary profile reads its own scope and a miss returns
    ``default`` — never borrow another profile's value out of
    ``os.environ``. For the default profile constructed unscoped, the bare
    read raises ``UnscopedSecretError``; there ``os.environ`` IS that
    profile's value, so fall back to it.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


logger = logging.getLogger(__name__)

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BRIDGE_DIR = Path(__file__).resolve().parent / "bridge"
DEFAULT_BRIDGE_PORT = 8647
HEALTH_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5
POLL_ERROR_BACKOFF_SECONDS = 3.0
NPM_INSTALL_TIMEOUT_SECONDS = 300

# Conservative Zalo text limits (no hard published cap; ~2000 observed).
ZALO_PER_MESSAGE_CHARS = 2000
ZALO_SAFE_CHARS = 1800
ZALO_MAX_CHUNKS = 8

USER_PREFIX = "u"
GROUP_PREFIX = "g"

# --- Access gate / dedup / audit -------------------------------------------
# The bridge ring buffer holds 500 events; the seen-set must be larger so a
# full replay (gateway restart while the bridge stays alive — the
# attach-on-restart design) is entirely suppressed.
SEEN_MSG_CAP = 2000
AUDIT_MAX_TEXT = 500
# zca-js uses sentinel uids for "@all" mentions.
MENTION_ALL_UIDS = {"-1", "0"}
# Friend cache refresh + negative-lookup TTL.
FRIEND_REFRESH_SECONDS = 900.0
FRIEND_MISS_TTL_SECONDS = 300.0
FRIEND_MISS_CAP = 500
# One stranger notification per sender per day.
PENDING_NOTIFY_COOLDOWN_SECONDS = 86400.0
PENDING_CODE_TTL_SECONDS = 3600.0
ADMIN_COMMANDS = {"/duyet", "/chan", "/duyet-nhom", "/ai"}


# ---------------------------------------------------------------------------
# Markdown stripping (URL-preserving) — Zalo renders no Markdown
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_CODE_INLINE_RE = re.compile(r"`([^`]+)`")
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)



_TECHNICAL_ERROR_PATTERNS = [
    r"Rate limited after \d+ retries",
    r"HTTP 429",
    r"RESOURCE_EXHAUSTED",
    r"RateLimitError",
    r"AuthenticationError",
    r"InternalServerError",
    r"Traceback \(most recent call last\)",
    r"antigravity/",
    r"openai\.",
    r"anthropic\.",
    r"google\.api_core",
]

def sanitize_technical_error(text: str) -> str:
    """Sanitize internal engine/provider error dumps before sending to Zalo."""
    for pattern in _TECHNICAL_ERROR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return "⚠️ Hệ thống AI đang tạm thời quá tải hoặc bận xử lý. Anh/chị vui lòng thử lại sau ít giây nhé."
    return text

def strip_markdown_preserving_urls(text: str) -> str:
    """Strip Markdown Zalo can't render, keeping bare URLs tappable."""
    if not text:
        return text

    def _unfence(m: re.Match) -> str:
        return m.group(1).rstrip("\n")

    text = _MD_CODE_BLOCK_RE.sub(_unfence, text)
    text = _MD_CODE_INLINE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITAL_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub("• ", text)
    return text


def split_for_zalo(
    text: str,
    max_chars: int = ZALO_SAFE_CHARS,
    max_chunks: int = ZALO_MAX_CHUNKS,
) -> List[str]:
    """Split into Zalo-sized chunks at paragraph/line/space boundaries."""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            remaining = ""
            break
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        if chunks:
            tail = chunks[-1]
            if len(tail) > max_chars - 1:
                tail = tail[: max_chars - 1]
            chunks[-1] = tail.rstrip() + " …(tiếp)"
        else:
            chunks.append(remaining[: max_chars - 1] + "…")
    return chunks


# ---------------------------------------------------------------------------
# Chat-id prefix helpers — Zalo user/group ids are both digit strings
# ---------------------------------------------------------------------------

def format_chat_id(thread_id: str, thread_type: str) -> str:
    tid = (thread_id or "").strip()
    return f"{GROUP_PREFIX}{tid}" if thread_type == "group" else f"{USER_PREFIX}{tid}"


def parse_chat_id(chat_id: str) -> Tuple[str, str]:
    """Return ``(thread_id, thread_type)``; unknown shapes degrade to a user."""
    cid = (chat_id or "").strip()
    if cid.startswith(GROUP_PREFIX):
        return cid[len(GROUP_PREFIX):], "group"
    if cid.startswith(USER_PREFIX):
        return cid[len(USER_PREFIX):], "user"
    return cid, "user"


# ---------------------------------------------------------------------------
# Inbound classification — zca-js content string vs structured object
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s\"'\\<>]+")

_PARAM_TYPE_MAP = (
    (("image", "photo"), MessageType.PHOTO),
    (("video",), MessageType.VIDEO),
    (("voice", "audio"), MessageType.VOICE),
    (("sticker",), MessageType.STICKER),
    (("file", "attach"), MessageType.DOCUMENT),
)


def _load_allowlist_store():
    """Import ``AllowlistStore`` from this plugin directory.

    Plugin dirs are not guaranteed to be on ``sys.path``, so load the
    sibling module by explicit file location rather than by name.
    """
    import importlib.util

    module_path = Path(__file__).resolve().parent / "allowlist_store.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_zalo_allowlist_store", module_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load allowlist store from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AllowlistStore


def mentions_self(msg: Dict[str, Any], own_id: str,
                  *, honor_mention_all: bool = False) -> bool:
    """True when a group message @-mentions the bot account.

    zca-js exposes ``TGroupMessage.mentions = [{uid, pos, len, type}]``; the
    bridge forwards the untouched payload as ``msg["raw"]``.
    """
    if not own_id:
        return False
    raw = msg.get("raw")
    if not isinstance(raw, dict):
        return False
    mentions = raw.get("mentions")
    if not isinstance(mentions, list):
        return False
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        uid = str(mention.get("uid") or "")
        if uid and uid == own_id:
            return True
        if honor_mention_all and uid in MENTION_ALL_UIDS:
            return True
    return False


def classify_inbound(msg: Dict[str, Any]) -> Tuple[MessageType, str, List[str], List[str]]:
    """Map a normalized bridge ``msg`` block onto the gateway event fields.

    Returns ``(message_type, text, media_urls, media_types)``.
    """
    content = str(msg.get("content") or "")
    if content:
        return MessageType.TEXT, content, [], []

    content_obj = msg.get("content_obj") if isinstance(msg.get("content_obj"), dict) else None
    title = str(msg.get("title") or "")

    if content_obj is not None:
        param_types: List[str] = []
        params = content_obj.get("parameters")
        if isinstance(params, list):
            for p in params:
                if isinstance(p, dict):
                    param_types.append(str(p.get("type", "")).lower())
        blob = json.dumps(content_obj, ensure_ascii=False)
        urls = [u.rstrip(".,);") for u in _URL_RE.findall(blob)]
        mtype = MessageType.TEXT
        for needles, candidate in _PARAM_TYPE_MAP:
            if any(any(n in pt for n in needles) for pt in param_types):
                mtype = candidate
                break
        media_url = urls[0] if urls else None
        media_urls = [media_url] if media_url else []
        mime_by_type = {
            MessageType.PHOTO: "image/jpeg",
            MessageType.VIDEO: "video/mp4",
            MessageType.VOICE: "audio/mp4",
            MessageType.STICKER: "image/webp",
            MessageType.DOCUMENT: "application/octet-stream",
        }
        label = title or {
            MessageType.PHOTO: "[ảnh]",
            MessageType.VIDEO: "[video]",
            MessageType.VOICE: "[tin nhắn thoại]",
            MessageType.STICKER: "[sticker]",
            MessageType.DOCUMENT: "[tệp đính kèm]",
        }.get(mtype, "[tin nhắn]")
        return mtype, label, media_urls, [mime_by_type[mtype]]
    return MessageType.TEXT, title or "[tin nhắn]", [], []


# ---------------------------------------------------------------------------
# Bridge HTTP client
# ---------------------------------------------------------------------------

class _ZaloBridgeClient:
    """Thin async client for the loopback bridge HTTP API."""

    def __init__(self, port: int, token: str, *, timeout: float = 20.0) -> None:
        self._base = f"http://127.0.0.1:{port}"
        self._headers = {"X-Bridge-Token": token}
        self._timeout = timeout

    async def _request(self, method: str, path: str, payload: Any = None,
                       timeout: Optional[float] = None) -> Dict[str, Any]:
        import aiohttp
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout or self._timeout)
        ) as session:
            async with session.request(
                method,
                self._base + path,
                headers=self._headers,
                json=payload,
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    err = ""
                    if isinstance(data, dict):
                        err = str(data.get("error", ""))
                    raise RuntimeError(f"zalo bridge {resp.status}: {err}")
                return data if isinstance(data, dict) else {}

    async def health(self, timeout: float = 3.0) -> Dict[str, Any]:
        import aiohttp
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(self._base + "/health") as resp:
                if resp.status != 200:
                    return {}
                return await resp.json(content_type=None)

    def events(self, since: int) -> Any:
        return self._request("GET", f"/events?since={since}")

    def friends(self) -> Any:
        return self._request("GET", "/friends")

    def user_info(self, uid: str) -> Any:
        from urllib.parse import quote
        return self._request("GET", f"/user-info?id={quote(str(uid), safe='')}")

    def groups(self) -> Any:
        return self._request("GET", "/groups")

    def send_text(self, thread_id: str, thread_type: str, msg: str) -> Any:
        return self._request("POST", "/send", {
            "thread_id": thread_id,
            "thread_type": thread_type,
            "msg": msg,
        })

    def send_media(self, thread_id: str, thread_type: str,
                   paths: List[str], caption: str = "") -> Any:
        return self._request("POST", "/send-media", {
            "thread_id": thread_id,
            "thread_type": thread_type,
            "paths": paths,
            "caption": caption,
        }, timeout=60.0)

    def send_typing(self, thread_id: str, thread_type: str) -> Any:
        return self._request("POST", "/typing", {
            "thread_id": thread_id,
            "thread_type": thread_type,
        })


# ---------------------------------------------------------------------------
# Bridge process management
# ---------------------------------------------------------------------------

def resolve_bridge_dir() -> Path:
    """Prefer the in-repo bridge; fall back to a HERMES_HOME mirror when the
    checkout is read-only (mirrors whatsapp_common.resolve_whatsapp_bridge_dir)."""
    try:
        probe = BRIDGE_DIR / ".write_test"
        probe.write_text("x")
        probe.unlink(missing_ok=True)
        return BRIDGE_DIR
    except OSError:
        pass
    try:
        from hermes_constants import get_hermes_home
        mirrored = Path(get_hermes_home()) / "zalo-bridge"
        if not mirrored.exists():
            shutil.copytree(BRIDGE_DIR, mirrored,
                            ignore=shutil.ignore_patterns("node_modules"))
        return mirrored
    except Exception:
        return BRIDGE_DIR


def compose_session_file(session_path: Path) -> Optional[str]:
    """Compose ``session.json`` from .env credentials if absent.

    Returns an error string on invalid input, None on success/no-op.
    """
    if session_path.exists():
        return None
    cookie_raw = _get_scoped_secret("ZALO_COOKIE_JSON") or ""
    imei = _get_scoped_secret("ZALO_IMEI") or ""
    ua = _get_scoped_secret("ZALO_USER_AGENT") or ""
    if not cookie_raw:
        return "No Zalo session: run `hermes setup zalo` (QR login) or set ZALO_COOKIE_JSON/ZALO_IMEI/ZALO_USER_AGENT"
    try:
        cookie = json.loads(cookie_raw)
    except json.JSONDecodeError as exc:
        return f"ZALO_COOKIE_JSON is not valid JSON: {exc}"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps({
        "cookie": cookie,
        "imei": imei,
        "userAgent": ua,
    }, indent=2))
    try:
        os.chmod(session_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return None


async def _wait_for_health(client: "_ZaloBridgeClient", deadline: float,
                           process: Any = None) -> bool:
    import time
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        # Fail fast when the bridge process already died (bad cookie,
        # missing deps) instead of polling a corpse for the full window.
        if process is not None and process.returncode is not None:
            return False
        try:
            health = await client.health()
            if health.get("ready"):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False


def bridge_token_for_session(session_path: Path) -> str:
    """Bridge auth token shared by every process talking to the bridge.

    Prefers an explicit ``ZALO_BRIDGE_TOKEN``. That override exists because
    the session-hash fallback below is inherently racy: the bridge rewrites
    ``session.json`` right after login (and every 30 minutes afterwards to
    rotate cookies), so a token derived from the file's bytes changes under
    the running bridge. The bridge keeps the hash it computed at startup,
    the adapter recomputes a *different* one, and every authenticated call
    fails with 401 — silently, because ``/health`` needs no token, so the
    adapter still reports "attached" while ``/events`` never returns data.

    Set ZALO_BRIDGE_TOKEN (any long random string) in the environment for
    both the gateway and the bridge process; the hash path stays only as a
    fallback for setups that never set it.
    """
    explicit = _get_scoped_secret("ZALO_BRIDGE_TOKEN", "") or ""
    if explicit.strip():
        return explicit.strip()
    try:
        return hashlib.sha256(session_path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ZaloAdapter(BasePlatformAdapter):
    """Zalo personal-account gateway adapter (via the local zca-js bridge)."""

    def __init__(self, config, **kwargs):
        platform = Platform("zalo")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # NOTE: sender authorization (allowlist + pairing handshake) is owned
        # by the gateway's authz layer, which consults the registry entry's
        # ``allowed_users_env`` / ``allow_all_env`` (ZALO_ALLOWED_USERS /
        # ZALO_ALLOW_ALL_USERS) and the pairing store. The adapter forwards
        # every inbound message so unknown senders get the friendly pairing
        # code reply instead of a silent drop — no log-digging onboarding.

        # Bridge settings
        try:
            self.bridge_port = int(
                os.getenv("ZALO_BRIDGE_PORT") or extra.get("port", DEFAULT_BRIDGE_PORT)
            )
        except (TypeError, ValueError):
            self.bridge_port = DEFAULT_BRIDGE_PORT
        self.self_listen = _truthy_env(
            "ZALO_SELF_LISTEN", bool(extra.get("self_listen", False))
        )

        # Runtime state
        self._client: Optional[_ZaloBridgeClient] = None
        self._process: Optional[Any] = None  # asyncio subproces handle
        self._spawned = False  # True only when THIS adapter started the bridge
        self._poll_task: Optional[asyncio.Task] = None
        self._event_cursor = 0
        self._lock_key: Optional[str] = None

        # --- Access gate / dedup / audit state ---
        self._own_id: str = ""
        self._seen_msgs: "OrderedDict[str, None]" = OrderedDict()
        self._friends: Dict[str, Dict[str, str]] = {}
        self._friend_miss: Dict[str, Tuple[float, bool]] = {}
        self._friend_task: Optional[asyncio.Task] = None
        self._pending_codes: Dict[str, Tuple[str, str, float]] = {}
        self._pending_notified: Dict[str, float] = {}

        try:
            from hermes_constants import get_hermes_home
            hermes_home = Path(get_hermes_home())
        except Exception:
            hermes_home = Path.home() / ".hermes"
        self._session_path = hermes_home / "zalo" / "session.json"
        self._audit_path = self._session_path.parent / "audit.jsonl"
        self._dedup_path = self._session_path.parent / "seen.json"
        self._load_dedup_state()

        self._allowlist = _load_allowlist_store()(
            self._session_path.parent / "allowlist.json"
        )
        self._allowlist.ensure_file()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        node_bin = shutil.which("node")
        if not node_bin:
            self._set_fatal_error(
                "missing_dep",
                "Node.js >= 18 is required for the Zalo personal adapter "
                "(https://nodejs.org)",
                retryable=False,
            )
            return False

        err = compose_session_file(self._session_path)
        if err:
            self._set_fatal_error("config_missing", err, retryable=False)
            return False

        # One profile owns one Zalo account — hash the imei+session path so
        # two profiles can't drive the same login simultaneously.
        try:
            from gateway.status import acquire_scoped_lock
            lock_src = (
                (_get_scoped_secret("ZALO_IMEI") or "")
                + "|" + str(self._session_path)
            )
            tok_hash = hashlib.sha256(lock_src.encode()).hexdigest()[:16]
            # Returns (acquired, existing_holder). Testing the tuple itself is
            # always truthy, so the conflict must be read off the first element.
            acquired, _existing = acquire_scoped_lock(
                "zalo", tok_hash, metadata={"platform": "zalo"}
            )
            if not acquired:
                self._set_fatal_error(
                    "lock_conflict",
                    "Zalo account already in use by another profile",
                    retryable=False,
                )
                return False
            # Only record the key we actually hold — releasing on the failure
            # path would drop the other profile's lock.
            self._lock_key = tok_hash
        except ImportError:
            self._lock_key = None

        # Long-lived daemon model: attach to an already-running bridge when
        # one is healthy (gateway restart, reconnect watcher), so Zalo sees
        # ONE stable web session instead of a re-login on every restart —
        # frequent re-logins get the session kicked server-side.
        bridge_token = bridge_token_for_session(self._session_path)
        client = _ZaloBridgeClient(self.bridge_port, bridge_token)
        try:
            existing = await client.health()
        except Exception:
            existing = {}
        if existing.get("ready"):
            self._client = client
            self._spawned = False
            self._mark_connected()
            # Same gate bootstrap as the spawn path below: own_id feeds group
            # mention-gating and the friend cache backs the access gate.
            # Attaching skipped both before, which silently disabled the gate.
            self._own_id = str(existing.get("own_id") or "")
            if not self._own_id:
                logger.warning(
                    "Zalo: own_id is empty — group mention-gating will drop "
                    "every group message. Check the bridge /health endpoint."
                )
            await self._refresh_friends()
            self._friend_task = asyncio.create_task(self._friend_refresh_loop())
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info(
                "Zalo personal adapter: attached to running bridge on 127.0.0.1:%s",
                self.bridge_port,
            )
            return True

        bridge_dir = resolve_bridge_dir()
        node_modules = bridge_dir / "node_modules" / "zca-js"
        if not node_modules.exists():
            logger.info(
                "Zalo bridge: installing dependencies in %s (one-time, may take a minute)",
                bridge_dir,
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm" + (".cmd" if sys.platform == "win32" else ""),
                    "install", "--omit=dev", "--no-audit", "--no-fund",
                    cwd=str(bridge_dir),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), NPM_INSTALL_TIMEOUT_SECONDS
                )
                if proc.returncode != 0:
                    raise RuntimeError(
                        (stderr or b"").decode(errors="replace").strip()[:400]
                    )
            except Exception as exc:
                self._release_lock()
                self._set_fatal_error(
                    "npm_install_failed",
                    f"Could not install Zalo bridge dependencies: {exc}",
                    retryable=True,
                )
                return False

        # Per-spawn env: the derived token lets any same-session process
        # authenticate; the bridge falls back to deriving it identically.
        env = os.environ.copy()
        env.update({
            "PORT": str(self.bridge_port),
            "BRIDGE_TOKEN": bridge_token,
            "ZALO_SESSION_FILE": str(self._session_path),
        })
        if not self.self_listen:
            env.pop("ZALO_SELF_LISTEN", None)
        else:
            env["ZALO_SELF_LISTEN"] = "1"

        try:
            # Bridge outlives the gateway (attach-on-restart model) — its
            # stdio must not pipe back to a dying parent (EPIPE) and should
            # land somewhere the user can read: $HERMES_HOME/zalo/bridge.log
            bridge_log = open(self._session_path.parent / "bridge.log",
                              "ab", buffering=0)
            self._process = await asyncio.create_subprocess_exec(
                node_bin, "index.js",
                cwd=str(bridge_dir),
                env=env,
                stdout=bridge_log,
                stderr=bridge_log,
            )
        except Exception as exc:
            self._release_lock()
            self._set_fatal_error(
                "spawn_failed", f"Could not spawn Zalo bridge: {exc}", retryable=True
            )
            return False

        self._client = _ZaloBridgeClient(self.bridge_port, bridge_token)
        if not await _wait_for_health(self._client, HEALTH_TIMEOUT_SECONDS,
                                      process=self._process):
            stderr_tail = ""
            try:
                log_bytes = (self._session_path.parent / "bridge.log").read_bytes()
                stderr_tail = log_bytes[-2048:].decode(errors="replace")
            except OSError:
                pass
            await self._terminate_bridge()
            self._release_lock()
            self._client = None
            self._set_fatal_error(
                "bridge_unready",
                "Zalo bridge did not become ready in time."
                + (f" Last output: {stderr_tail.strip()[:300]}" if stderr_tail else ""),
                retryable=True,
            )
            return False

        self._spawned = True
        self._mark_connected()

        # own_id drives group mention-gating; the bridge exposes it on /health.
        try:
            health = await self._client.health()
            self._own_id = str(health.get("own_id") or "")
        except Exception:
            self._own_id = ""
        if not self._own_id:
            logger.warning(
                "Zalo: own_id is empty — group mention-gating will drop every "
                "group message. Check the bridge /health endpoint."
            )

        await self._refresh_friends()
        self._friend_task = asyncio.create_task(self._friend_refresh_loop())

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Zalo personal adapter: bridge ready on 127.0.0.1:%s (session %s)",
            self.bridge_port,
            self._session_path.name,
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        for attr in ("_poll_task", "_friend_task"):
            task = getattr(self, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, attr, None)
        await self._terminate_bridge()
        self._client = None
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("zalo", self._lock_key)
            except Exception:
                pass
            self._lock_key = None

    async def _terminate_bridge(self) -> None:
        # Attached bridges belong to another lifecycle (a previous gateway,
        # a manual daemon run) — leave them running so the Zalo session
        # stays warm; only reap what we spawned.
        if not self._spawned:
            return
        proc = self._process
        self._process = None
        self._spawned = False
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Access gate / audit
    # ------------------------------------------------------------------

    def _audit(self, verdict: str, *, direction: str = "in",
               sender: str = "", thread: str = "", thread_type: str = "",
               msg_id: str = "", text: str = "",
               extra: Optional[Dict[str, Any]] = None) -> None:
        """Append one JSONL audit record. Never raises.

        NOTE: this records "who asked what / what the bot replied". It does
        NOT capture which MCP tools ran — mcp-odoo only audits its write
        path, so read queries leave no trace there either.
        """
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "dir": direction,
            "verdict": verdict,
            "sender": sender,
            "thread": thread,
            "thread_type": thread_type,
            "msg_id": msg_id,
            "text": (text or "")[:AUDIT_MAX_TEXT],
        }
        if extra:
            record.update(extra)
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _owner_env(self) -> str:
        return _get_scoped_secret("ZALO_OWNER_ID", "") or ""

    def _is_admin(self, uid: str) -> bool:
        return self._allowlist.is_admin(uid, self._owner_env())

    async def _refresh_friends(self) -> None:
        """Reload the friend cache from the bridge."""
        client = self._client
        if not client:
            return
        try:
            data = await client.friends()
        except Exception as exc:
            logger.warning("Zalo: could not fetch friend list: %s", exc)
            return
        fresh: Dict[str, Dict[str, str]] = {}
        for entry in (data.get("friends") or []):
            fid = str(entry.get("id") or "")
            if fid:
                fresh[fid] = {
                    "name": str(entry.get("name") or ""),
                    "phone": str(entry.get("phone") or ""),
                }
        if fresh:
            self._friends = fresh
            self._friend_miss.clear()
            logger.info("Zalo: cached %d friends", len(fresh))

    async def _friend_refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FRIEND_REFRESH_SECONDS)
                await self._refresh_friends()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Zalo: friend refresh iteration failed",
                             exc_info=True)

    async def _is_friend(self, uid: str) -> bool:
        """Cache-first friendship check.

        A miss asks the bridge (someone who befriended the account after the
        last refresh). Both positive and negative results are cached briefly
        so a stranger sending many messages costs one API call, not one per
        message.
        """
        if not uid:
            return False
        if uid in self._friends:
            return True
        client = self._client
        if not client:
            return False
        now = time.monotonic()
        cached = self._friend_miss.get(uid)
        if cached and now - cached[0] < FRIEND_MISS_TTL_SECONDS:
            return cached[1]
        try:
            info = await client.user_info(uid)
        except Exception:
            return False  # fail-closed
        is_friend = bool(info.get("is_friend"))
        if is_friend:
            self._friends[uid] = {
                "name": str(info.get("name") or ""),
                "phone": str(info.get("phone") or ""),
            }
        if len(self._friend_miss) > FRIEND_MISS_CAP:
            self._friend_miss.clear()
        self._friend_miss[uid] = (now, is_friend)
        return is_friend

    async def _sender_allowed(self, sender_id: str, thread_id: str,
                              is_group: bool, *,
                              bypass_group_check: bool = False) -> bool:
        """Hard gate, evaluated before the event reaches the gateway.

        Unlike the gateway authz layer (which deliberately forwards every
        message so strangers receive a pairing code), unknown senders are
        dropped silently here.
        """
        if not sender_id:
            return False

        # 0. Admins always pass; the denylist must never lock them out.
        if self._is_admin(sender_id):
            return True

        # 1. Denylist beats everything else, friendship included.
        if self._allowlist.user_denied(sender_id):
            return False

        # 2. Manual allowlist.
        allowed = self._allowlist.user_allowed(sender_id)

        # 3. Friendship (only in "friends" mode).
        if not allowed and self._allowlist.mode == "friends":
            allowed = await self._is_friend(sender_id)

        if not allowed:
            return False

        # 4. Groups must be approved explicitly. `bypass_group_check` exists
        #    for /duyet-nhom: an admin has to run it INSIDE the unapproved
        #    group, which the check above would otherwise block forever.
        if is_group and not bypass_group_check:
            if not self._allowlist.group_allowed(thread_id):
                return False

        return True

    def _load_dedup_state(self) -> None:
        """Restore the seen-set and event cursor from disk.

        In-memory state alone does not survive a gateway restart, and the
        bridge deliberately outlives the gateway — so on restart the adapter
        re-reads the bridge's ring buffer from cursor 0 with an empty
        seen-set and answers every buffered message again. Observed in
        production: three identical messages replayed across three restarts,
        none flagged as duplicates.
        """
        try:
            data = json.loads(self._dedup_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        seen = data.get("seen")
        if isinstance(seen, list):
            for mid in seen[-SEEN_MSG_CAP:]:
                self._seen_msgs[str(mid)] = None
        try:
            self._event_cursor = max(0, int(data.get("cursor", 0)))
        except (TypeError, ValueError):
            self._event_cursor = 0
        if self._seen_msgs:
            logger.info(
                "Zalo: restored %d seen message ids, cursor=%d",
                len(self._seen_msgs), self._event_cursor,
            )

    def _save_dedup_state(self) -> None:
        """Persist the seen-set atomically. Never raises."""
        try:
            tmp = self._dedup_path.with_suffix(".json.tmp")
            self._dedup_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({
                    "cursor": self._event_cursor,
                    "seen": list(self._seen_msgs),
                }),
                encoding="utf-8",
            )
            tmp.replace(self._dedup_path)
        except OSError:
            pass

    def _is_duplicate(self, msg_id: str) -> bool:
        """LRU seen-set keyed by Zalo message id, persisted across restarts.

        Required because ``_poll_loop`` only advances ``_event_cursor``
        after a whole batch is handled, and a gateway restart against a
        still-running bridge resets the cursor to 0 — replaying the ring
        buffer.
        """
        if not msg_id:
            return False  # nothing to key on; let it through
        if msg_id in self._seen_msgs:
            return True
        self._seen_msgs[msg_id] = None
        if len(self._seen_msgs) > SEEN_MSG_CAP:
            self._seen_msgs.popitem(last=False)
        self._save_dedup_state()
        return False

    async def _notify_admins_pending(self, uid: str, text: str) -> None:
        """Tell admins a stranger messaged the bot, with an approval code."""
        targets = self._allowlist.notify_targets(self._owner_env())
        if not targets:
            return
        now = time.monotonic()
        last = self._pending_notified.get(uid, 0.0)
        if last and now - last < PENDING_NOTIFY_COOLDOWN_SECONDS:
            return
        self._pending_notified[uid] = now

        name, phone = uid, ""
        client = self._client
        if client:
            try:
                info = await client.user_info(uid)
                name = str(info.get("name") or uid)
                phone = str(info.get("phone") or "")
            except Exception:
                pass

        code = hashlib.sha1(
            f"{uid}:{int(now // 3600)}".encode("utf-8")
        ).hexdigest()[:4]
        self._pending_codes[code] = (uid, name, now)

        body = (
            "🔔 Người lạ nhắn bot\n"
            f"   Tên:  {name}\n"
            + (f"   SĐT:  {phone}\n" if phone else "")
            + f'   Nội dung: "{text[:80]}"\n\n'
            f"   Duyệt:  /duyet {code}\n"
            f"   Chặn:   /chan {code}\n"
            "   Bỏ qua: (không cần làm gì)"
        )
        for admin_id in targets:
            try:
                await self.send(format_chat_id(admin_id, "user"), body)
            except Exception as exc:
                logger.warning(
                    "Zalo: could not notify admin %s: %s", admin_id, exc
                )

    async def _reply(self, thread_id: str, thread_type: str,
                     message: str) -> None:
        await self.send(format_chat_id(thread_id, thread_type), message)

    async def _handle_admin_command(self, cmd: List[str], sender_id: str,
                                    thread_id: str, thread_type: str,
                                    is_group: bool) -> None:
        verb = cmd[0]

        if verb in {"/duyet", "/chan"}:
            if len(cmd) < 2:
                await self._reply(thread_id, thread_type,
                                  f"Cú pháp: {verb} <mã>")
                return
            entry = self._pending_codes.get(cmd[1])
            if not entry or time.monotonic() - entry[2] > PENDING_CODE_TTL_SECONDS:
                await self._reply(thread_id, thread_type,
                                  "Mã không đúng hoặc đã hết hạn.")
                return
            target_uid, target_name, _ = entry
            bucket = "allow" if verb == "/duyet" else "deny"
            self._allowlist.add("users", bucket, target_uid, target_name)
            self._pending_codes.pop(cmd[1], None)
            self._audit(
                "approved" if verb == "/duyet" else "blocked",
                sender=sender_id, thread=thread_id, thread_type=thread_type,
                extra={
                    "target_uid": target_uid,
                    "target_name": target_name,
                    "by_admin": self._allowlist.admin_name(sender_id),
                },
            )
            verb_vi = "duyệt" if verb == "/duyet" else "chặn"
            await self._reply(thread_id, thread_type,
                              f"✓ Đã {verb_vi} {target_name}.")
            return

        if verb == "/duyet-nhom":
            if not is_group:
                await self._reply(thread_id, thread_type,
                                  "Lệnh này chỉ dùng trong nhóm.")
                return
            group_name = thread_id
            client = self._client
            if client:
                try:
                    listing = await client.groups()
                    for group in (listing.get("groups") or []):
                        if str(group.get("id")) == thread_id:
                            group_name = str(group.get("name") or thread_id)
                            break
                except Exception:
                    pass
            self._allowlist.add("groups", "allow", thread_id, group_name)
            self._audit(
                "approved_group",
                sender=sender_id, thread=thread_id, thread_type=thread_type,
                extra={
                    "group_name": group_name,
                    "by_admin": self._allowlist.admin_name(sender_id),
                },
            )
            await self._reply(thread_id, "group",
                              f'✓ Đã duyệt nhóm "{group_name}".')
            return

        if verb == "/ai":
            await self._reply(thread_id, thread_type, self._render_who())
            return

    def _render_who(self) -> str:
        admins = self._allowlist.admin_ids(self._owner_env())
        lines = [f"👤 Admin ({len(admins)})"]
        if admins:
            lines.append(
                "   " + " · ".join(self._allowlist.admin_name(a) for a in admins)
            )
        else:
            lines.append("   (chưa có)")

        def render_section(icon: str, title: str, section: str,
                           bucket: str) -> List[str]:
            entries = self._allowlist.entries(section, bucket)
            out = [f"\n{icon} {title} ({len(entries)})"]
            for entry in entries[:20]:
                if isinstance(entry, dict):
                    label = str(entry.get("name") or entry.get("id") or "")
                else:
                    label = str(entry)
                out.append(f"   {label}")
            if len(entries) > 20:
                out.append(f"   … và {len(entries) - 20} nữa")
            return out

        lines += render_section("✅", "Cho phép thủ công", "users", "allow")
        lines += render_section("⛔", "Chặn", "users", "deny")
        lines += render_section("👥", "Nhóm", "groups", "allow")

        if self._allowlist.mode == "friends":
            lines.append(
                f"\nChế độ: bạn bè — {len(self._friends)} bạn bè đều có quyền"
            )
        else:
            lines.append(
                "\nChế độ: chỉ danh sách (bạn bè KHÔNG tự có quyền)"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Inbound polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        assert self._client is not None
        while True:
            try:
                data = await self._client.events(self._event_cursor)
                cursor = int(data.get("cursor", self._event_cursor))
                for evt in data.get("events", []):
                    try:
                        await self._handle_bridge_event(evt)
                    except Exception:
                        logger.exception("Zalo: failed handling bridge event")
                if cursor > self._event_cursor:
                    self._event_cursor = cursor
                    self._save_dedup_state()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Zalo poll failed: %s", exc)
                await asyncio.sleep(POLL_ERROR_BACKOFF_SECONDS)

    async def _handle_bridge_event(self, evt: Dict[str, Any]) -> None:
        if evt.get("kind") != "message":
            return
        thread_id = str(evt.get("thread_id") or "")
        thread_type = str(evt.get("thread_type") or "user")
        is_group = thread_type == "group"
        msg = evt.get("msg") or {}

        sender_id = str(msg.get("uid_from") or "")
        if evt.get("is_self"):
            # Self events only matter when the user explicitly opted in;
            # attribute them to the thread owner so gates still apply.
            sender_id = thread_id

        msg_id = str(msg.get("msg_id") or msg.get("cli_msg_id") or "")
        raw_text = str(msg.get("content") or "")

        def _log(verdict: str, **kwargs: Any) -> None:
            self._audit(verdict, sender=sender_id, thread=thread_id,
                        thread_type=thread_type, msg_id=msg_id,
                        text=raw_text, **kwargs)

        cmd = raw_text.strip().split()
        is_group_approve = bool(cmd) and cmd[0] == "/duyet-nhom"

        # ---- GATE 1: allowlist (drop before any expensive work) ----
        if not await self._sender_allowed(
            sender_id, thread_id, is_group,
            bypass_group_check=is_group_approve and self._is_admin(sender_id),
        ):
            _log("denied")
            if not is_group:
                await self._notify_admins_pending(sender_id, raw_text)
            return

        # ---- GATE 2: dedup ----
        if self._is_duplicate(msg_id):
            _log("dup")
            return

        # ---- GATE 3: mention gating in groups ----
        if is_group and _truthy_env("ZALO_REQUIRE_MENTION", True):
            honor_all = _truthy_env("ZALO_MENTION_ALL_COUNTS", False)
            if not mentions_self(msg, self._own_id,
                                 honor_mention_all=honor_all):
                # v1 drops ambient traffic. v2 could buffer it into the
                # session transcript so a later @mention has context.
                _log("ambient")
                return

        # ---- Admin commands (never reach the agent) ----
        if cmd and cmd[0] in ADMIN_COMMANDS:
            if not self._is_admin(sender_id):
                # Stay silent: do not reveal that these commands exist.
                _log("cmd_denied", extra={"cmd": cmd[0]})
                return
            _log("cmd", extra={"cmd": cmd[0]})
            await self._handle_admin_command(
                cmd, sender_id, thread_id, thread_type, is_group
            )
            return

        _log("allowed")

        # user_id stays RAW (no prefix): the gateway compares it against
        # ZALO_ALLOWED_USERS and the pairing store — only chat_id carries
        # the u/g thread-type prefix (outbound routing needs it).
        source = self.build_source(
            chat_id=format_chat_id(thread_id, thread_type),
            chat_type="group" if is_group else "dm",
            user_id=sender_id,
            user_name=sender_id,
            chat_name=thread_id,
        )
        mtype, text, media_urls, _labels = classify_inbound(msg)
        event = MessageEvent(
            text=text,
            message_type=mtype,
            source=source,
            raw_message=evt,
            message_id=str(msg.get("msg_id") or msg.get("cli_msg_id") or ""),
            media_urls=media_urls,
            media_types=[],
        )
        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        client = self._client
        if not client:
            return SendResult(success=False, error="Zalo adapter not connected")
        thread_id, thread_type = parse_chat_id(chat_id)
        sanitized_content = sanitize_technical_error(content)
        chunks = split_for_zalo(strip_markdown_preserving_urls(sanitized_content))
        last_msg_id: Optional[str] = None
        try:
            for chunk in chunks:
                result = await client.send_text(thread_id, thread_type, chunk)
                message = result.get("message") or {}
                mid = message.get("msgId")
                if mid:
                    last_msg_id = str(mid)
            self._audit("sent", direction="out", thread=thread_id,
                        thread_type=thread_type, msg_id=last_msg_id or "",
                        text=content, extra={"chunks": len(chunks)})
            return SendResult(success=True, message_id=last_msg_id)
        except Exception as exc:
            logger.error("Zalo send failed: %s", exc)
            self._audit("send_failed", direction="out", thread=thread_id,
                        thread_type=thread_type, text=content,
                        extra={"error": str(exc)[:200]})
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        client = self._client
        if not client or not chat_id:
            return
        thread_id, thread_type = parse_chat_id(chat_id)
        try:
            await client.send_typing(thread_id, thread_type)
        except Exception as exc:
            logger.debug("Zalo typing indicator failed: %s", exc)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        _tid, thread_type = parse_chat_id(chat_id)
        return {"name": chat_id, "type": "group" if thread_type == "group" else "dm"}

    def format_message(self, content: str) -> str:
        return strip_markdown_preserving_urls(content)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, image_path, caption or "")

    async def send_document(
        self,
        chat_id: str,
        document_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, document_path, caption or "")

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        preview_path: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, video_path, "")

    async def _send_media(self, chat_id: str, file_path: str,
                          caption: str) -> SendResult:
        client = self._client
        if not client:
            return SendResult(success=False, error="Zalo adapter not connected")
        path = Path(file_path)
        if not path.is_absolute() or not path.is_file():
            # The bridge attaches local paths on ITS filesystem — which is
            # the same machine by design; refuse anything else loudly.
            return SendResult(
                success=False,
                error=f"Zalo bridge needs an absolute existing local path: {file_path}",
            )
        thread_id, thread_type = parse_chat_id(chat_id)
        try:
            await client.send_media(thread_id, thread_type, [str(path)], caption)
            return SendResult(success=True, message_id=None)
        except Exception as exc:
            logger.error("Zalo media send failed: %s", exc)
            return SendResult(success=False, error=str(exc))


def _truthy_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Plugin entry-point hooks
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    return _truthy_env("ZALO_ENABLED", False)


def check_requirements() -> bool:
    """Passive gate: enabled flag, Node runtime, and a usable session."""
    if not _enabled():
        return False
    if not shutil.which("node"):
        return False
    if _get_scoped_secret("ZALO_COOKIE_JSON"):
        return True
    try:
        from hermes_constants import get_hermes_home
        session = Path(get_hermes_home()) / "zalo" / "session.json"
        return session.exists()
    except Exception:
        return False


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    if not (
        os.getenv("ZALO_ENABLED")
        or getattr(config, "enabled", False)
        or extra.get("enabled")
    ):
        return False
    if extra.get("cookie_json") or _get_scoped_secret("ZALO_COOKIE_JSON"):
        return True
    try:
        from hermes_constants import get_hermes_home
        return (Path(get_hermes_home()) / "zalo" / "session.json").exists()
    except Exception:
        return False


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig.extra from env-only setups (IRC/LINE pattern)."""
    if not _enabled():
        return None
    seeded: Dict[str, Any] = {}
    if os.getenv("ZALO_BRIDGE_PORT"):
        try:
            seeded["port"] = int(os.environ["ZALO_BRIDGE_PORT"])
        except ValueError:
            pass
    if os.getenv("ZALO_HOME_CHANNEL"):
        seeded["home_channel"] = os.environ["ZALO_HOME_CHANNEL"]
    return seeded or {}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process cron delivery — relays through the running bridge.

    Cron jobs often execute detached from the gateway; the bridge (and thus
    the live Zalo session) belongs to the gateway process, so we POST to it
    rather than spawning a second login. If the bridge isn't reachable we
    return an explicit error instead of attempting a second concurrent login
    on the same account (risk of tripping Zalo's security).
    """
    if not chat_id:
        return {"error": "Zalo standalone send: missing chat_id"}
    try:
        port = int(os.getenv("ZALO_BRIDGE_PORT") or DEFAULT_BRIDGE_PORT)
    except ValueError:
        port = DEFAULT_BRIDGE_PORT

    thread, thread_type = parse_chat_id(chat_id)
    try:
        from hermes_constants import get_hermes_home
        session_path = Path(get_hermes_home()) / "zalo" / "session.json"
    except Exception:
        session_path = Path.home() / ".hermes" / "zalo" / "session.json"
    token = bridge_token_for_session(session_path)
    headers = {"X-Bridge-Token": token} if token else {}

    import aiohttp
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        ) as session:
            async with session.post(
                f"http://127.0.0.1:{port}/send",
                headers=headers,
                json={"thread_id": thread, "thread_type": thread_type,
                      "msg": strip_markdown_preserving_urls(message or "")},
            ) as resp:
                if resp.status == 401:
                    return {"error": "Zalo bridge rejected the derived token "
                                     "(session file changed since the bridge started?)"}
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    err = data.get("error", "") if isinstance(data, dict) else ""
                    return {"error": f"Zalo bridge {resp.status}: {err}"}
                return {"success": True}
    except Exception as exc:
        return {"error": (
            f"Zalo bridge not reachable on 127.0.0.1:{port} ({exc}). "
            "Start the gateway so the Zalo session is live, then retry."
        )}


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup zalo``."""
    print()
    print("Zalo personal account setup")
    print("---------------------------")
    print("WARNING: this uses unofficial APIs against your PERSONAL Zalo")
    print("account. Zalo may suspend or lock it — use a SECONDARY account.")
    print()
    print("Two ways to authenticate:")
    print("  1) QR login (recommended):  cd", BRIDGE_DIR, "&& npm install && node index.js --qr-login")
    print("     Scan zalo-login-qr.png with the Zalo app; the session file is")
    print("     written automatically.")
    print("  2) Cookie export from chat.zalo.me (DevTools console):")
    print("     localStorage.getItem('z_uuid')  -> imei")
    print("     navigator.userAgent             -> user agent")
    print("     Export cookies with a browser extension -> JSON")
    print()

    try:
        from hermes_cli.config import (
            get_env_value as _get_env,
            save_env_value as _set_env,
        )
    except ImportError:
        print("hermes_cli.config unavailable; set ZALO_* vars in ~/.hermes/.env manually.")
        return

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        existing = _get_env(var) if callable(_get_env) else None
        # Show what's already saved so re-running setup is an edit session,
        # not a blind overwrite. Blank input always keeps the current value.
        if existing:
            suffix = f" [keep: {existing}]"
        else:
            suffix = ""
        try:
            if secret:
                from hermes_cli.secret_prompt import masked_secret_prompt
                value = masked_secret_prompt(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            _set_env(var, value)

    _prompt("ZALO_ENABLED", "Enable Zalo personal adapter? (true/false)")
    _prompt("ZALO_HOME_CHANNEL", "Home channel ID for cron delivery (blank=skip)")
    print()
    print("Access: anyone who messages the bot gets a pairing code; approve")
    print("with:  hermes pairing approve zalo <code>")
    print("(optional pre-approval: ZALO_ALLOWED_USERS=<id> in .env)")
    print()
    print("If you exported cookies instead of using QR login, also paste:")
    _prompt("ZALO_COOKIE_JSON", "Cookie JSON", secret=True)
    _prompt("ZALO_IMEI", "imei (z_uuid)", secret=True)
    _prompt("ZALO_USER_AGENT", "User agent")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="zalo",
        label="Zalo (Personal)",
        adapter_factory=lambda cfg: ZaloAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ZALO_ENABLED"],
        install_hint="Node.js >= 18 required; deps auto-install on first connect",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ZALO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ZALO_ALLOWED_USERS",
        allow_all_env="ZALO_ALLOW_ALL_USERS",
        max_message_length=ZALO_SAFE_CHARS,
        emoji="💠",
        pii_safe=False,
        allow_update_command=False,
        platform_hint=(
            "You are chatting via ZALO PERSONAL ACCOUNT (unofficial API). "
            "Messages do NOT render Markdown — write plain text; bare URLs "
            "are linkified. Chat ids carry a prefix: u<id>=direct message, "
            "g<id>=group. A new sender who messages the bot receives a "
            "pairing code the owner approves with 'hermes pairing approve "
            "zalo <code>'; paired users can talk in DMs and in any group "
            "the bot has joined. Media sending uses local absolute file "
            "paths on the same machine as the gateway. IMPORTANT: this "
            "integration uses an unofficial library against a personal "
            "account — avoid aggressive bulk actions (mass messaging/friend "
            "requests) that could get the account locked, and never "
            "reference the account's cookie/session contents in conversation."
        ),
    )
