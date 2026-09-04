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
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
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
# Attachment archive. Zalo's media URLs expire, and Hermes' own document cache
# is cleared after 24h and dropped once the turn ends — so a file referenced
# in a later message is gone. Keep our own copy, indexed per sender.
ATTACH_MAX_BYTES = 25 * 1024 * 1024
ATTACH_RETENTION_DAYS = 90
ATTACH_INDEX_PER_CHAT = 50
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
    # File shares carry a bare extension (fileExt), so list the common ones
    # explicitly — matching on the word "file" alone never fires for a .doc.
    (("image", "photo", "jpg", "jpeg", "png", "gif", "webp", "heic"),
     MessageType.PHOTO),
    (("video", "mp4", "mov", "avi", "mkv", "webm"), MessageType.VIDEO),
    (("voice", "audio", "mp3", "m4a", "aac", "wav", "ogg"), MessageType.VOICE),
    (("sticker",), MessageType.STICKER),
    (("file", "attach", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
      "pdf", "txt", "csv", "zip", "rar"), MessageType.DOCUMENT),
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


def strip_leading_mentions(msg: Dict[str, Any], text: str) -> str:
    """Remove @-mention prefixes so a command can be parsed from a group message.

    Addressing the bot in a group means the text arrives as
    ``"@Bot Name /duyet-nhom"``. Splitting that on whitespace yields ``"@Bot"``
    as the first token, so every admin command was unreachable in exactly the
    place ``/duyet-nhom`` has to be used.

    zca-js gives each mention a ``pos``/``len`` span over the raw text, so cut
    by span rather than guessing where a display name ends — names contain
    spaces, and Vietnamese names commonly contain several.
    """
    raw = msg.get("raw")
    spans = []
    if isinstance(raw, dict) and isinstance(raw.get("mentions"), list):
        for mention in raw["mentions"]:
            if not isinstance(mention, dict):
                continue
            try:
                pos = int(mention.get("pos"))
                length = int(mention.get("len"))
            except (TypeError, ValueError):
                continue
            if pos >= 0 and length > 0:
                spans.append((pos, pos + length))

    if spans:
        # Drop only mentions anchored at the start (allowing whitespace
        # between them); a mention inside the sentence is part of the message.
        cursor = 0
        for start, end in sorted(spans):
            if start > cursor and text[cursor:start].strip():
                break
            cursor = max(cursor, end)
        return text[cursor:].strip()

    # No usable spans (older payloads, or a client that omits them). Display
    # names contain spaces, so token-by-token trimming cannot tell where the
    # name ends. Only rescue the case that matters — a command somewhere after
    # a leading mention — and leave ordinary text untouched.
    stripped = text.strip()
    if not stripped.startswith("@"):
        return stripped
    tokens = stripped.split()
    for i, token in enumerate(tokens):
        if token.startswith("/"):
            return " ".join(tokens[i:])
    return stripped


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

        # File attachments arrive differently: `params` (singular) holding a
        # JSON *string* with fileExt/fileSize, and the download URL on `href`.
        # Reading only `parameters` classified every document as plain text
        # with no media URL, so nothing was ever archived.
        raw_params = content_obj.get("params")
        if isinstance(raw_params, str) and raw_params.strip():
            try:
                parsed = json.loads(raw_params)
                if isinstance(parsed, dict):
                    ext = str(parsed.get("fileExt") or "").lower()
                    if ext:
                        param_types.append(ext)
            except ValueError:
                pass

        blob = json.dumps(content_obj, ensure_ascii=False)
        urls = [u.rstrip(".,);") for u in _URL_RE.findall(blob)]

        # A plain photo carries neither `parameters` nor a fileExt: the CDN
        # link sits on href/thumb and inside params as rawUrl, leaving
        # param_types empty and the message classified as text with its image
        # dropped. Fall back to the extension in the URL path — query strings
        # and fragments stripped first, or ".jpg?foo" never matches.
        if not param_types:
            for url in urls:
                path = url.split("?", 1)[0].split("#", 1)[0]
                _, dot, ext = path.rpartition(".")
                if dot and 1 <= len(ext) <= 5 and ext.isalnum():
                    param_types.append(ext.lower())

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
        # mtype stays TEXT when no param type matched — and TEXT has no mime
        # entry, so indexing here used to raise KeyError and take down the
        # whole bridge-event handler, losing the message (attachments
        # included) rather than degrading to a plain-text event.
        mime = mime_by_type.get(mtype)
        return mtype, label, media_urls, [mime] if mime else []
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

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XLSX_MAX_ROWS_PER_SHEET = 500


