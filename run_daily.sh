#!/bin/bash
# Weekly driver for the fundamentals tracker, invoked by the LaunchAgent
# com.owen.fundamentals-tracker. Safe to run by hand too:  ./run_weekly.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/Users/owen/opt/anaconda3/envs/py312/bin/python3"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/weekly.log"

mkdir -p "$LOG_DIR"

# keep the log from growing without bound (trim to last 2000 lines)
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

{
  echo "=============================================================="
  echo "run start: $(date '+%Y-%m-%d %H:%M:%S %Z')"
} >> "$LOG"

cd "$ROOT" || exit 1
"$PYTHON" build_dashboard.py >> "$LOG" 2>&1
status=$?

if [ $status -eq 0 ]; then
  echo "run OK:   $(date '+%Y-%m-%d %H:%M:%S %Z')" >> "$LOG"
else
  echo "run FAILED (exit $status): $(date '+%Y-%m-%d %H:%M:%S %Z')" >> "$LOG"
fi

# prune raw CSVs older than a year; the dashboard only needs the newest
find "$ROOT/data" -name 'fundamentals_*.csv' -type f -mtime +365 -delete 2>/dev/null

exit $status
