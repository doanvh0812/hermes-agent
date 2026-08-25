"""Hot-reloading allowlist store for the Zalo adapter.

Backs the access model documented in ``~/.hermes/zalo/allowlist.design.md``:

    0. admin     -> allow  (denylist does not apply; never lock yourself out)
    1. denylist  -> deny   (beats friendship)
    2. allowlist -> allow  (manual entries, e.g. non-friends)
    3. friend    -> allow  (only when mode == "friends")
    4. otherwise -> deny

The JSON file is re-read when its mtime changes (checked at most every
``RELOAD_CHECK_SECONDS``), so edits take effect without a gateway restart.
A malformed file keeps the last good copy in memory rather than failing
open — a syntax error while hand-editing must never turn the bot public.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

RELOAD_CHECK_SECONDS = 5.0
VALID_MODES = {"friends", "list"}


def _entry_id(entry: Any) -> str:
    """Accept both ``{"id": ..., "name": ...}`` and a bare id string."""
    if isinstance(entry, dict):
        raw = entry.get("id")
        return str(raw) if raw not in (None, "") else ""
    if isinstance(entry, str):
        return entry
    return ""


def _entry_name(entry: Any, fallback: str = "") -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or fallback)
    return fallback


class AllowlistStore:
    """Read/write access to ``allowlist.json`` with hot reload."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._data: Dict[str, Any] = {}
        self._mtime: float = -1.0
        self._last_check: float = 0.0

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _maybe_reload(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_check < RELOAD_CHECK_SECONDS:
            return
        self._last_check = now
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            # File missing: nothing is allowed except the env-var admin
            # fallback. Do NOT keep a stale in-memory copy here — a deleted
            # file is an explicit operator action.
            self._data = {}
            self._mtime = -1.0
            return
        if mtime == self._mtime:
            return
        try:
            parsed = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Corrupt or unreadable: keep the last good copy (fail-closed).
            return
        if isinstance(parsed, dict):
            self._data = parsed
            self._mtime = mtime

    def _ids(self, section: str, bucket: str) -> set:
        self._maybe_reload()
        entries = (self._data.get(section) or {}).get(bucket) or []
        if not isinstance(entries, list):
            return set()
        return {eid for eid in (_entry_id(e) for e in entries) if eid}

    def entries(self, section: str, bucket: str) -> List[Any]:
        """Raw entry list — used for rendering ``/ai``."""
        self._maybe_reload()
        entries = (self._data.get(section) or {}).get(bucket) or []
        return entries if isinstance(entries, list) else []

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def mode(self) -> str:
        self._maybe_reload()
        mode = str(self._data.get("mode") or "friends").lower()
        return mode if mode in VALID_MODES else "friends"

    def user_allowed(self, uid: str) -> bool:
        return bool(uid) and uid in self._ids("users", "allow")

    def user_denied(self, uid: str) -> bool:
        return bool(uid) and uid in self._ids("users", "deny")

    def group_allowed(self, gid: str) -> bool:
        return bool(gid) and gid in self._ids("groups", "allow")

    # ------------------------------------------------------------------
    # Admins
    # ------------------------------------------------------------------

    def admin_ids(self, env_fallback: str = "") -> List[str]:
        """Admin ids in declaration order.

        Falls back to a CSV env value (``ZALO_OWNER_ID``) only when the
        file declares no admins — that bootstraps the very first approval
        before ``allowlist.json`` exists.
        """
        self._maybe_reload()
        raw = self._data.get("admins") or []
        out: List[str] = []
        if isinstance(raw, list):
            for entry in raw:
                eid = _entry_id(entry)
                if eid and eid not in out:
                    out.append(eid)
        if not out and env_fallback:
            for part in env_fallback.split(","):
                part = part.strip()
                if part and part not in out:
                    out.append(part)
        return out

    def is_admin(self, uid: str, env_fallback: str = "") -> bool:
        return bool(uid) and uid in self.admin_ids(env_fallback)

    def notify_targets(self, env_fallback: str = "") -> List[str]:
        """Which admins receive stranger notifications."""
        ids = self.admin_ids(env_fallback)
        self._maybe_reload()
        if str(self._data.get("notify") or "all").lower() == "first":
            return ids[:1]
        return ids

    def admin_name(self, uid: str) -> str:
        self._maybe_reload()
        raw = self._data.get("admins") or []
        if isinstance(raw, list):
            for entry in raw:
                if _entry_id(entry) == uid:
                    return _entry_name(entry, uid)
        return uid

    # ------------------------------------------------------------------
    # Mutation (atomic writes)
    # ------------------------------------------------------------------

    def add_admin(self, entry_id: str, name: str = "") -> bool:
        """Add an admin. Returns True when the file changed.

        ``admins`` is a flat list, unlike ``users``/``groups`` which nest a
        bucket under a section — passing it to :meth:`add` would rewrite it
        as ``{"<bucket>": [...]}`` and silently destroy every admin entry.
        """
        entry_id = str(entry_id or "")
        if not entry_id:
            return False
        self._maybe_reload(force=True)
        data = dict(self._data)
        admins = list(data.get("admins") or [])
        for existing in admins:
            if _entry_id(existing) == entry_id:
                return False
        admins.append({"id": entry_id, "name": name or entry_id})
        data["admins"] = admins
        self._write(data)
        return True

    def remove_admin(self, entry_id: str) -> bool:
        """Remove an admin. Refuses to remove the last one.

        An empty ``admins`` list falls back to ``ZALO_OWNER_ID``; if that is
        unset too, nobody can approve anyone and the only way back is
        hand-editing the file.
        """
        entry_id = str(entry_id or "")
        if not entry_id:
            return False
        self._maybe_reload(force=True)
        data = dict(self._data)
        admins = list(data.get("admins") or [])
        kept = [e for e in admins if _entry_id(e) != entry_id]
        if len(kept) == len(admins):
            return False
        if not kept:
            raise ValueError(
                "refusing to remove the last admin — add another one first"
            )
        data["admins"] = kept
        self._write(data)
        return True

    def add(self, section: str, bucket: str, entry_id: str,
            name: str = "") -> bool:
        """Add one entry to a section/bucket list (users, groups).

        Not for ``admins`` — that is a flat list; use :meth:`add_admin`.
        """
        if section == "admins":
            raise ValueError("use add_admin() — 'admins' is a flat list")
        entry_id = str(entry_id or "")
        if not entry_id:
            return False
        self._maybe_reload(force=True)
        data = dict(self._data)
        sec = dict(data.get(section) or {})
        lst = list(sec.get(bucket) or [])
        for existing in lst:
            if _entry_id(existing) == entry_id:
                return False
        lst.append({"id": entry_id, "name": name or entry_id})
        sec[bucket] = lst
        data[section] = sec
        self._write(data)
        return True

    def remove(self, section: str, bucket: str, entry_id: str) -> bool:
        if section == "admins":
            raise ValueError("use remove_admin() — 'admins' is a flat list")
        entry_id = str(entry_id or "")
        if not entry_id:
            return False
        self._maybe_reload(force=True)
        data = dict(self._data)
        sec = dict(data.get(section) or {})
        lst = list(sec.get(bucket) or [])
        kept = [e for e in lst if _entry_id(e) != entry_id]
        if len(kept) == len(lst):
            return False
        sec[bucket] = kept
        data[section] = sec
        self._write(data)
        return True

    def _write(self, data: Dict[str, Any]) -> None:
        data.setdefault("mode", "friends")
        tmp = self._path.with_name(self._path.name + ".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self._path)  # atomic on the same filesystem
        self._data = data
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = -1.0
        # A fresh write must not be masked by the reload throttle.
        self._last_check = time.monotonic()

    def ensure_file(self) -> None:
        """Create a skeleton file on first run so the operator has
        something to edit."""
        if self._path.exists():
            return
        self._write({
            "mode": "friends",
            "admins": [],
            "notify": "all",
            "users": {"allow": [], "deny": []},
            "groups": {"allow": []},
        })
