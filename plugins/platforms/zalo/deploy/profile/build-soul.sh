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
