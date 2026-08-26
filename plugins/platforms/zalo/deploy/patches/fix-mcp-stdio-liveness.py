#!/usr/bin/env python3
"""Re-apply the MCP stdio liveness fix to an installed hermes-agent.

WHY THIS EXISTS
---------------
`tools/mcp_tool.py::MCPServerTask._stdio_children_dead()` returns True — its
contract is "every stdio child we spawned has exited" — the moment it finds a
child that is ALIVE. The logic is inverted, and the unreachable line that sat
directly underneath it in the shipped source states the intended meaning:

    for pid in pids:
        if not psutil.pid_exists(pid):
            continue  # this one is dead
        return True   # alive (signal permission irrelevant for liveness)
        return False  # at least one child alive      <-- never reached
    return True

Consequence: the #81995 fast-fail path treats a perfectly healthy stdio MCP
server as dead, so EVERY tool call on it is aborted immediately with

    MCP call failed: TimeoutError: MCP stdio subprocess for '<name>' has
    exited; failing the call fast instead of waiting 300s

This affects every stdio MCP server, not just Odoo, and it is invisible from
the usual checks:

  * `hermes mcp test <name>` PASSES — it connects and lists tools but never
    calls one, so it does not touch this code path.
  * The subprocess really is running; `ps` / Get-CimInstance shows it.
  * Calls fail in ~0.02s, far too fast to be a genuine 300s timeout — that
    timing is the tell.

Observed on Hermes 0.20.5. Check whether upstream has fixed it before running
this; if the surrounding source no longer matches, the script refuses rather
than guessing.

USAGE
-----
    python fix-mcp-stdio-liveness.py [--hermes-agent DIR] [--check]

    --check   report status and exit non-zero if the patch is needed

Idempotent: running it twice is a no-op. Writes a .bak-<timestamp> beside the
file on first application.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

BROKEN = """            if not psutil.pid_exists(pid):
                continue  # this one is dead
            return True  # alive (signal permission irrelevant for liveness)
            return False  # at least one child alive
        return True"""

FIXED = """            if not psutil.pid_exists(pid):
                continue  # this one is dead
            # PATCHED (deploy/patches/fix-mcp-stdio-liveness.py): upstream
            # returned True here -- "all children dead" -- on finding a child
            # ALIVE. The unreachable `return False` that followed shows the
            # intent. The bug made every stdio MCP tool call fast-fail with
            # "subprocess has exited" while the process was still running.
            return False  # at least one child alive -> not all dead
        return True"""

MARKER = "deploy/patches/fix-mcp-stdio-liveness.py"


def default_hermes_agent() -> Path:
    if env := os.environ.get("HERMES_HOME"):
        # A profile home is .../profiles/<name>; the install lives at the root.
        p = Path(env)
        root = p.parent.parent if p.parent.name == "profiles" else p
        return root / "hermes-agent"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "hermes" / "hermes-agent"
    return Path.home() / ".hermes" / "hermes-agent"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-agent", type=Path, default=None)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    root = args.hermes_agent or default_hermes_agent()
    target = root / "tools" / "mcp_tool.py"

    if not target.is_file():
        print(f"NOT FOUND: {target}")
        print("Pass --hermes-agent DIR pointing at the hermes-agent checkout.")
        return 2

    src = target.read_text(encoding="utf-8")

    if MARKER in src:
        print(f"already patched: {target}")
        return 0

    if BROKEN not in src:
        print(f"PATTERN NOT FOUND in {target}")
        print("Either upstream fixed this, or the surrounding code changed.")
        print("Inspect _stdio_children_dead() by hand — do not force it.")
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
    print("Restart the gateway, then confirm with a real tool call — not with")
    print("`hermes mcp test`, which passes either way.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
