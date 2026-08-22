#!/bin/bash
# Daily driver for the fundamentals tracker, invoked by the LaunchAgent
# com.owen.fundamentals-tracker. Safe to run by hand too:  ./run_daily.sh
#
# Raw CSVs in data/ are never pruned: they are the durable record from which
# history.db is rebuilt, and the estimate files cannot be refetched.
#
# No locking here -- build_dashboard.py takes an fcntl lock itself, because
# macOS ships no flock(1).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/Users/owen/opt/anaconda3/envs/py312/bin/python3"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/run.log"

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

# history.html carries the per-company drilldown and the 13F fund view, and is
# rendered from the store rather than from the fetch -- so it must be rebuilt
# after the run or new quarters land in history.db and are never seen. Its
# failure is not the run's failure: the fetch already succeeded and the data is
# safe, so $status is deliberately left alone here.
if [ $status -eq 0 ]; then
  "$PYTHON" visualize.py >> "$LOG" 2>&1 \
    || echo "visualize FAILED (dashboard and store are unaffected)" >> "$LOG"
  "$PYTHON" tools/build_13f_report.py >> "$LOG" 2>&1 \
    || echo "13F report FAILED (dashboard and store are unaffected)" >> "$LOG"
fi

if [ $status -eq 0 ]; then
  echo "run OK:   $(date '+%Y-%m-%d %H:%M:%S %Z')" >> "$LOG"
else
  echo "run FAILED (exit $status): $(date '+%Y-%m-%d %H:%M:%S %Z')" >> "$LOG"
fi

exit $status
