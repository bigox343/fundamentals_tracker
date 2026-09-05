"""SEC XBRL company facts: point-in-time fundamentals with filing dates.

A different SEC API from the 13F information tables in edgar.py, and a
different shape: facts keyed by us-gaap concept, each carrying the period it
covers *and the date it was filed*. That second date is the whole reason this
module exists -- it is what makes a historical valuation series free of
lookahead. yfinance serves period ends only.

Knows nothing about SQL, HTML, the universe or yfinance. Borrows exactly one
name from edgar: the SEC-polite HTTP getter.

Usage:  facts = fetch_ticker_facts("AAPL", "0000320193")
"""
from __future__ import annotations

import json
from typing import NamedTuple

from edgar import sec_get

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/"
               "CIK{cik}/us-gaap/{concept}.json")


def parse_ticker_map(raw: bytes) -> dict[str, str]:
    """ticker -> zero-padded 10-digit CIK, from company_tickers.json.

    Zero-padded because every data.sec.gov path wants CIK0000320193, not
    CIK320193, and the file stores the integer.
    """
    payload = json.loads(raw)
    return {row["ticker"]: f"{int(row['cik_str']):010d}"
            for row in payload.values() if row.get("ticker")}


# --------------------------------------------------------------------------- #
# Network                                                                      #
# --------------------------------------------------------------------------- #
def ticker_cik_map() -> dict[str, str]:
    """One request, no key. 10,412 entries as of 2026-09-05."""
    return parse_ticker_map(sec_get(TICKER_MAP_URL))
