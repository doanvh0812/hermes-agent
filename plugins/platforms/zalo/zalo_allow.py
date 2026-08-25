#!/usr/bin/env python3
"""zalo-allow — pick Zalo friends/groups from a list and grant them access.

Talks to the running bridge over loopback, shows display names and phone
numbers, and writes ``allowlist.json``. Nobody has to read a log or know a
raw uid.

    python3 zalo_allow.py              interactive picker
    python3 zalo_allow.py --list       print current access, exit
    python3 zalo_allow.py --json       machine-readable dump

The gateway does not need to be running; the bridge does.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
DEFAULT_PORT = 8647
HTTP_TIMEOUT = 30.0


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def load_store():
    spec = importlib.util.spec_from_file_location(
        "zalo_allowlist_store", HERE / "allowlist_store.py"
    )
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load allowlist_store.py next to {HERE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AllowlistStore


def env_value(name: str) -> str:
    """Read from the process env, falling back to $HERMES_HOME/.env."""
    if os.environ.get(name):
        return os.environ[name].strip()
    env_file = hermes_home() / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def bridge_token() -> str:
    explicit = env_value("ZALO_BRIDGE_TOKEN")
    if explicit:
        return explicit
    # Legacy fallback: hash of the session file. Racy by design — the bridge
    # rewrites that file after login and every 30 minutes, so this only
    # works if it has not rotated since the bridge started.
    try:
        return hashlib.sha256(
            (hermes_home() / "zalo" / "session.json").read_bytes()
        ).hexdigest()
    except OSError:
        return ""


def bridge_get(path: str) -> Any:
    port = env_value("ZALO_BRIDGE_PORT") or str(DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    token = bridge_token()
    if token:
        req.add_header("X-Bridge-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            sys.exit(
                "Bridge rejected the token.\n"
                "Set ZALO_BRIDGE_TOKEN in $HERMES_HOME/.env to the same value "
                "the bridge was started with, then restart both."
            )
        sys.exit(f"bridge error {exc.code} on {path}")
    except urllib.error.URLError as exc:
        sys.exit(
            f"Cannot reach the bridge on port {port} ({exc.reason}).\n"
            "Start it first:  node bridge/index.js"
        )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def fmt_phone(phone: str) -> str:
    return phone if phone else "—"


def status_of(uid: str, store, mode: str, friend_ids: set) -> Tuple[str, str]:
    """Return (marker, label) describing current access for a user."""
    if store.is_admin(uid, env_value("ZALO_OWNER_ID")):
        return "★", "admin"
    if store.user_denied(uid):
        return "⛔", "bị chặn"
    if store.user_allowed(uid):
        return "✓", "đã cho phép"
    if mode == "friends" and uid in friend_ids:
        return "◦", "bạn bè"
    return " ", "chưa có quyền"


def print_access(store) -> None:
    owner = env_value("ZALO_OWNER_ID")
    admins = store.admin_ids(owner)
    print(f"\n👤 Admin ({len(admins)})")
    for uid in admins:
        print(f"   {store.admin_name(uid)}  [{uid}]")
    if not admins:
        print("   (chưa có — đặt ZALO_OWNER_ID hoặc thêm vào allowlist.json)")

    for icon, title, section, bucket in (
        ("✅", "Cho phép thủ công", "users", "allow"),
        ("⛔", "Chặn", "users", "deny"),
        ("👥", "Nhóm", "groups", "allow"),
    ):
        entries = store.entries(section, bucket)
        print(f"\n{icon} {title} ({len(entries)})")
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else str(entry)
            uid = entry.get("id") if isinstance(entry, dict) else entry
            print(f"   {name}  [{uid}]")

    print(f"\nChế độ: {store.mode}", end="")
    if store.mode == "friends":
        print(" — mọi bạn bè trên Zalo đều có quyền dùng bot")
    else:
        print(" — chỉ danh sách trên, bạn bè KHÔNG tự có quyền")
    print(f"File  : {store.path}\n")


# --------------------------------------------------------------------------
# Interactive picker
# --------------------------------------------------------------------------

_DIRECTORY_CACHE: Dict[str, Any] = {}


def fetch_directory(refresh: bool = False) -> Tuple[List[Any], List[Any]]:
    """Fetch groups + friends once per run.

    Zalo throttles these upstream calls, and the bridge surfaces a throttle
    as a 500. Re-listing after every edit would trip it, so the directory is
    cached and only the local access markers are recomputed.
    """
    if refresh or "groups" not in _DIRECTORY_CACHE:
        _DIRECTORY_CACHE["groups"] = bridge_get("/groups").get("groups") or []
        _DIRECTORY_CACHE["friends"] = bridge_get("/friends").get("friends") or []
    return _DIRECTORY_CACHE["groups"], _DIRECTORY_CACHE["friends"]


def build_rows(store, refresh: bool = False) -> Tuple[List[Dict[str, Any]], set]:
    groups, friends = fetch_directory(refresh=refresh)
    friend_ids = {str(f.get("id")) for f in friends if f.get("id")}
    mode = store.mode

    rows: List[Dict[str, Any]] = []
    for g in sorted(groups, key=lambda x: (x.get("name") or "").lower()):
        gid = str(g.get("id") or "")
        rows.append({
            "kind": "group",
            "id": gid,
            "name": g.get("name") or gid,
            "extra": f"{g.get('members', 0)} thành viên",
            "marker": "✓" if store.group_allowed(gid) else " ",
            "label": "đã duyệt" if store.group_allowed(gid) else "chưa duyệt",
        })
    for f in sorted(friends, key=lambda x: (x.get("name") or "").lower()):
        uid = str(f.get("id") or "")
        marker, label = status_of(uid, store, mode, friend_ids)
        rows.append({
            "kind": "user",
            "id": uid,
            "name": f.get("name") or uid,
            "extra": fmt_phone(f.get("phone") or ""),
            "marker": marker,
            "label": label,
        })
    return rows, friend_ids


def render(rows: List[Dict[str, Any]], store) -> None:
    print("\n" + "=" * 62)
    print("  ★ admin   ✓ đã cho phép   ◦ bạn bè (tự có quyền)   ⛔ chặn")
    print("=" * 62)
    last_kind = None
    for i, row in enumerate(rows, 1):
        if row["kind"] != last_kind:
            header = "NHÓM" if row["kind"] == "group" else "BẠN BÈ"
            print(f"\n--- {header} ---")
            last_kind = row["kind"]
        print(f"  {i:3}. [{row['marker']}] {row['name'][:28]:<28} "
              f"{row['extra'][:16]:<16} {row['label']}")


HELP = """
Lệnh:
   1,5,7    cho phép (nhóm: duyệt nhóm; người: thêm vào allowlist)
  -3        bỏ quyền (xoá khỏi allowlist / bỏ duyệt nhóm)
  !4        chặn hẳn (thêm vào denylist — thắng cả bạn bè)
  +2        đặt làm admin
  -+2       bỏ quyền admin
   l        xem lại danh sách
   r        tải lại danh bạ từ Zalo (dùng khi vừa kết bạn / vào nhóm mới)
   q        thoát
