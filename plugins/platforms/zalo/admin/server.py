#!/usr/bin/env python3
"""Web admin panel for the Zalo bot.

Everything here is a UI over machinery that already exists:

  * the bridge's loopback HTTP API — ``/health``, ``/own-id``, ``/friends``,
    ``/groups``, ``/qr/start``, ``/qr/status``
  * ``allowlist_store.AllowlistStore`` — the same reader/writer the adapter
    uses, so an edit made here is picked up by a running gateway within its
    5-second hot-reload window. No restart, no second source of truth.
  * ``$HERMES_HOME/zalo/audit.jsonl``

It deliberately does NOT re-implement any of that.

SECURITY POSTURE
----------------
This panel grants and revokes access to the bot and can start a QR login —
anyone who reaches it can take over the bot's Zalo account. It therefore:

  * binds 127.0.0.1 by default and expects a TLS-terminating reverse proxy
    in front of it (see deploy/admin/Caddyfile.example);
  * REFUSES to bind a non-loopback address at all. Hermes' own dashboard made
    the same call in its June 2026 hardening, where ``--insecure`` became a
    no-op. Terminate TLS at the proxy; do not expose this process directly.
  * requires a password (scrypt, stdlib — no new dependency) and will not
    start without one;
  * issues an HMAC-signed session cookie, HttpOnly + SameSite=Strict, and
    requires a CSRF token on every state-changing request;
  * throttles failed logins per client.

Set the password once:

    python server.py --set-password

Run:

    python server.py                    # 127.0.0.1:8648
    python server.py --port 8648
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # allowlist_store lives one level up

# Under a scheduled task, stdout/stderr are redirected files opened in the
# console codepage (cp1252 here), so a single non-ASCII character in a log
# line kills the process at startup with UnicodeEncodeError. Every message in
# this app is Vietnamese, so force UTF-8 rather than policing the strings.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import httpx  # noqa: E402
from fastapi import Body, FastAPI, Form, Request  # noqa: E402
from fastapi.responses import (  # noqa: E402
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates  # noqa: E402

from allowlist_store import AllowlistStore  # noqa: E402

# ── configuration ──────────────────────────────────────────────────────────

COOKIE = "zalo_admin"
SESSION_TTL = 8 * 3600
LOGIN_WINDOW = 300
LOGIN_MAX_FAILS = 8

PASSWORD_ENV = "ZALO_ADMIN_PASSWORD_HASH"
SECRET_ENV = "ZALO_ADMIN_SECRET"

# n=2**15, r=8 needs ~32 MB, which is exactly OpenSSL's default maxmem --
# it raises "memory limit exceeded" unless maxmem is raised explicitly.
SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "hermes"
    return Path.home() / ".hermes"


def env_file() -> Path:
    return hermes_home() / ".env"


def env_value(name: str) -> str:
    """Read from the process env, falling back to $HERMES_HOME/.env.

    Same resolution order as zalo_allow.py so the two agree about which
    bridge token and which profile they are talking about.
    """
    if v := os.environ.get(name):
        return v.strip()
    path = env_file()
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(rf"^{re.escape(name)}=(.*)$", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def bridge_base() -> str:
    port = env_value("ZALO_BRIDGE_PORT") or "8647"
    return f"http://127.0.0.1:{port}"


def store() -> AllowlistStore:
    s = AllowlistStore(hermes_home() / "zalo" / "allowlist.json")
    s.ensure_file()
    return s


# ── password + session ─────────────────────────────────────────────────────

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$", 2)
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(dk_b64)
    except Exception:
        return False
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=len(expect), **SCRYPT)
    return hmac.compare_digest(dk, expect)


def _secret() -> bytes:
    v = env_value(SECRET_ENV)
    if not v:
        # Derived, not random: a restart must not silently log everyone out,
        # and there is no shared store to keep a random key in.
        v = hashlib.sha256(
            (env_value(PASSWORD_ENV) + str(hermes_home())).encode()
        ).hexdigest()
    return v.encode()


def sign_session(issued: int, csrf: str) -> str:
    payload = base64.urlsafe_b64encode(f"{issued}:{csrf}".encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def read_session(raw: str) -> Optional[Tuple[int, str]]:
    if not raw or "." not in raw:
        return None
    payload, _, sig = raw.rpartition(".")
    expect = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        pad = "=" * (-len(payload) % 4)
        issued_s, _, csrf = base64.urlsafe_b64decode(payload + pad).decode().partition(":")
        issued = int(issued_s)
    except Exception:
        return None
    if time.time() - issued > SESSION_TTL:
        return None
    return issued, csrf


_fails: Dict[str, List[float]] = defaultdict(list)


def throttled(client: str) -> bool:
    now = time.time()
    hits = [t for t in _fails[client] if now - t < LOGIN_WINDOW]
    _fails[client] = hits
    return len(hits) >= LOGIN_MAX_FAILS


def record_fail(client: str) -> None:
    _fails[client].append(time.time())


# ── bridge access ──────────────────────────────────────────────────────────

async def bridge(path: str, method: str = "GET", json_body: Any = None) -> Tuple[bool, Any]:
    """Call the bridge. Returns (ok, payload-or-error-string)."""
    token = env_value("ZALO_BRIDGE_TOKEN")
    headers = {"X-Bridge-Token": token} if token else {}
    url = bridge_base() + path
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.request(method, url, headers=headers, json=json_body)
        if r.status_code >= 400:
            return False, f"bridge {r.status_code}: {r.text[:200]}"
        ctype = r.headers.get("content-type", "")
        return True, (r.json() if "json" in ctype else r.text)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _entry_ids(st: AllowlistStore, section: str, bucket: str) -> set:
    return {str(e.get("id") if isinstance(e, dict) else e) for e in st.entries(section, bucket)}


def audit_senders() -> Dict[str, Dict[str, Any]]:
    """Everyone who has ever messaged the bot, from audit.jsonl.

    The friend list alone is not enough to administer access: the people who
    most need granting are precisely the ones who messaged and were refused,
    and a refused stranger is by definition not a friend. audit.jsonl records
    the sender uid on every inbound event, so it is the only complete roster
    of "who has actually turned up".

    It stores no display name, so names come from the bridge later.
    """
    path = hermes_home() / "zalo" / "audit.jsonl"
    seen: Dict[str, Dict[str, Any]] = {}
    if not path.is_file():
        return seen
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("dir") != "in":
                continue
            uid = str(d.get("sender") or "")
            if not uid:
                continue
            rec = seen.setdefault(uid, {"count": 0, "last": "", "last_text": ""})
            rec["count"] += 1
            ts = str(d.get("ts") or "")
            if ts > rec["last"]:
                rec["last"] = ts
                rec["last_text"] = str(d.get("text") or "")[:60]
    return seen


# Zalo throttles directory lookups and the bridge surfaces a throttle as a
# plain 500, so resolved names are cached for the life of the process and the
# number of lookups per request is capped.
_name_cache: Dict[str, str] = {}
_NAME_LOOKUPS_PER_REQUEST = 12


async def resolve_name(uid: str, budget: List[int]) -> str:
    """Best-effort display name for a uid the friend list does not cover.

    An empty result is meaningful, not merely missing: a uid minted under a
    PREVIOUS bot account cannot be resolved by the current one, because a Zalo
    uid is relative to the account observing it. Those entries are dead weight
    in allowlist.json and the UI labels them so they can be cleared out.
    """
    if uid in _name_cache:
        return _name_cache[uid]
    if budget[0] <= 0:
        return ""
    budget[0] -= 1
    ok, data = await bridge(f"/user-info?id={uid}")
    name = str((data or {}).get("name") or "") if ok and isinstance(data, dict) else ""
    _name_cache[uid] = name
    return name


def _norm_people(raw: Any) -> List[Dict[str, str]]:
    """Flatten the bridge's friend/group payload into {id, name, extra}."""
    items = raw if isinstance(raw, list) else (
        raw.get("friends") or raw.get("groups") or raw.get("items") or []
        if isinstance(raw, dict) else []
    )
    out: List[Dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        uid = str(it.get("userId") or it.get("id") or it.get("groupId") or "")
        if not uid:
            continue
        out.append({
            "id": uid,
            "name": str(it.get("displayName") or it.get("zaloName")
                        or it.get("name") or uid),
            "extra": str(it.get("phoneNumber") or it.get("phone")
                         or (f"{it.get('totalMember')} thành viên"
                             if it.get("totalMember") else "") or ""),
        })
    out.sort(key=lambda r: r["name"].lower())
    return out


# ── app ────────────────────────────────────────────────────────────────────

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def client_key(request: Request) -> str:
    return request.client.host if request.client else "?"


def authed(request: Request) -> Optional[str]:
    """Return the session CSRF token when the request carries a valid session."""
    sess = read_session(request.cookies.get(COOKIE, ""))
    return sess[1] if sess else None


def deny(request: Request) -> Optional[JSONResponse]:
    if authed(request) is None:
        return JSONResponse({"error": "chưa đăng nhập"}, status_code=401)
    return None


def csrf_ok(request: Request) -> bool:
    token = authed(request)
    sent = request.headers.get("x-csrf-token", "")
    return bool(token) and hmac.compare_digest(token, sent)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, loi: str = ""):
    # Starlette 1.x signature: (request, name, context). The legacy
    # (name, {"request": ...}) form makes Jinja treat the dict as the
    # template name -> TypeError: unhashable type: 'dict'.
    return templates.TemplateResponse(request, "login.html", {"loi": loi})


