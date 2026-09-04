#!/usr/bin/env python3
"""Make odoo-mcp's `execute_method` obey the exact side-effect allowlist.

WHY THIS EXISTS
---------------
Enabling `execute_method` (needed for the transfer-receipt flow) opens a read
path that the field ACL never sees.

`field_policy.json` is enforced in `tools_read.py` — `redact_records`,
`redact_record`, `check_aggregate`. `tools_write.py`, where `execute_method`
lives, does not import `field_policy` at all, and `odoo_client.py` does no
redaction either. So whatever `execute_method` returns comes back raw.

That would not matter if `execute_method` only reached the three allow-listed
receipt methods. It does not. The gate is:

    safety = classify_method_safety(method)          # NAME-based
    review_required = safety["safety"] in {"side_effect", "unknown"}

and `classify_method_safety` (diagnostics.py) calls a method `read_only` — no
allowlist required — when its name is in

    {search, search_count, search_read, read, fields_get, name_get,
     name_search, context_get}

or merely starts with `get_` / `_get_`. Only create/write/unlink are blocked
outright. So with `execute_method` enabled:

    execute_method(model="hr.employee", method="search_read",
                   kwargs={"fields": [...]})

returns every field, on any model, under whatever the configured Odoo
credential can see — bypassing the `allow`/`deny` lists that DEPLOY.md
describes as "enforced server-side on every read". Where that credential is
an administrator, `field_policy.json` is the only barrier there is, and this
walks around it.

THE FIX
-------
Require the exact `model.method` allowlist for EVERY method reaching
`execute_method`, not just for the ones whose names look risky. The write
surface then really is the allow-listed methods and nothing else, which is
what the config comment and the SOUL guardrail both claim.

Nothing legitimate loses out: reads go through `search_records` /
`read_record` / `aggregate_records`, which do apply the field policy.
`ODOO_MCP_ALLOW_UNKNOWN_METHODS=1` still disables the gate wholesale — leave
it at "0".

USAGE
-----
    python fix-execute-method-field-acl.py [--site-packages DIR] [--check]

    --check   report status and exit non-zero if the patch is needed

Idempotent. Writes a .bak-<timestamp> beside the file on first application.
Reapply after any `uv tool upgrade odoo-mcp` — an upgrade replaces the file.
Verified against odoo-mcp 1.3.0.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

BROKEN = """        review_required = safety["safety"] in {"side_effect", "unknown"}
        if (
            review_required"""

FIXED = """        # PATCHED (deploy/patches/fix-execute-method-field-acl.py): upstream
        # required review only for side_effect/unknown names, letting anything
        # classified read_only by NAME (search_read, read, name_get, get_*)
        # through on any model. execute_method never applies field_policy.json
        # — that lives in tools_read.py — so those calls returned unredacted
        # rows and walked around the field ACL. Require the exact allowlist for
        # every method instead; real reads have their own policy-aware tools.
        review_required = True
        if (
            review_required"""

MARKER = "deploy/patches/fix-execute-method-field-acl.py"


def default_site_packages() -> Path:
    if env := os.environ.get("ODOO_MCP_SITE_PACKAGES"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "uv" / "tools" / "odoo-mcp" / "Lib" / "site-packages"
    return Path.home() / ".local" / "share" / "uv" / "tools" / "odoo-mcp" / "lib"


def find_target(root: Path) -> Path | None:
    direct = root / "odoo_mcp" / "tools_write.py"
    if direct.is_file():
        return direct
    # Linux uv layout buries site-packages under lib/python3.X/
    for hit in sorted(root.glob("**/odoo_mcp/tools_write.py")):
        return hit
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-packages", type=Path, default=None)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    root = args.site_packages or default_site_packages()
    target = find_target(root)

    if target is None:
        print(f"NOT FOUND: no odoo_mcp/tools_write.py under {root}")
        print("Pass --site-packages DIR pointing at the odoo-mcp install.")
        return 2

    src = target.read_text(encoding="utf-8")

    if MARKER in src:
        print(f"already patched: {target}")
        return 0

    if BROKEN not in src:
        print(f"PATTERN NOT FOUND in {target}")
        print("Either upstream changed the gate, or the version differs.")
        print("Read execute_method() by hand — do not force it.")
        return 3

    if args.check:
        print(f"PATCH NEEDED: {target}")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_suffix(f".py.bak-{stamp}")
    backup.write_text(src, encoding="utf-8")

    target.write_text(src.replace(BROKEN, FIXED, 1), encoding="utf-8")
    print(f"patched : {target}")
    print(f"backup  : {backup}")
    print()
    print("Restart the gateway. Confirm with a real call: execute_method on an")
    print("allow-listed receipt method must still work, and the same tool with")
    print("model='hr.employee', method='search_read' must now be refused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