"""


def apply_action(token: str, rows: List[Dict[str, Any]], store) -> Optional[str]:
    """Apply one token. Returns a message, or None when nothing happened."""
    m = re.fullmatch(r"(-\+|\+|-|!)?(\d+)", token)
    if not m:
        return f"  ? bỏ qua '{token}'"
    op, idx = m.group(1) or "", int(m.group(2))
    if not (1 <= idx <= len(rows)):
        return f"  ? số {idx} ngoài danh sách"
    row = rows[idx - 1]
    rid, name, kind = row["id"], row["name"], row["kind"]

    try:
        if op == "+":
            if kind != "user":
                return "  ! chỉ đặt admin cho người dùng, không phải nhóm"
            return (f"  ★ {name} là admin" if store.add_admin(rid, name)
                    else f"  = {name} đã là admin")
        if op == "-+":
            if kind != "user":
                return "  ! chỉ áp dụng cho người dùng"
            return (f"  ☆ bỏ admin: {name}" if store.remove_admin(rid)
                    else f"  = {name} vốn không phải admin")
        if op == "!":
            if kind != "user":
                return "  ! chặn chỉ áp dụng cho người dùng"
            store.remove("users", "allow", rid)
            return (f"  ⛔ chặn {name}" if store.add("users", "deny", rid, name)
                    else f"  = {name} đã bị chặn")
        if op == "-":
            if kind == "group":
                return (f"  ✗ bỏ duyệt nhóm {name}"
                        if store.remove("groups", "allow", rid)
                        else f"  = nhóm {name} vốn chưa duyệt")
            removed = store.remove("users", "allow", rid)
            removed |= store.remove("users", "deny", rid)
            return (f"  ✗ bỏ quyền {name}" if removed
                    else f"  = {name} vốn không trong danh sách")
        # no prefix: allow
        if kind == "group":
            return (f"  ✓ duyệt nhóm {name}"
                    if store.add("groups", "allow", rid, name)
                    else f"  = nhóm {name} đã duyệt rồi")
        store.remove("users", "deny", rid)
        return (f"  ✓ cho phép {name}" if store.add("users", "allow", rid, name)
                else f"  = {name} đã được phép")
    except ValueError as exc:
        return f"  ! {exc}"


def interactive(store) -> None:
    rows, _ = build_rows(store)
    if not rows:
        print("Bridge không trả về bạn bè hay nhóm nào.")
        return
    render(rows, store)
    print(HELP)
    while True:
        try:
            raw = input("Chọn> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw in {"q", "quit", "exit"}:
            break
        if raw in {"l", "list"}:
            rows, _ = build_rows(store)
            render(rows, store)
            continue
        if raw in {"r", "refresh"}:
            rows, _ = build_rows(store, refresh=True)
            render(rows, store)
            continue
        for token in re.split(r"[,\s]+", raw):
            if token:
                msg = apply_action(token, rows, store)
                if msg:
                    print(msg)
        rows, _ = build_rows(store)  # markers only; directory is cached
    print_access(store)


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Quản lý quyền truy cập bot Zalo."
    )
    ap.add_argument("--list", action="store_true",
                    help="in danh sách quyền hiện tại rồi thoát")
    ap.add_argument("--json", action="store_true",
                    help="xuất JSON (bạn bè, nhóm, quyền) rồi thoát")
    ap.add_argument("--file", type=Path, default=None,
                    help="đường dẫn allowlist.json")
    args = ap.parse_args()

    store_cls = load_store()
    path = args.file or (hermes_home() / "zalo" / "allowlist.json")
    store = store_cls(path)
    store.ensure_file()

    if args.json:
        rows, _ = build_rows(store)
        json.dump({
            "mode": store.mode,
            "admins": store.admin_ids(env_value("ZALO_OWNER_ID")),
            "rows": rows,
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return

    if args.list:
        print_access(store)
        return

    interactive(store)


if __name__ == "__main__":
    main()