@app.post("/login")
async def login(request: Request, matkhau: str = Form("")):
    ck = client_key(request)
    if throttled(ck):
        return RedirectResponse("/login?loi=Quá nhiều lần sai, thử lại sau 5 phút.", 303)
    stored = env_value(PASSWORD_ENV)
    if not stored or not verify_password(matkhau, stored):
        record_fail(ck)
        return RedirectResponse("/login?loi=Mật khẩu không đúng.", 303)

    csrf = secrets.token_urlsafe(24)
    resp = RedirectResponse("/", 303)
    # Secure only when the proxy tells us the browser hop was TLS. Setting it
    # unconditionally would break a loopback-only install over plain http.
    secure = request.headers.get("x-forwarded-proto", "").lower() == "https"
    resp.set_cookie(
        COOKIE, sign_session(int(time.time()), csrf),
        httponly=True, samesite="strict", secure=secure,
        max_age=SESSION_TTL, path="/",
    )
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    csrf = authed(request)
    if csrf is None:
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "index.html", {"csrf": csrf})


@app.get("/api/status")
async def api_status(request: Request):
    if r := deny(request):
        return r
    ok, health = await bridge("/health")
    st = store()
    admins = st.admin_ids(env_fallback=env_value("ZALO_OWNER_ID"))
    return {
        "bridge_ok": ok,
        "ready": bool(ok and isinstance(health, dict) and health.get("ready")),
        "own_id": (health or {}).get("own_id") if isinstance(health, dict) else "",
        "error": None if ok else health,
        "mode": st.mode,
        "admins": [{"id": a, "name": st.admin_name(a) or a} for a in admins],
        "counts": {
            "allow": len(_entry_ids(st, "users", "allow")),
            "deny": len(_entry_ids(st, "users", "deny")),
            "groups": len(_entry_ids(st, "groups", "allow")),
        },
    }


