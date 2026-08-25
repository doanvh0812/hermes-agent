#!/usr/bin/env bash
# Assemble the zalo-bot profile's SOUL.md.
#
# A profile reads its OWN SOUL.md; the root one does not apply. Both the
# guardrail snippet and the skill body go in, so the rules are present in
# every system prompt instead of waiting on a skill context match.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PROFILE="${1:-zalo-bot}"
DEST="$HERMES_HOME/profiles/$PROFILE/SOUL.md"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

[ -d "$(dirname "$DEST")" ] || { echo "profile not found: run 'hermes profile create $PROFILE'"; exit 1; }

{
  cat "$HERE/SOUL.snippet.md"
  echo; echo "---"; echo
  # strip the skill's YAML frontmatter — meaningless inside SOUL.md
  sed '1,/^---$/d; 1,/^---$/d' "$HERE/skills/odoo/odoo-chat-support/SKILL.md"
} > "$DEST"

echo "wrote $DEST ($(wc -l < "$DEST") lines)"

# Hermes scans every context file with the "context" threat scope before it
# enters the system prompt. A hit replaces the WHOLE file with
# "[BLOCKED: SOUL.md contained potential prompt injection]" — so a guardrail
# document that quotes an attack verbatim silently disables every guardrail in
# itself. Nothing else surfaces this: the gateway still logs the right profile
# and reports connected. Fail the build instead.
PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python}"
[ -x "$PY" ] || PY="${HERMES_HOME}/hermes-agent/venv/Scripts/python.exe"
if [ -x "$PY" ]; then
  ( cd "$HERMES_HOME/hermes-agent" && "$PY" - "$DEST" <<'EOF'
import sys
from pathlib import Path
from tools.threat_patterns import scan_for_threats

dest = Path(sys.argv[1])
text = dest.read_text(encoding="utf-8")
if scan_for_threats(text, scope="context"):
    print("\nBUILD FAILED: the assembled SOUL.md trips Hermes' context threat scan.")
    print("It would be replaced wholesale by a [BLOCKED] placeholder, leaving the")
    print("agent with NO guardrails. Offending lines:\n")
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip() and scan_for_threats(line, scope="context"):
            print(f"  line {i}: {line.strip()[:150]}")
    print("\nDescribe such demands instead of quoting them verbatim.")
    sys.exit(1)
print("threat scan: clean — SOUL.md will load in full")
EOF
  ) || exit 1
else
  echo "WARNING: could not locate the Hermes venv python; SKIPPED the threat scan." >&2
  echo "         Run profiles/<name>/verify-soul.py manually before trusting this." >&2
fi
