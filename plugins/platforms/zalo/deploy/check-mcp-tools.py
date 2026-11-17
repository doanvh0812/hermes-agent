#!/usr/bin/env python3
"""Did the RUNNING gateway actually register its MCP tools?

The failure this exists for, measured on 2026-11-17: the gateway was up,
the task said Running, the bridge said ready, the Odoo API key authenticated
over XML-RPC, and the health check was green -- while the bot had had zero
Odoo tools for four days.

    2026-11-13 04:14:01 WARNING tools.mcp_tool:
      Failed to connect to MCP server 'odoo' (...): CancelledError

A transient timeout during a crash-restart, and nothing retried it:

  * ``odoo`` is an EAGER server (no ``lazy: true``), so a failed connect
    means ``_discover_and_register_server`` raised and the tools were never
    registered at all.
  * the gateway calls ``discover_mcp_tools()`` exactly ONCE, at startup
    (``gateway/run.py``). The only other path is the ``/reload-mcp``
    command. The interactive CLI has a between-turns refresh; the gateway
    does not.

So the gateway holds no Odoo tools until a human restarts it, and every
existing indicator stays green. The first question anyone asks gets
"em không tra được" and that reads like a data problem, not an outage.

WHY THIS PARSES THE LOG. The gateway's tool registry lives in that
process's memory. Nothing else can see it: ``gateway_state.json`` carries
no MCP field, the admin panel is a separate process, and the schema cache
is only consulted for lazy servers. Spawning the MCP server ourselves and
handshaking it -- what a naive check would do -- proves the server is
startable and would have PASSED on every one of those four broken days.
The gateway's own log is the only external evidence of what it registered,
so that is what this reads.

Deliberately NOT done here: probing the MCP server itself every run. That
spawns an Odoo-connecting subprocess on each tick for no new information;
the credential/reachability question is already section 5 of the health
check.

Pure stdlib on purpose -- it runs from both zalo-health.sh and
zalo-health.ps1, under whatever interpreter the host happens to have.

Usage:
    check-mcp-tools.py --log <agent.log> [--server NAME ...] [--grace SEC]

Exit codes:
    0  every expected server is registered under the current gateway life
    1  a server is missing or failed -- the caller should alert
    2  indeterminate (log rotated away, no gateway start found); the caller
       should warn, not alert. A monitor that pages because its own helper
       could not read a file trains people to ignore it.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta

# Lines this reads, verbatim from a live 0.20.5 gateway:
#
#   ...,514 INFO tools.mcp_tool: MCP server 'odoo' (stdio): registered 12 tool(s): mcp__odoo__...
#   ...,224 WARNING tools.mcp_tool: Failed to connect to MCP server 'odoo' (command=...): CancelledError
#   ...,516 INFO gateway.run: Starting Hermes Gateway...
TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
OK = re.compile(r"MCP server '([^']+)'[^:]*: registered (\d+) tool\(s\)")
BAD = re.compile(r"Failed to connect to MCP server '([^']+)'")
# The outer 120s discovery timeout can fire without any per-server line, so
# the servers stranded in it are named only here.
STRANDED = re.compile(r"MCP discovery (?:timed out|interrupted) while .*?connecting[^:]*: (.+)$")
# "Starting Hermes Gateway..." is logged by runner.start(), immediately AFTER
# discovery returns -- which is why a healthy registration timestamp sits a
# few milliseconds BEFORE the boot marker, not after it. The --grace window
# below exists for exactly that ordering.
BOOT = re.compile(r"Starting Hermes Gateway")


def _ts(line):
    m = TS.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
        microsecond=int(m.group(2)) * 1000
    )


def scan(path):
    """Last boot, and the latest success/failure per server."""
    boot = None
    ok, bad = {}, {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            t = _ts(line)
            if t is None:
                continue
            if BOOT.search(line):
                boot = t
                continue
            m = OK.search(line)
            if m:
                ok[m.group(1)] = (t, int(m.group(2)))
                continue
            m = BAD.search(line)
            if m:
                bad[m.group(1)] = (t, line.rstrip().rsplit(": ", 1)[-1][:60])
                continue
            m = STRANDED.search(line)
            if m:
                for name in (n.strip() for n in m.group(1).split(",")):
                    if name:
                        bad[name] = (t, "discovery timed out while connecting")
    return boot, ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument(
        "--server",
        action="append",
        default=[],
        help="expected server name; repeatable. Default: every server the log "
             "has ever registered or failed, which is the set this host runs.",
    )
    # Discovery is bounded by a 120s outer timeout, so a success belonging to
    # the current boot is at most ~2 minutes before the boot marker. 10
    # minutes is comfortably outside that and comfortably inside "this
    # success is from a PREVIOUS gateway life", which is the thing being
    # detected.
    ap.add_argument("--grace", type=int, default=600)
    args = ap.parse_args()

    try:
        boot, ok, bad = scan(args.log)
    except OSError as exc:
        print(f"cannot read gateway log: {exc}")
        return 2

    if boot is None:
        print("no 'Starting Hermes Gateway' in the log (rotated?) -- MCP state unknown")
        return 2

    expected = args.server or sorted(set(ok) | set(bad))
    if not expected:
        print("no MCP server mentioned in the log; nothing to check")
        return 0

    problems, good = [], []
    for name in expected:
        ok_t, count = ok.get(name, (None, 0))
        bad_t, why = bad.get(name, (None, ""))

        if bad_t is not None and (ok_t is None or bad_t > ok_t):
            problems.append(
                f"MCP server '{name}' failed to connect at "
                f"{bad_t:%Y-%m-%d %H:%M} ({why}); the gateway holds none of "
                f"its tools and will not retry on its own"
            )
        elif ok_t is None:
            problems.append(f"MCP server '{name}' has never registered any tool")
        elif ok_t < boot - timedelta(seconds=args.grace):
            # The gateway booted and discovery logged nothing for this server
            # -- the outer-timeout path. Same end state as a failed connect:
            # no tools, no retry.
            problems.append(
                f"gateway started {boot:%Y-%m-%d %H:%M} but the last "
                f"registration of '{name}' was {ok_t:%Y-%m-%d %H:%M} -- this "
                f"gateway life never registered its tools"
            )
        else:
            good.append(f"{name} {count} tools")

    if problems:
        print("; ".join(problems))
        return 1
    print("MCP ok: " + ", ".join(good))
    return 0


if __name__ == "__main__":
    sys.exit(main())