@app.get("/api/directory")
async def api_directory(request: Request, kind: str = "friends"):
    """Friends or groups, each annotated with its current access state.

    The bridge throttles these upstream calls and reports a throttle as a 500,
    so the UI fetches once and refreshes only on demand.
    """
    if r := deny(request):
        return r
    ok, raw = await bridge("/friends" if kind == "friends" else "/groups")
    if not ok:
        return JSONResponse({"error": raw}, status_code=502)

    st = store()
    admins = set(st.admin_ids(env_fallback=env_value("ZALO_OWNER_ID")))
    allow = _entry_ids(st, "users", "allow")
    deny_ids = _entry_ids(st, "users", "deny")
    groups = _entry_ids(st, "groups", "allow")
    mode = st.mode

    people = _norm_people(raw)

    if kind == "friends":
        # Three sources, not one. The friend list alone hides exactly the
        # people who need attention: someone refused at the gate is by
        # definition not a friend, so they never appeared here and could not
        # be granted from this screen at all.
        by_id: Dict[str, Dict[str, Any]] = {}
        for p in people:
            by_id[p["id"]] = {**p, "source": "friend"}

        stored_names = {
            str(e.get("id")): str(e.get("name") or "")
            for section, bucket in (("users", "allow"), ("users", "deny"))
            for e in st.entries(section, bucket)
            if isinstance(e, dict) and e.get("id")
        }
        stored_names.update({
            a: st.admin_name(a) or "" for a in admins
        })

        seen = audit_senders()
        budget = [_NAME_LOOKUPS_PER_REQUEST]
        for uid in list(seen) + [u for u in stored_names if u not in seen]:
            if uid in by_id:
                by_id[uid]["source"] = "friend"
                continue
            name = stored_names.get(uid) or await resolve_name(uid, budget)
            by_id[uid] = {
                "id": uid,
                "name": name or uid,
                "extra": "",
                # No name from any source means the uid cannot be resolved by
                # the CURRENT bot account — almost always a leftover from a
                # previous account, since a uid is relative to the observer.
                "source": "stale" if not name else ("seen" if uid in seen else "list"),
            }
        for uid, info in seen.items():
            row = by_id.get(uid)
            if row is not None:
                row["last_seen"] = str(info.get("last") or "")[:19].replace("T", " ")
                row["msg_count"] = info.get("count", 0)
                row["last_text"] = info.get("last_text", "")
        people = list(by_id.values())

    rows = []
    for p in people:
        if kind == "friends":
            if p["id"] in admins:
                state, label = "admin", "quản trị"
            elif p["id"] in deny_ids:
                state, label = "deny", "đã chặn"
            elif p["id"] in allow:
                state, label = "allow", "được dùng"
            elif mode == "friends" and p.get("source") == "friend":
                state, label = "friend", "được dùng (bạn bè)"
            else:
                state, label = "none", "chưa cấp"
        else:
            approved = p["id"] in groups
            state, label = ("allow", "đã duyệt") if approved else ("none", "chưa duyệt")
        rows.append({**p, "state": state, "label": label})

    # Anyone who has messaged and has no access yet is what this screen exists
    # for, so sort them to the top instead of leaving them under the friends.
    if kind == "friends":
        order = {"none": 0, "deny": 1, "allow": 2, "friend": 3, "admin": 4}
        rows.sort(key=lambda r: (
            order.get(r["state"], 9),
            -int(r.get("msg_count") or 0),
            r["name"].lower(),
        ))
    return {"rows": rows, "mode": mode}


