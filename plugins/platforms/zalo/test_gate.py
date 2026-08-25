#!/usr/bin/env python3
"""Standalone checks for the Zalo access gate.

Exercises AllowlistStore + mentions_self + the dedup LRU without needing a
running gateway or bridge. Run:  python3 ~/.hermes/zalo/test_gate.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store_mod = _load("allowlist_store", PLUGIN / "allowlist_store.py")
AllowlistStore = store_mod.AllowlistStore

PASS, FAIL = [], []


def check(label: str, got, want) -> None:
    (PASS if got == want else FAIL).append(label)
    mark = "✓" if got == want else "✗"
    detail = "" if got == want else f"   (got {got!r}, want {want!r})"
    print(f"  {mark} {label}{detail}")


# ---------------------------------------------------------------------------
# AllowlistStore
# ---------------------------------------------------------------------------
print("\n=== AllowlistStore ===")
tmp = Path(tempfile.mkdtemp()) / "allowlist.json"
tmp.write_text(json.dumps({
    "mode": "friends",
    "admins": [{"id": "admin1", "name": "Anh Doan"},
               {"id": "admin2", "name": "Chi Ha"}],
    "notify": "all",
    "users": {"allow": [{"id": "u_manual", "name": "Nha thau"}],
              "deny": [{"id": "u_bad", "name": "Da nghi viec"}]},
    "groups": {"allow": [{"id": "g_ok", "name": "Ke toan"}]},
}), encoding="utf-8")
s = AllowlistStore(tmp)

check("mode = friends", s.mode, "friends")
check("admin1 is admin", s.is_admin("admin1"), True)
check("stranger is not admin", s.is_admin("nobody"), False)
check("admin order preserved", s.admin_ids(), ["admin1", "admin2"])
check("admin name lookup", s.admin_name("admin2"), "Chi Ha")
check("notify=all -> both", s.notify_targets(), ["admin1", "admin2"])
check("manual allow", s.user_allowed("u_manual"), True)
check("deny hit", s.user_denied("u_bad"), True)
check("group allow", s.group_allowed("g_ok"), True)
check("unknown group denied", s.group_allowed("g_other"), False)

# env fallback only when file lists no admins
empty = Path(tempfile.mkdtemp()) / "a.json"
empty.write_text('{"mode":"list","admins":[]}', encoding="utf-8")
s2 = AllowlistStore(empty)
check("env fallback used", s2.admin_ids("envadmin"), ["envadmin"])
check("file admins beat env", s.admin_ids("envadmin"), ["admin1", "admin2"])
check("mode=list honored", s2.mode, "list")

# notify=first
first = Path(tempfile.mkdtemp()) / "a.json"
first.write_text(json.dumps({
    "notify": "first",
    "admins": [{"id": "a1"}, {"id": "a2"}],
}), encoding="utf-8")
check("notify=first -> one", AllowlistStore(first).notify_targets(), ["a1"])

# mutation
s.add("users", "allow", "u_new", "Nguoi moi")
check("add persists", AllowlistStore(tmp).user_allowed("u_new"), True)
check("add is idempotent", s.add("users", "allow", "u_new", "x"), False)
s.remove("users", "allow", "u_new")
check("remove persists", AllowlistStore(tmp).user_allowed("u_new"), False)
check("file mode 0600", oct(tmp.stat().st_mode & 0o777), "0o600")

# corrupt file keeps last good copy (fail-closed)
s3 = AllowlistStore(tmp)
_ = s3.mode                      # prime cache
tmp.write_text("{ broken json", encoding="utf-8")
s3._last_check = 0.0             # bypass throttle
check("corrupt file -> keeps admins", s3.is_admin("admin1"), True)
check("corrupt file -> no new access", s3.user_allowed("anyone"), False)

# missing file denies everything but env admin
gone = Path(tempfile.mkdtemp()) / "nope.json"
s4 = AllowlistStore(gone)
check("missing file -> deny", s4.user_allowed("u_manual"), False)
check("missing file -> env admin ok", s4.is_admin("envadmin", "envadmin"), True)

# ---------------------------------------------------------------------------
# mentions_self  (parsed standalone; adapter.py imports gateway internals)
# ---------------------------------------------------------------------------
print("\n=== mentions_self ===")
src = (PLUGIN / "adapter.py").read_text(encoding="utf-8")
start = src.index("def mentions_self")
end = src.index("def classify_inbound")
ns: dict = {"MENTION_ALL_UIDS": {"-1", "0"}, "Dict": dict, "Any": object}
exec(compile(src[start:end], "mentions_self", "exec"), ns)
mentions_self = ns["mentions_self"]

BOT = "bot999"
check("tagged", mentions_self({"raw": {"mentions": [{"uid": BOT}]}}, BOT), True)
check("other tagged",
      mentions_self({"raw": {"mentions": [{"uid": "someone"}]}}, BOT), False)
check("no mentions", mentions_self({"raw": {}}, BOT), False)
check("raw missing", mentions_self({}, BOT), False)
check("own_id empty -> False",
      mentions_self({"raw": {"mentions": [{"uid": BOT}]}}, ""), False)
check("@all ignored by default",
      mentions_self({"raw": {"mentions": [{"uid": "-1"}]}}, BOT), False)
check("@all honored when enabled",
      mentions_self({"raw": {"mentions": [{"uid": "-1"}]}}, BOT,
                    honor_mention_all=True), True)
check("mixed list finds bot",
      mentions_self({"raw": {"mentions": [{"uid": "x"}, {"uid": BOT}]}}, BOT),
      True)
check("malformed entries survived",
      mentions_self({"raw": {"mentions": ["junk", None, {"uid": BOT}]}}, BOT),
      True)

# ---------------------------------------------------------------------------
# dedup LRU
# ---------------------------------------------------------------------------
print("\n=== dedup ===")
CAP = 2000
seen: "OrderedDict[str, None]" = OrderedDict()


def is_dup(mid: str) -> bool:
    if not mid:
        return False
    if mid in seen:
        return True
    seen[mid] = None
    if len(seen) > CAP:
        seen.popitem(last=False)
    return False


check("first sighting", is_dup("m1"), False)
check("replay caught", is_dup("m1"), True)
check("empty id passes", is_dup(""), False)
check("empty id passes twice", is_dup(""), False)
for i in range(500):
    is_dup(f"ring{i}")
check("500-event replay all dup",
      all(is_dup(f"ring{i}") for i in range(500)), True)
for i in range(CAP + 100):
    is_dup(f"flood{i}")
check("LRU bounded", len(seen) <= CAP, True)

# ---------------------------------------------------------------------------
print(f"\n{'=' * 46}")
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print(f"  ✗ {f}")
    sys.exit(1)
print("All gate logic checks passed.")
