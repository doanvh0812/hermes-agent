#!/usr/bin/env python3
"""Wire the health check's alert channel to Telegram, and prove it delivers.

The health check detects everything it was built to detect and then writes the
alert to a log file on the box that is broken. That is gap #9 in DEPLOY.md: an
alert nobody reads is not monitoring. Telegram is the channel on purpose --
it is a DIFFERENT transport from the one being monitored, so a dead Zalo (or a
dead bridge, or a gateway with no MCP tools) cannot swallow the notification
about itself.

This writes two keys into the profile .env:

    ZALO_ALERT_TG_TOKEN   bot token from @BotFather
    ZALO_ALERT_TG_CHAT    chat id to deliver to

Deliberately NOT named TELEGRAM_BOT_TOKEN / TELEGRAM_HOME_CHANNEL, even though
both health scripts accept those as a fallback. The gateway reads a profile's
.env for PLATFORM credentials; a TELEGRAM_BOT_TOKEN sitting in the zalo-bot
profile is one `platforms.telegram.enabled` away from the bot silently
connecting to Telegram as a second chat surface. The alert-specific name can
never be mistaken for that.

Usage:
    setup-telegram.py --token <bot-token> [--chat <id>] [--env <path>]
                      [--dry-run] [--no-test]

With no --chat it resolves the chat id from getUpdates: send the bot any
message first (press Start in the chat), then run this.

Pure stdlib, and its own strings are ASCII: it runs under whatever interpreter
the host has, and a Windows console on cp1252 renders non-ASCII as '?'. Same
constraint as check-mcp-tools.py next door.

That is not enough on its own, though -- the strings Telegram hands back are
not ours. A bot named in Vietnamese crashed this script outright on the
cp1252 console (UnicodeEncodeError on the '@name (First Last)' line), so
stdout is reconfigured below to degrade such characters instead. A setup run
must not die over how a bot chose to spell its own name.
"""

import argparse
import json
import os
import shutil
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ZALO_ALERT_TG_API repoints this (and both health scripts) at a local
# listener, so the whole flow -- including the .env write, which touches the
# file holding the Zalo session and the Odoo key -- can be rehearsed before it
# runs for real.
API_BASE = os.environ.get("ZALO_ALERT_TG_API", "https://api.telegram.org")
KEY_TOKEN = "ZALO_ALERT_TG_TOKEN"
KEY_CHAT = "ZALO_ALERT_TG_CHAT"

# Bot names, chat titles and Telegram's own error descriptions can hold any
# script; this console is cp1252. Degrade what it cannot represent rather than
# raising UnicodeEncodeError in the middle of a setup run. Only affects what
# is PRINTED -- the values written to .env and sent to Telegram are untouched.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def mask(secret):
    """Tokens must never reach a terminal, a log, or a transcript in full."""
    if not secret:
        return "(empty)"
    return "%s...(%d chars)" % (secret[:6], len(secret))


def call(token, method, params=None, timeout=15):
    """One Telegram API call. Returns (ok, result_or_error_string)."""
    url = "%s/bot%s/%s" % (API_BASE.rstrip("/"), token, method)
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # Telegram puts the real reason in the body of a 4xx, not the status.
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            return False, body.get("description", str(exc))
        except Exception:
            return False, "HTTP %s" % exc.code
    except Exception as exc:
        return False, str(exc)
    if not body.get("ok"):
        return False, body.get("description", "unknown error")
    return True, body.get("result")