@app.post("/api/access")
async def api_access(request: Request, payload: Dict[str, Any] = Body(...)):
    """Apply one access change.

    action: allow | deny | clear | admin | unadmin | group_allow | group_clear
    """
    if r := deny(request):
        return r
    if not csrf_ok(request):
        return JSONResponse({"error": "CSRF token không hợp lệ"}, status_code=403)

    uid = str(payload.get("id") or "")
    name = str(payload.get("name") or "")
    action = str(payload.get("action") or "")
    if not uid:
        return JSONResponse({"error": "thiếu id"}, status_code=400)

    st = store()
    try:
        if action == "allow":
            st.remove("users", "deny", uid)
            st.add("users", "allow", uid, name)
        elif action == "deny":
            st.remove("users", "allow", uid)
            st.add("users", "deny", uid, name)
        elif action == "clear":
            st.remove("users", "allow", uid)
            st.remove("users", "deny", uid)
        elif action == "admin":
            st.add_admin(uid, name)
        elif action == "unadmin":
            # remove_admin distinguishes two refusals, and they mean opposite
            # things: it RAISES ValueError for the last admin (an empty list
            # falls back to ZALO_OWNER_ID and, if that is unset, locks everyone
            # out of approvals), and RETURNS False when the id simply is not an
            # admin. Translate both -- the store's own message is English and
            # would otherwise surface raw in a Vietnamese UI.
            try:
                removed = st.remove_admin(uid)
            except ValueError:
                return JSONResponse(
                    {"error": "Không thu được quyền của quản trị viên cuối cùng. "
                              "Hãy phong quyền cho người khác trước, rồi thu lại."},
                    status_code=409,
                )
            if not removed:
                return JSONResponse(
                    {"error": "Người này không phải quản trị viên."},
                    status_code=404,
                )
        elif action == "group_allow":
            st.add("groups", "allow", uid, name)
        elif action == "group_clear":
            st.remove("groups", "allow", uid)
        else:
            return JSONResponse({"error": f"action lạ: {action}"}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return {"ok": True}


@app.post("/api/mode")
async def api_mode(request: Request, payload: Dict[str, Any] = Body(...)):
    if r := deny(request):
        return r
    if not csrf_ok(request):
        return JSONResponse({"error": "CSRF token không hợp lệ"}, status_code=403)
    mode = str(payload.get("mode") or "")
    if mode not in ("friends", "list"):
        return JSONResponse({"error": "mode phải là friends hoặc list"}, status_code=400)

    st = store()
    # Go through the store's own atomic writer (tmp + replace) rather than
    # rewriting the file here, so a concurrent hot-reload never sees a
    # half-written allowlist.
    st._maybe_reload(force=True)
    data = dict(st._data)
    data["mode"] = mode
    st._write(data)
    return {"ok": True, "mode": mode}


@app.post("/api/qr/start")
async def api_qr_start(request: Request):
    if r := deny(request):
        return r
    if not csrf_ok(request):
        return JSONResponse({"error": "CSRF token không hợp lệ"}, status_code=403)
    ok, data = await bridge("/qr/start", method="POST", json_body={})
    if not ok:
        return JSONResponse({"error": data}, status_code=502)

    # The bridge returns only {token, expires_at, reused}. Build the shareable
    # link against THIS panel's public origin, not the bridge's port: the QR
    # page is proxied through /qr below so a phone reaches it over the panel's
    # TLS instead of requiring port 8647 to be opened to the internet.
    token = (data or {}).get("token", "")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return {**data, "url": f"{proto}://{host}/qr?t={token}" if host and token else ""}


@app.get("/qr", response_class=HTMLResponse)
async def qr_page(t: str = ""):
    """Proxy the bridge's QR page.

    Deliberately NOT behind the admin session: this is the link handed to a
    phone, which will not be logged into the panel. The single-use token in
    the query string is the credential — the bridge issues 24 random bytes,
    expires them after 10 minutes, and burns them the moment a login lands.
    Everything else on the bridge stays loopback-only.
    """
    ok, body = await bridge(f"/qr?t={t}")
    if not ok:
        return HTMLResponse(
            "<h1>Link không dùng được</h1>"
            "<p>Mã đã hết hạn, đã được dùng, hoặc token sai. "
            "Vào trang quản trị và tạo mã mới.</p>",
            status_code=403,
        )
    return HTMLResponse(body if isinstance(body, str) else json.dumps(body))


@app.get("/qr/status")
async def qr_status_proxy(t: str = ""):
    """Companion to /qr — the proxied page polls this path on its own origin.

    Unauthenticated for the same reason, and equally token-gated. Without it
    the page loads but never advances past "waiting", because its fetch would
    hit the panel's session wall.
    """
    ok, data = await bridge(f"/qr/status?t={t}")
    if not ok:
        return JSONResponse({"state": "expired"}, status_code=403)
    return data


@app.get("/api/qr/status")
async def api_qr_status(request: Request, t: str = ""):
    if r := deny(request):
        return r
    ok, data = await bridge(f"/qr/status?t={t}")
    if not ok:
        return JSONResponse({"error": data}, status_code=502)
    return data


@app.get("/api/audit")
async def api_audit(request: Request, limit: int = 200, q: str = ""):
    if r := deny(request):
        return r
    path = hermes_home() / "zalo" / "audit.jsonl"
    if not path.is_file():
        return {"rows": []}
    rows: List[Dict[str, Any]] = []
    needle = q.lower().strip()
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if needle and needle not in json.dumps(d, ensure_ascii=False).lower():
                continue
            rows.append({
                "ts": str(d.get("ts", ""))[:19].replace("T", " "),
                "dir": d.get("dir", ""),
                "verdict": d.get("verdict", ""),
                "sender": str(d.get("sender", "")),
                "thread_type": d.get("thread_type", ""),
                "text": str(d.get("text", ""))[:160],
                "error": str(d.get("error", ""))[:120],
            })
    return {"rows": rows[-limit:][::-1]}


# ── entry point ────────────────────────────────────────────────────────────

def cmd_set_password() -> int:
    import getpass

    pw = getpass.getpass("Mật khẩu admin mới: ")
    if len(pw) < 10:
        print("Quá ngắn — tối thiểu 10 ký tự.")
        return 1
    if pw != getpass.getpass("Nhập lại: "):
        print("Hai lần nhập không khớp.")
        return 1

    line = f"{PASSWORD_ENV}={hash_password(pw)}"
    path = env_file()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if re.search(rf"^{PASSWORD_ENV}=", text, re.M):
        text = re.sub(rf"^{PASSWORD_ENV}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n\n# Bang admin web (plugins/platforms/zalo/admin)\n{line}\n"
    path.write_text(text, encoding="utf-8")
    print(f"Đã ghi hash vào {path}")
    print("Mật khẩu KHÔNG được lưu — chỉ có hash scrypt.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8648)
    ap.add_argument("--set-password", action="store_true")
    args = ap.parse_args()

    if args.set_password:
        return cmd_set_password()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"TỪ CHỐI bind {args.host}.", file=sys.stderr)
        print(
            "Trang này cấp/thu quyền dùng bot và khởi động được đăng nhập QR — "
            "ai vào được thì chiếm được tài khoản Zalo.\n"
            "Hãy bind loopback và đặt reverse proxy có TLS phía trước "
            "(deploy/admin/Caddyfile.example).",
            file=sys.stderr,
        )
        return 2

    if not env_value(PASSWORD_ENV):
        print("Chưa đặt mật khẩu admin.", file=sys.stderr)
        print(f"Chạy:  python {Path(__file__).name} --set-password", file=sys.stderr)
        return 2

    import uvicorn

    print(f"Zalo admin -> http://{args.host}:{args.port}")
    print(f"HERMES_HOME = {hermes_home()}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
