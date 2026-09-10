#!/bin/bash
# Weekly fundamentals note, generated locally and emailed.
#
# Two stages on purpose. tools/weekly_note.py extracts every figure from the
# built dashboard, so the numbers are code output, not model output -- the
# first hand-written version of this note had eight wrong revenue-growth
# figures, one of which inverted the story (Deere quoted at +2.4% against a
# real -11.1%). Claude is used only to deliver what the script produced, and
# is told not to add or alter a figure.
#
# Scheduled by com.owen.fundamentals-weekly. Run by hand any time:
#   ./tools/weekly_email.sh            # send
#   ./tools/weekly_email.sh --dry-run  # print, send nothing
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

RECIPIENT="${WEEKLY_NOTE_TO:-li.gang.nju@gmail.com}"
LOG="$ROOT/logs/weekly.log"
mkdir -p "$ROOT/logs" "$ROOT/reports"

say(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# The note reads the dashboard, so a stale dashboard means a stale note.
if [ ! -f dashboard.html ]; then
  say "no dashboard.html -- run build_dashboard.py first; nothing sent"
  exit 1
fi
AGE=$(( ($(date +%s) - $(stat -f %m dashboard.html)) / 86400 ))
if [ "$AGE" -gt 3 ]; then
  say "dashboard.html is ${AGE} days old; sending anyway but the note will say so"
fi

NOTE=$(python tools/weekly_note.py 2>>"$LOG")
if [ -z "$NOTE" ]; then
  say "weekly_note.py produced nothing; nothing sent"
  exit 1
fi

if [ "${1:-}" = "--dry-run" ]; then
  echo "$NOTE"
  say "dry run -- nothing sent"
  exit 0
fi

SUBJECT="Fundamentals note — week to $(date '+%-d %b %Y')"

# The prompt goes in on stdin, not as an argument: the note is multi-line and
# passing it as argv both breaks parsing and risks the argument-length limit.
# --allowedTools is scoped to the one Gmail call this needs. Everything else
# still prompts, which a non-interactive run cannot answer -- so a prompt is a
# failure here, not a silent no-op, and it lands in the log.
RESULT=$(printf '%s\n' \
"Send the text between the markers below as a plain-text email to ${RECIPIENT}," \
"with subject \"${SUBJECT}\". Send it verbatim: do not summarise it, do not" \
"reformat it, and above all do not add, round, or alter any figure -- the" \
"numbers were computed by a script and any change you make to one is an error." \
"Reply with only SENT or FAILED plus the reason." \
"" \
"--- BEGIN NOTE ---" \
"$NOTE" \
"--- END NOTE ---" \
  | claude -p --allowedTools "mcp__claude_ai_Gmail__send_message" 2>&1 | tail -3)

say "$RESULT"
case "$RESULT" in
  *SENT*) exit 0 ;;
  *)      say "delivery failed -- the note is still at reports/weekly_$(date +%Y%m%d).txt"
          exit 1 ;;
esac
