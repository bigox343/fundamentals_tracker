"""Backfill five years of dividend-unadjusted closes.

One yf.download over the universe, same plumbing as the daily run's own
closeRaw fetch in build_dashboard.record_history. Only needed once; the daily
run keeps it current from here.

period=STORE_PERIOD ("5y"), not HIST_PERIOD ("1y"): this backfill exists to
seed the same five years of history the store otherwise only accumulates one
day at a time, so a valuation history built from it does not start five years
short.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history                                                  # noqa: E402
from build_dashboard import STORE_PERIOD, UNIVERSE, fetch_closes  # noqa: E402


def main() -> int:
    tickers = sorted({t for sec in UNIVERSE.values()
                      for sub in sec.values() for t in sub})
    closes = fetch_closes(tickers, period=STORE_PERIOD, adjusted=False)
    conn = history.connect()
    history.ensure_schema(conn)
    n = history.ingest_prices(conn, closes, metric="closeRaw")
    conn.close()
    got = closes.shape[1] if not closes.empty else 0
    print(f"Stored {n:,} closeRaw rows over {got} tickers.")
    if got < len(tickers):
        print(f"  WARNING: {len(tickers) - got} of {len(tickers)} universe "
              f"tickers returned no closeRaw history at all.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