def _xlsx_column_index(ref: str) -> int:
    """Turn a cell reference like ``BC12`` into a zero-based column index.

    Rows skip empty cells entirely, so without this a row whose first value
    sits in column C would render shifted two columns left and silently
    misalign against its header.
    """
    match = re.match(r"([A-Z]+)", ref or "")
    if not match:
        return 0
    n = 0
    for ch in match.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx_to_text(path: Path) -> str:
    """Render a workbook as tab-separated text, using only the stdlib.

    openpyxl is not in the Hermes venv and this must not add a dependency.
    An .xlsx is a zip of XML, so the sheets are readable directly — the same
    approach the .docx branch already takes.

    Two encodings of a string cell exist and both appear in the wild:
    ``t="s"`` indexes into xl/sharedStrings.xml, while ``t="inlineStr"`` holds
    the text inside the cell. The file that prompted this had no shared-strings
    part at all, so handling only the first would have produced empty output.
    """
    import zipfile as zf
    from xml.etree import ElementTree

    chunks: List[str] = []
    with zf.ZipFile(path) as archive:
        names = set(archive.namelist())

        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(si.itertext()) for si in root.iter(f"{_XLSX_NS}si")]

        # Sheet display names, in workbook order — "Bảng giá" is worth more to
        # the reader than "sheet3.xml".
        titles: List[str] = []
        if "xl/workbook.xml" in names:
            wb = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            titles = [s.get("name", "") for s in wb.iter(f"{_XLSX_NS}sheet")]

        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        for index, sheet_name in enumerate(sheets):
            root = ElementTree.fromstring(archive.read(sheet_name))
            title = titles[index] if index < len(titles) else sheet_name.rsplit("/", 1)[-1]
            rows: List[str] = []
            truncated = False
            for row in root.iter(f"{_XLSX_NS}row"):
                cells: Dict[int, str] = {}
                for cell in row.iter(f"{_XLSX_NS}c"):
                    kind = cell.get("t")
                    value_node = cell.find(f"{_XLSX_NS}v")
                    if kind == "s" and value_node is not None and (value_node.text or "").isdigit():
                        idx = int(value_node.text)
                        value = shared[idx] if idx < len(shared) else ""
                    elif kind == "inlineStr":
                        inline = cell.find(f"{_XLSX_NS}is")
                        value = "".join(inline.itertext()) if inline is not None else ""
                    else:
                        value = value_node.text if value_node is not None else ""
                    value = (value or "").strip()
                    if value:
                        cells[_xlsx_column_index(cell.get("r", ""))] = value
                if cells:
                    width = max(cells) + 1
                    rows.append("\t".join(cells.get(i, "") for i in range(width)))
                if len(rows) >= XLSX_MAX_ROWS_PER_SHEET:
                    truncated = True
                    break
            if rows:
                if truncated:
                    rows.append(
                        f"[... còn nữa, chỉ lấy {XLSX_MAX_ROWS_PER_SHEET} dòng đầu]"
                    )
                chunks.append(f"=== {title} ===\n" + "\n".join(rows))
    return "\n\n".join(chunks)


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
        # code -> (kind, target_id, display_name, issued_at); kind is
        # "user" or "group" so an approval code cannot be redeemed
        # by the wrong command.
        self._pending_codes: Dict[str, Tuple[str, str, str, float]] = {}
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
            self._sync_gateway_allowlist()
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
        self._sync_gateway_allowlist()

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

    async def _archive_attachment(self, url: str, msg: Dict[str, Any],
                                  sender_id: str, thread_id: str,
                                  thread_type: str) -> Optional[Dict[str, Any]]:
        """Download an inbound attachment and record it against the chat.

        Zalo's media URLs are short-lived and Hermes discards its document
        cache once the turn ends, so "the invoice I sent this morning" cannot
        be resolved later. Store the bytes ourselves plus an index the skill
        can consult on a subsequent turn.
        """
        try:
            import aiohttp
        except ImportError:
            return None

        base = self._session_path.parent / "attachments"
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        folder = base / day
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        # The display name lives in content_obj.title. The previous version
        # said so but then read msg["raw"]["title"] — content_obj is a SIBLING
        # of raw, not nested inside it — so a document arrived as a bare hash
        # with no name and no extension. That in turn made _extract_text bail
        # immediately, since it dispatches on Path(name).suffix.
        title = str(msg.get("title") or "").strip()
        content_obj = msg.get("content_obj")
        if not title and isinstance(content_obj, dict):
            title = str(content_obj.get("title") or "").strip()
        raw = msg.get("raw")
        if not title and isinstance(raw, dict):
            title = str(raw.get("title") or "").strip()

        # Zalo puts the real extension in content_obj.params — a JSON *string*
        # carrying fileExt/fileSize (classify_inbound already parses it for
        # type detection). It is the only reliable source: the title may be
        # missing entirely, and when present it does not always carry a suffix.
        file_ext = ""
        if isinstance(content_obj, dict):
            raw_params = content_obj.get("params")
            if isinstance(raw_params, str) and raw_params.strip():
                try:
                    parsed = json.loads(raw_params)
                    if isinstance(parsed, dict):
                        file_ext = re.sub(
                            r"[^\w]", "", str(parsed.get("fileExt") or "")
                        ).lower()[:12]
                except ValueError:
                    pass

        # Keep the original name when it is safe; it is what the user will
        # refer to ("cái file bảng giá").
        safe = re.sub(r"[^\w.\-]", "_", title)[:80] if title else ""
        name = f"{digest}_{safe}" if safe else digest
        # Always end up with a suffix, even when the title was missing or
        # already carried one — extraction and every downstream reader key off
        # it. Without this the file is stored correctly and is still unusable.
        if file_ext and not name.lower().endswith(f".{file_ext}"):
            name = f"{name}.{file_ext}"
        path = folder / name

        if not path.exists():
            try:
                timeout = aiohttp.ClientTimeout(total=60)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            return None
                        size = 0
                        with open(path, "wb") as handle:
                            async for chunk in resp.content.iter_chunked(65536):
                                size += len(chunk)
                                if size > ATTACH_MAX_BYTES:
                                    handle.close()
                                    path.unlink(missing_ok=True)
                                    logger.info(
                                        "Zalo: attachment over %d bytes, skipped",
                                        ATTACH_MAX_BYTES,
                                    )
                                    return None
                                handle.write(chunk)
            except Exception as exc:
                logger.warning("Zalo: attachment download failed: %s", exc)
                path.unlink(missing_ok=True)
                return None

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "path": str(path),
            "name": title or name,
            "sender": sender_id,
            "thread": thread_id,
            "thread_type": thread_type,
            "msg_id": str(msg.get("msg_id") or ""),
            "size": path.stat().st_size if path.exists() else 0,
            "text_path": None,
        }

        # Extract text now, while the file arrives. Doing it lazily would push
        # the agent into shell commands — python3 -c with zipfile — which trip
        # the approval prompt on every read and cannot parse legacy .doc (OLE2,
        # not zip) anyway. antiword/pdftotext are cheap and read-only.
        text_path = self._extract_text(path, name)
        if text_path:
            record["text_path"] = str(text_path)

        self._append_attachment_index(thread_id, record)
        self._prune_attachments(base)
        return record

    def _extract_text(self, path: Path, name: str) -> Optional[Path]:
        """Best-effort text extraction alongside the stored file.

        Returns None when nothing suitable exists — the file is still stored
        and usable; only its text is unavailable. Extraction failures are not
        fatal and must never block archiving.
        """
        suffix = Path(name).suffix.lower()
        # antiword handles legacy .doc; pdftotext covers .pdf. Office XML
        # formats (.docx/.xlsx) are zip archives — read the main part with
        # stdlib rather than shelling out.
        commands = {
            ".doc": ["antiword", str(path)],
            ".pdf": ["pdftotext", "-q", str(path), "-"],
        }
        try:
            if suffix == ".docx":
                import zipfile as zf
                from xml.etree import ElementTree
                with zf.ZipFile(path) as archive:
                    blob = archive.read("word/document.xml")
                root = ElementTree.fromstring(blob)
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                text = "\n".join(
                    "".join(node.itertext())
                    for node in root.iter(f"{ns}p")
                )
            elif suffix in (".xlsx", ".xlsm"):
                text = _xlsx_to_text(path)
                if not text:
                    return None
            elif suffix == ".doc" or (suffix == ".pdf"
                                      and shutil.which("pdftotext")):
                cmd = commands.get(suffix)
                if not cmd or not shutil.which(cmd[0]):
                    return None
                result = subprocess.run(
                    cmd, capture_output=True, timeout=30,
                    stdin=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    return None
                text = result.stdout.decode("utf-8", errors="replace")
            else:
                return None
        except Exception as exc:
            logger.debug("Zalo: text extraction failed for %s: %s", name, exc)
            return None

        text = (text or "").strip()
        if not text:
            return None
        try:
            out = path.with_suffix(path.suffix + ".txt")
            out.write_text(text, encoding="utf-8")
            return out
        except OSError:
            return None

    def _attachment_index_path(self) -> Path:
        return self._session_path.parent / "attachments" / "index.json"

    def _append_attachment_index(self, thread_id: str,
                                 record: Dict[str, Any]) -> None:
        """Index per chat, newest first, bounded. Never raises."""
        idx_path = self._attachment_index_path()
        try:
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        entries = data.get(thread_id) or []
        entries = [e for e in entries if e.get("msg_id") != record["msg_id"]]
        entries.insert(0, record)
        data[thread_id] = entries[:ATTACH_INDEX_PER_CHAT]
        try:
            tmp = idx_path.with_suffix(".json.tmp")
            idx_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(idx_path)
        except OSError:
            pass

    def _prune_attachments(self, base: Path) -> None:
        """Drop day-folders past the retention window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=ATTACH_RETENTION_DAYS)
        try:
            for folder in base.iterdir():
                if not folder.is_dir():
                    continue
                try:
                    day = datetime.strptime(folder.name, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                if day < cutoff:
                    for child in folder.iterdir():
                        child.unlink(missing_ok=True)
                    folder.rmdir()
        except OSError:
            pass

    def _recent_attachments(self, thread_id: str, limit: int = 5) -> str:
        """A short note listing files this chat sent, for the agent's context.

        Paths are included so the agent can open them; the skill decides
        whether that is appropriate. Kept terse — this rides along with every
        message in a chat that has ever sent a file.
        """
        try:
            data = json.loads(self._attachment_index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        entries = (data.get(thread_id) or [])[:limit] if isinstance(data, dict) else []
        if not entries:
            return ""
        lines = ["[Tệp đã gửi trong cuộc trò chuyện này:]"]
        for e in entries:
            when = str(e.get("ts", ""))[:16].replace("T", " ")
            size_kb = max(1, int(e.get("size", 0)) // 1024)
            # When extracted text exists, point at it: reading that file is a
            # plain read, whereas opening the original .doc/.pdf would push
            # the agent into shell commands and approval prompts.
            target = e.get("text_path") or e.get("path")
            kind = "nội dung" if e.get("text_path") else "file gốc"
            lines.append(
                f"- {e.get('name')} ({size_kb}KB, {when}; {kind}: {target})"
            )
        return "\n".join(lines)

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

    async def _refresh_own_id(self) -> None:
        """Re-read own_id from the bridge, in case the account changed.

        ``--qr-web`` re-logins the RUNNING bridge into a different Zalo account
        without a restart. The bridge then reports the new own_id, but this
        adapter captured its copy once at connect and kept it — so
        ``mentions_self`` compares an incoming mention against the *previous*
        account's id, never matches, and every group message is filed as
        ``ambient``. Direct messages keep working, which makes it look like the
        bot is fine and merely ignoring the group.

        Observed live: bridge on 699758145934526126, adapter still holding
        638527951485115695 from a session ten hours older.
        """
        client = self._client
        if not client:
            return
        try:
            health = await client.health()
        except Exception as exc:
            logger.debug("Zalo: own_id refresh failed: %s", exc)
            return
        fresh = str(health.get("own_id") or "")
        if not fresh or fresh == self._own_id:
            return
        logger.warning(
            "Zalo: bridge account changed (own_id %s -> %s); group "
            "mention-gating now follows the new account. Access lists are "
            "keyed by uid and a uid is relative to the observing account, so "
            "allowlist.json almost certainly needs re-granting.",
            self._own_id or "(empty)", fresh,
        )
        self._own_id = fresh

    async def _friend_refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FRIEND_REFRESH_SECONDS)
                await self._refresh_own_id()
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

    def _sync_gateway_allowlist(self) -> None:
        """Mirror allowlist.json into ZALO_ALLOWED_USERS.

        There are two independent gates. This adapter's runs first and is the
        one operators manage (`/duyet`, `zalo_allow.py`, allowlist.json). The
        gateway's own authz layer runs afterwards and reads the registry's
        ``allowed_users_env`` — ZALO_ALLOWED_USERS — which knows nothing about
        any of that.

        A user approved here but absent from that variable therefore passes
        this gate, is logged ``allowed``, and is then dropped by the gateway
        with "Unauthorized user" — a silent failure whose only trace is in a
        log nobody reads. Observed in production after approving a colleague.

        Rather than teach operators to edit two places, publish the union
        (admins + allowed users) into the env the gateway reads. The denylist
        is honoured by omission: this adapter has already refused those
        senders before the gateway is reached.
        """
        allowed = set(self._allowlist.admin_ids(self._owner_env()))
        for entry in self._allowlist.entries("users", "allow"):
            uid = entry.get("id") if isinstance(entry, dict) else entry
            if uid:
                allowed.add(str(uid))

        # In "friends" mode any friend may talk to the bot, so the env
        # allowlist cannot enumerate them — include the cached friends too.
        if self._allowlist.mode == "friends":
            allowed.update(self._friends)

        if not allowed:
            return
        current = {p for p in os.environ.get("ZALO_ALLOWED_USERS", "").split(",") if p}
        if current != allowed:
            os.environ["ZALO_ALLOWED_USERS"] = ",".join(sorted(allowed))
            logger.info(
                "Zalo: synced %d sender(s) into ZALO_ALLOWED_USERS", len(allowed)
            )

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
        if is_group and not self._allowlist.group_allowed(thread_id):
            # The bypass exists so an admin can run /duyet-nhom INSIDE a group
            # that is not approved yet — without it, approving a group is
            # impossible. Re-check admin here rather than trusting the flag:
            # a caller that sets it for a non-admin would otherwise open every
            # unapproved group to that user.
            if not (bypass_group_check and self._is_admin(sender_id)):
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
        self._pending_codes[code] = ("user", uid, name, now)

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

    async def _group_name(self, thread_id: str) -> str:
        """Resolve a group's display name; falls back to its id."""
        client = self._client
        if not client:
            return thread_id
        try:
            listing = await client.groups()
            for group in (listing.get("groups") or []):
                if str(group.get("id")) == thread_id:
                    return str(group.get("name") or thread_id)
        except Exception:
            pass
        return thread_id

    async def _notify_admins_group(self, thread_id: str, sender_id: str,
                                   text: str) -> None:
        """Ask admins to approve a group they may not be a member of.

        ``/duyet-nhom`` has to be typed inside the group, which assumes an
        admin is in it. Often they are not — someone adds the bot to a team
        chat the admin never joins, and approval becomes impossible without
        first getting invited. This routes the request to their DM instead.
        """
        targets = self._allowlist.notify_targets(self._owner_env())
        if not targets:
            return
        now = time.monotonic()
        key = f"group:{thread_id}"
        if now - self._pending_notified.get(key, 0.0) < PENDING_NOTIFY_COOLDOWN_SECONDS:
            return
        self._pending_notified[key] = now

        group_name = await self._group_name(thread_id)
        who = sender_id
        if sender_id in self._friends:
            who = self._friends[sender_id].get("name") or sender_id
        elif self._client:
            try:
                info = await self._client.user_info(sender_id)
                who = str(info.get("name") or sender_id)
            except Exception:
                pass

        code = hashlib.sha1(
            f"g:{thread_id}:{int(now // 3600)}".encode("utf-8")
        ).hexdigest()[:4]
        self._pending_codes[code] = ("group", thread_id, group_name, now)

        body = (
            "🔔 Nhóm chưa được duyệt\n"
            f"   Nhóm:    {group_name}\n"
            f"   Người hỏi: {who}\n"
            f'   Nội dung: "{text[:80]}"\n\n'
            f"   Duyệt:   /duyet-nhom {code}\n"
            "   Bỏ qua:  (không cần làm gì)"
        )
        for admin_id in targets:
            try:
                await self.send(format_chat_id(admin_id, "user"), body)
            except Exception as exc:
                logger.warning(
                    "Zalo: could not notify admin %s about group %s: %s",
                    admin_id, thread_id, exc,
                )

    async def _reply(self, thread_id: str, thread_type: str,
                     message: str) -> None:
        await self.send(format_chat_id(thread_id, thread_type), message)

    async def _handle_admin_command(self, cmd: List[str], sender_id: str,
                                    thread_id: str, thread_type: str,
                                    is_group: bool) -> None:
        # Defence in depth: the caller checks this too, but every command below
        # mutates the allowlist, so the guard belongs where the mutation is
        # rather than only at the one site that happens to call it today.
        if not self._is_admin(sender_id):
            self._audit("cmd_denied", sender=sender_id, thread=thread_id,
                        thread_type=thread_type, extra={"cmd": cmd[0]})
            return

        verb = cmd[0]

        if verb in {"/duyet", "/chan"}:
            if len(cmd) < 2:
                await self._reply(thread_id, thread_type,
                                  f"Cú pháp: {verb} <mã>")
                return
            entry = self._pending_codes.get(cmd[1])
            # A group code must not be usable here: it would file the group id
            # under users and quietly grant it nothing while looking like it
            # worked.
            if (not entry or entry[0] != "user"
                    or time.monotonic() - entry[3] > PENDING_CODE_TTL_SECONDS):
                await self._reply(thread_id, thread_type,
                                  "Mã không đúng hoặc đã hết hạn.")
                return
            _kind, target_uid, target_name, _ts = entry
            bucket = "allow" if verb == "/duyet" else "deny"
            self._allowlist.add("users", bucket, target_uid, target_name)
            self._pending_codes.pop(cmd[1], None)
            self._sync_gateway_allowlist()
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
            # With a code: approve remotely, from the admin's DM. Without one:
            # approve the group the command was typed in.
            if len(cmd) > 1:
                entry = self._pending_codes.get(cmd[1])
                if (not entry or entry[0] != "group"
                        or time.monotonic() - entry[3] > PENDING_CODE_TTL_SECONDS):
                    await self._reply(thread_id, thread_type,
                                      "Mã không đúng hoặc đã hết hạn.")
                    return
                _kind, target_id, group_name, _ts = entry
                self._allowlist.add("groups", "allow", target_id, group_name)
                self._pending_codes.pop(cmd[1], None)
                self._audit(
                    "approved_group",
                    sender=sender_id, thread=target_id, thread_type="group",
                    extra={
                        "group_name": group_name,
                        "by_admin": self._allowlist.admin_name(sender_id),
                        "remote": True,
                    },
                )
                await self._reply(thread_id, thread_type,
                                  f'✓ Đã duyệt nhóm "{group_name}".')
                return

            if not is_group:
                await self._reply(
                    thread_id, thread_type,
                    "Lệnh này dùng trong nhóm, hoặc kèm mã từ thông báo: "
                    "/duyet-nhom <mã>",
                )
                return
            group_name = await self._group_name(thread_id)
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
                if cursor < self._event_cursor:
                    # The bridge restarted: its ring buffer and cursor reset
                    # to 0 while ours persisted. Holding the old value would
                    # silently drop every event until the new bridge caught
                    # up to it — the first N messages after a re-login would
                    # never reach the agent. Follow it back down; msg_id
                    # dedup still suppresses anything already handled.
                    logger.info(
                        "Zalo: bridge cursor reset (%d -> %d); realigning",
                        self._event_cursor, cursor,
                    )
                    self._event_cursor = cursor
                    self._save_dedup_state()
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

        # In a group the text starts with the @-mention that addressed the
        # bot, so commands must be read after it.
        cmd = strip_leading_mentions(msg, raw_text).split()
        is_group_approve = bool(cmd) and cmd[0] == "/duyet-nhom"

        # ---- GATE 1: allowlist (drop before any expensive work) ----
        if not await self._sender_allowed(
            sender_id, thread_id, is_group,
            bypass_group_check=is_group_approve and self._is_admin(sender_id),
        ):
            _log("denied")
            if is_group:
                # Only worth surfacing when the sender themselves is allowed —
                # otherwise the group is not the thing being refused, and any
                # stranger could page the admin by messaging a random group.
                if await self._sender_allowed(sender_id, thread_id, False):
                    await self._notify_admins_group(thread_id, sender_id, raw_text)
            else:
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
                # Zalo sends an attachment as its own message, with no text
                # and therefore no mention — the caption arrives separately.
                # Dropping it here means "@Bot đọc file này" refers to
                # something that was never archived. Keep the file, skip the
                # agent: the sender already passed the gate above.
                _mtype, _t, _urls, _lbl = classify_inbound(msg)
                if _urls:
                    for media_url in _urls:
                        record = await self._archive_attachment(
                            media_url, msg, sender_id, thread_id, thread_type
                        )
                        if record:
                            _log("attachment", extra={
                                "file": record["name"],
                                "size": record["size"],
                                "untagged": True,
                            })
                    return
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

        # Keep the gateway's own allowlist in step before handing the event
        # over — it re-checks the sender against ZALO_ALLOWED_USERS and drops
        # anyone missing from it, however this gate ruled.
        self._sync_gateway_allowlist()

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

        # Archive before dispatch: the URL is already expiring, and the agent
        # may need the file again on a later turn.
        for media_url in media_urls:
            record = await self._archive_attachment(
                media_url, msg, sender_id, thread_id, thread_type
            )
            if record:
                _log("attachment", extra={
                    "file": record["name"], "size": record["size"],
                })
        # Tell the agent what this chat has sent before. Without it the file
        # exists on disk but the model has no idea it can be referred to.
        recent = self._recent_attachments(thread_id)
        if recent and text:
            text = f"{text}\n\n{recent}"

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

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[List[str]],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render the clarify prompt in Vietnamese.

        The base implementation hardcodes its instruction line in English
        (``gateway/platforms/base.py``: "Reply with the number, the option
        text, or your own answer."). It is a literal, not an i18n lookup, so
        adding a locale file cannot reach it — and Hermes ships no ``vi``
        locale anyway. The base docstring names overriding this method as the
        supported extension point, so the string lives here rather than in a
        patch to core.

        Everything else follows the base contract: keep the ❓ prefix and the
        numbered list, and call ``mark_awaiting_text`` for the multiple-choice
        path so the gateway's text-intercept captures a typed reply. Zalo has
        no button UI, so both modes are plain text.
        """
        if choices:
            is_multi = False
            try:
                from tools import clarify_gateway as _cg

                with _cg._lock:
                    entry = _cg._entries.get(clarify_id)
                is_multi = bool(entry and getattr(entry, "multi_select", False))
            except Exception:
                is_multi = False

            lines = [f"❓ {question}", ""]
            for i, choice in enumerate(choices, start=1):
                lines.append(f"  {i}. {choice}")
            lines.append("")
            if is_multi:
                lines.append(
                    "Anh/chị chọn nhiều mục được ạ — trả lời bằng các số cách "
                    'nhau bởi dấu phẩy (ví dụ "1, 3"), hoặc gõ nội dung lựa '
                    "chọn, hoặc câu trả lời riêng của anh/chị."
                )
            else:
                lines.append(
                    "Anh/chị trả lời bằng số thứ tự, hoặc gõ nội dung lựa "
                    "chọn, hoặc câu trả lời riêng của anh/chị ạ."
                )
            text = "\n".join(lines)

            from tools.clarify_gateway import mark_awaiting_text

            mark_awaiting_text(clarify_id)
        else:
            text = f"❓ {question}"

        return await self.send(chat_id, text, metadata=metadata)

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
