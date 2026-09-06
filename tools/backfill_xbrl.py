"""Sweep SEC XBRL into the reported table.

Runs outside build_dashboard.py on purpose: a SEC outage, a changed tag or a
bad parse must not cost you the dashboard, the same reasoning that keeps
portfolio.py off the daily path and record_history() non-fatal.

Steady state makes almost no requests. A ticker is re-checked only once its
newest known filing has gone stale, because a 10-Q lands roughly every 90 days
and nothing changes in between.

Usage:
    python tools/backfill_xbrl.py            # only what is due
    python tools/backfill_xbrl.py --all      # every ticker, the first sweep
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history                      # noqa: E402
import xbrl                         # noqa: E402
from build_dashboard import UNIVERSE  # noqa: E402

DATA_DIR = ROOT / "data"

# A 10-Q lands roughly every 90 days. 85 gives a few days of slack before the
# next one is expected without re-checking a name that just filed.
STALE_DAYS = 85

# SEC allows 10 requests a second. This is the per-request floor, not a target.
FETCH_SLEEP = 0.11


def universe_tickers() -> list[str]:
    return sorted({t for sec in UNIVERSE.values()
                   for sub in sec.values() for t in sub})


def due(tickers, last: dict[str, str], today: str,
        stale_days: int = STALE_DAYS) -> list[str]:
    """Tickers worth re-checking: never swept, or gone stale."""
    now = date.fromisoformat(today)
    out = []
    for t in tickers:
        seen = last.get(t)
        if not seen or (now - date.fromisoformat(seen)).days > stale_days:
            out.append(t)
    return out


# Every concept the sweep probes, us-gaap and dei alike, each tagged with the
# taxonomy its companyconcept endpoint lives under. DEI_CONCEPTS' cover-page
# facts (EntityCommonStockSharesOutstanding) are a different namespace on the
# same API, not a different API -- CONCEPT_URL/fetch_concept already
# parameterize it.
_SWEEP_CONCEPTS: list[tuple[str, tuple[str, ...], str]] = (
    [(name, chain, "us-gaap") for name, chain in xbrl.CONCEPTS.items()]
    + [(name, chain, "dei") for name, chain in xbrl.DEI_CONCEPTS.items()])


def sweep_ticker(ticker: str, cik: str, fetch) -> list:
    """Every concept for one ticker, every chain member the sweep fetched.

    Storing every tag rather than one elected winner costs no additional HTTP
    requests -- the loop below already fetches every tag in the chain, and
    used to throw away all but pick_tag's choice. The store stays a faithful
    mirror of what SEC published; the election (xbrl.splice) happens at read
    time instead, so a future change to that rule needs no re-fetch.

    Returns [] rather than raising: one bad ticker must not abort a sweep of
    152 others.
    """
    facts: list = []
    try:
        for name, chain, taxonomy in _SWEEP_CONCEPTS:
            parsed: list = []
            for tag in chain:
                try:
                    raw = fetch(cik, tag, taxonomy)
                except Exception:       # noqa: BLE001 - a missing tag is normal
                    continue
                time.sleep(FETCH_SLEEP)
                if not raw:
                    continue
                parsed.extend(xbrl.parse_concept(ticker, tag, raw))
            # An instant fact has no duration, so quarterly() would discard it.
            facts.extend(parsed if name in xbrl.INSTANT_CONCEPTS
                         else xbrl.quarterly(parsed))
    except Exception as exc:            # noqa: BLE001 - reported, never raised
        print(f"  {ticker}: {exc}", file=sys.stderr)
        return []
    return facts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="sweep every ticker, not only those due")
    args = ap.parse_args(argv)

    conn = history.connect()
    history.ensure_schema(conn)
    tickers = universe_tickers()
    targets = (tickers if args.all
               else due(tickers, history.last_filed(conn),
                        date.today().isoformat()))
    if not targets:
        print("Nothing due.")
        return 0

    cik_map = xbrl.ticker_cik_map()
    missing = [t for t in targets if t not in cik_map]
    if missing:
        print(f"No CIK for {', '.join(missing)} — skipped")

    total = 0
    for i, ticker in enumerate(t for t in targets if t in cik_map):
        facts = sweep_ticker(ticker, cik_map[ticker], xbrl.fetch_concept)
        total += history.upsert_reported(conn, facts)
        print(f"  [{i+1}] {ticker}: {len(facts)} facts")

    written = history.write_reported_csv(conn, DATA_DIR / "reported.csv.gz")
    conn.close()
    print(f"Stored {total} facts; archive holds {written} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