def resolve_chat(token):
    """Newest chat that has spoken to this bot.

    getUpdates only works when no webhook is installed and only covers the
    last 24h, so both failures get an actionable message instead of a stack
    trace.
    """
    ok, info = call(token, "getWebhookInfo")
    if ok and isinstance(info, dict) and info.get("url"):
        return None, ("this bot has a webhook installed (%s), so getUpdates "
                      "returns nothing -- pass --chat <id> explicitly"
                      % info["url"])

    ok, updates = call(token, "getUpdates", {"limit": 100, "timeout": 0})
    if not ok:
        return None, "getUpdates failed: %s" % updates

    found = []
    for upd in updates or []:
        # A private chat arrives as 'message'; a group the bot was added to may
        # only ever produce 'my_chat_member' until someone talks.
        for key in ("message", "edited_message", "channel_post",
                    "my_chat_member", "chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if chat and chat.get("id") is not None:
                found.append((upd.get("update_id", 0), chat))
                break
    if not found:
        return None, ("no chat has messaged this bot yet -- open the bot in "
                      "Telegram, press Start (or send any message), then "
                      "re-run this")
    found.sort(key=lambda p: p[0])
    return found[-1][1], None


def read_env(path):
    """Raw bytes + the file's dominant line ending.

    Read as bytes on purpose: this file holds the Zalo session token and the
    Odoo key. Decoding it, editing text, and re-encoding risks mangling a
    byte this script has no business touching.
    """
    if not os.path.exists(path):
        return b"", b"\r\n" if os.name == "nt" else b"\n"
    with open(path, "rb") as fh:
        raw = fh.read()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return raw, (b"\r\n" if crlf >= lf else b"\n")


def write_env(path, raw, eol, pairs, dry_run):
    """Replace or append each key, touching nothing else. Atomic, backed up."""
    lines = raw.split(b"\n") if raw else []
    # Strip the trailing empty element a final newline produces, so appending
    # does not leave a blank line in the middle of the file.
    trailing_newline = bool(lines) and lines[-1] == b""
    if trailing_newline:
        lines.pop()
    lines = [ln.rstrip(b"\r") for ln in lines]

    changed = []
    for key, value in pairs:
        kb = key.encode()
        new = kb + b"=" + value.encode()
        for i, ln in enumerate(lines):
            if ln.startswith(kb + b"="):
                if ln == new:
                    changed.append("%s unchanged" % key)
                else:
                    lines[i] = new
                    changed.append("%s replaced" % key)
                break
        else:
            lines.append(new)
            changed.append("%s added" % key)

    out = eol.join(lines) + eol
    if dry_run:
        return changed, None

    backup = None
    # Only back up when something actually changes. Every backup is a full copy
    # of a file holding the Zalo session, the Odoo key and the OpenAI key, so a
    # re-run that changes nothing must not leave another one lying around.
    if out == raw:
        return changed, None
    if os.path.exists(path):
        backup = "%s.bak-%s" % (path, datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)

    tmp = path + ".tmp-setup-telegram"
    with open(tmp, "wb") as fh:
        fh.write(out)
    if os.path.exists(path):
        # Carry the original mode across: on Linux this file should stay 600,
        # and a fresh temp file would not be.
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return changed, backup


def main():
    ap = argparse.ArgumentParser()
    # Not required: with --token omitted the token is read from stdin, which
    # keeps it out of shell history and out of the process table, where
    # anything on the box can read a command line.
    ap.add_argument("--token", help="bot token from @BotFather; omit to pipe "
                                    "it on stdin instead")
    ap.add_argument("--chat", help="chat id; default: resolved from getUpdates")
    ap.add_argument("--env", help="profile .env; default: $HERMES_HOME/.env")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change and still send the test "
                         "message, but write nothing to .env")
    ap.add_argument("--no-test", action="store_true",
                    help="skip the delivery test (not recommended: the test "
                         "is the only proof the channel works)")
    args = ap.parse_args()

    token = (args.token or sys.stdin.readline()).strip()
    if not token:
        print("FAIL: no token (pass --token, or pipe it on stdin)")
        return 1
    env_path = args.env or os.path.join(
        os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")), ".env")

    print("token: %s" % mask(token))

    ok, me = call(token, "getMe")
    if not ok:
        print("FAIL: token rejected by Telegram: %s" % me)
        return 1
    print("bot:   @%s (%s)" % (me.get("username", "?"), me.get("first_name", "")))

    if args.chat:
        chat_id = args.chat.strip()
        chat_label = "(given on the command line)"
    else:
        chat, err = resolve_chat(token)
        if err:
            print("FAIL: %s" % err)
            return 1
        chat_id = str(chat["id"])
        title = chat.get("title") or " ".join(
            x for x in (chat.get("first_name"), chat.get("last_name")) if x)
        chat_label = "%s %s" % (chat.get("type", "?"), title or "")
    print("chat:  %s %s" % (chat_id, chat_label))

    if not args.no_test:
        host = os.environ.get("COMPUTERNAME") or os.uname().nodename
        text = ("Kenh canh bao Zalo bot da noi xong.\n"
                "Host: %s\n"
                "Tu gio su co bridge / token / Odoo / MCP se bao vao day." % host)
        ok, err = call(token, "sendMessage",
                       {"chat_id": chat_id, "text": text})
        if not ok:
            print("FAIL: cannot deliver to chat %s: %s" % (chat_id, err))
            print("      (a bot cannot message a user who has not started it; "
                   "in a group, check the bot is a member)")
            return 1
        print("test:  delivered")

    raw, eol = read_env(env_path)
    changed, backup = write_env(
        env_path, raw, eol,
        [(KEY_TOKEN, token), (KEY_CHAT, chat_id)], args.dry_run)

    print("env:   %s" % env_path)
    print("       eol=%s; %s" % ("CRLF" if eol == b"\r\n" else "LF",
                                 ", ".join(changed)))
    if args.dry_run:
        print("DRY RUN -- nothing written")
        return 0
    if backup:
        # Say this out loud: the copy holds every secret the original does.
        print("       backup: %s (contains secrets -- delete when happy)" % backup)
    print("OK: alerts will now be delivered to Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
