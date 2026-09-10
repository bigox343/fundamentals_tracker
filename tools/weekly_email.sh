#!/bin/bash
# Weekly fundamentals note, generated locally and emailed.
#
# Two stages on purpose, and the line between them is the whole design.
#
# tools/weekly_pack.py measures. It puts the dashboard next to the estimate
# revisions, the insider tape and the 13F holdings, and computes the places
# those sources disagree -- which is where the reading lives and which no
# single screen shows.
#
# Claude then WRITES the note from that pack: what the week means, what to
# watch, what would change the conclusion. It is explicitly barred from
# introducing a figure the pack does not contain. Interpretation is what a
# model is for; arithmetic is not. An earlier hand-written version of this
# note carried eight wrong revenue-growth figures, one of which (Deere at
# +2.4% against a real -11.1%) inverted its own conclusion.
#
# Scheduled by com.owen.fundamentals-weekly (Saturday morning). Run by hand any time:
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

NOTE=$(python tools/weekly_pack.py 2>>"$LOG")
if [ -z "$NOTE" ]; then
  say "weekly_pack.py produced nothing; nothing sent"
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
"You are writing this week's fundamentals note for the analyst who owns this" \
"book of 153 names. Below is an evidence pack measured from their own data." \
"" \
"Write the note as prose an experienced analyst would send a colleague:" \
"roughly 500-700 words, plain text, no markdown headers, no bullet soup." \
"Lead with what the week actually means, not with a list of figures." \
"" \
"What makes this note worth reading:" \
"  - The DIVERGENCE section is the spine. A level says what something costs;" \
"    a revision says which way the number behind it is going. Where those" \
"    disagree is the story. Build the note around it." \
"  - Say what you would watch next week, and what would change the reading." \
"  - Where two sources conflict, say so plainly rather than picking one." \
"  - Name the data-quality problems. A multiple that moved because the vendor" \
"    restated it is not news, and the reader needs to know which is which." \
"  - Be willing to say a signal is probably noise, or that a cheap name is" \
"    cheap for a reason. Confident hedging is worse than a clear doubt." \
"" \
"Hard rules:" \
"  - Use ONLY figures that appear in the pack. Do not compute new ones, do not" \
"    round differently, do not recall a number from anywhere else. If you want" \
"    to make a point the pack does not support, drop the point." \
"  - Do not restate the whole pack. Choose what matters and leave the rest." \
"  - If you cite an own-history percentile, carry the coverage caveat once." \
"" \
"Send the finished note as a plain-text email to ${RECIPIENT} with subject" \
"\"${SUBJECT}\". Reply with only SENT or FAILED plus the reason." \
"" \
"--- BEGIN EVIDENCE PACK ---" \
"$NOTE" \
"--- END EVIDENCE PACK ---" \
  | claude -p --allowedTools "mcp__claude_ai_Gmail__send_message" 2>&1 | tail -3)

say "$RESULT"
case "$RESULT" in
  *SENT*) exit 0 ;;
  *)      say "delivery failed -- the pack is still at reports/pack_$(date +%Y%m%d).txt"
          exit 1 ;;
esac
