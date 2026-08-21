"""One-time backfill of 13F filings into the dated raw archives.

Fetches every wanted quarter for every fund with no CUSIP map, so the archive
is written before the map exists -- the map is then built from the archive
(tools/build_cusip_map.py) and the store ingested from it with no second fetch.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import edgar                      # noqa: E402
from build_dashboard import DATA_DIR, FUNDS, THIRTEENF_QUARTERS  # noqa: E402


def quarter_tag(quarter: str) -> str:
    year, month, _ = quarter.split("-")
    return f"{year}Q{(int(month) - 1) // 3 + 1}"


def main() -> int:
    quarters = edgar.quarter_ends(THIRTEENF_QUARTERS)
    print(f"Backfilling {len(FUNDS)} funds x {len(quarters)} quarters: "
          f"{quarters[-1]} .. {quarters[0]}", flush=True)

    _rows, filings, raw, stale, failed = edgar.collect_13f(
        FUNDS, quarters, cusip_map={}, already={})

    by_quarter = defaultdict(list)
    for rec in raw:
        by_quarter[rec["quarter"]].append(rec)

    total = 0
    for quarter, records in sorted(by_quarter.items()):
        path = DATA_DIR / f"13f_{quarter_tag(quarter)}.csv.gz"
        n = edgar.write_raw_archive(records, path)
        total += n
        print(f"  {path.name:<20} {n:>7} lines", flush=True)

    ok = sum(1 for f in filings if f.status == "ok")
    print(f"\n{ok}/{len(filings)} filings ingested, {total} raw lines")
    if stale:
        print("\nSTALE CIKs (pointing at a wound-down or superseded entity):")
        for line in stale:
            print("  " + line)
    if failed:
        print(f"\nFAILED funds: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
