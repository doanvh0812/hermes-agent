#!/usr/bin/env python3
"""Let `compression.progress_notices` actually suppress compaction status.

WHY THIS EXISTS
---------------
Chat users see these in the thread, mid-conversation:

    ♻️ Compacting context…
    ✓ Context compaction complete — continuing turn...

`gateway/run.py::_prepare_gateway_status_message` is meant to keep that out of
messaging surfaces. Its own comment states the design: opt-in #52995,
`compression.progress_notices: true` lets ROUTINE compression progress through,
and "Default False keeps the silent-by-design behavior".

It does not, because the two gates are nested:

    if _TELEGRAM_NOISY_STATUS_RE.search(text):          # outer
        if not (progress_notices_enabled()
                and _COMPRESSION_PROGRESS_STATUS_RE.search(text)):   # inner
            return None

The inner gate only runs when the OUTER regex matches. That outer regex lists
compression *failures* and the auto-lower notice — not the ordinary start and
done lines. Measured against the live regexes:

    "✓ Context compaction complete — continuing turn..."  outer: no   inner: no
    "♻️ Compacting context…"                              outer: no   inner: no
    "Preflight compression: ~220,025 tokens >= 80,000"    outer: YES  inner: no
    "compression summary failed"                          outer: YES  inner: no

So preflight and failures are suppressed as intended, while the two statuses a
user actually notices bypass suppression entirely — and no configuration
reaches them. `progress_notices` can only open a gate that never closes for
these two.

THE FIX
-------
Add both templates to the outer regex, so they fall under the existing
`progress_notices` gate instead of around it. Nothing new is invented: with
the default `false` they go quiet (the documented intent), and
`compression.progress_notices: true` still shows them for anyone who wants
them. Local/CLI surfaces are unaffected — `_gateway_surface_passes_raw_text`
returns before either gate.

USAGE
-----
    python fix-compaction-status-leak.py [--hermes-agent DIR] [--check]

    --check   report status and exit non-zero if the patch is needed

Idempotent. Writes a .bak-<timestamp> beside the file on first application.
Verified against Hermes 0.20.5. Re-check after `hermes update`: if upstream
reworks the gate, this refuses rather than guessing.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

BROKEN = """    r"|skipping\s+concurrent\s+compression\""""

FIXED = """    r"|skipping\s+concurrent\s+compression"
    # PATCHED (deploy/patches/fix-compaction-status-leak.py): the ordinary
    # compaction start/done statuses were in neither gate, so they reached chat
    # surfaces no matter what compression.progress_notices said -- the inner
    # gate never runs unless this outer regex matches first. Listing them here
    # puts them under that switch, which is what its own comment below claims.
    r"|compacting\s+context"
    r"|context\s+compaction\s+complete\""""

MARKER = "deploy/patches/fix-compaction-status-leak.py"


def default_hermes_agent() -> Path:
    if env := os.environ.get("HERMES_HOME"):
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
    target = root / "gateway" / "run.py"

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
        print("Either upstream reworked the status gate, or the regex changed.")
        print("Read _TELEGRAM_NOISY_STATUS_RE by hand -- do not force it.")
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
    print("Restart the gateway, then confirm by calling")
    print("_prepare_gateway_status_message() with the Zalo platform and both")
    print("status strings -- it must return None for each.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
