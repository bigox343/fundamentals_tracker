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
from datetime import date
from typing import NamedTuple

from edgar import sec_get

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/"
               "CIK{cik}/us-gaap/{concept}.json")

# Concept chains. Multiples are built from aggregates rather than per-share
# figures, so that a split cannot corrupt them: a dollar total carries no share
# basis. epsDiluted is fetched only as an independent cross-check in the proof
# harness -- it is never a numerator's source.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "netIncome": ("NetIncomeLoss",),
    "revenue":   ("RevenueFromContractWithCustomerExcludingAssessedTax",
                  "Revenues", "SalesRevenueNet"),
    "opIncome":  ("OperatingIncomeLoss",),
    "dep":       ("DepreciationDepletionAndAmortization",
                  "DepreciationAmortizationAndAccretionNet"),
    "shares":    ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "cash":      ("CashAndCashEquivalentsAtCarryingValue",),
    "debtLT":    ("LongTermDebtNoncurrent",),
    "debtST":    ("LongTermDebtCurrent",),
    "cfo":       ("NetCashProvidedByUsedInOperatingActivities",),
    "capex":     ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "epsDiluted": ("EarningsPerShareDiluted",),
}

# Balance-sheet concepts are reported as an instant -- a level on a date -- not
# as a duration. They must never go through quarterly(), which keeps only
# three-month spans and would drop every one of them, nor be summed over four
# quarters, which would count the same cash four times.
INSTANT_CONCEPTS = frozenset({"cash", "debtLT", "debtST"})


class Fact(NamedTuple):
    ticker: str
    concept: str        # the us-gaap tag as filed
    period_start: str   # '' for an instant fact (balance sheet items)
    period_end: str
    fy: int | None
    fp: str | None
    form: str
    filed: str
    value: float


# Fiscal periods are not exactly 91/365 days: filers use 4-4-5 calendars and
# 52/53-week years. These windows are wide enough for both and narrow enough
# that a 6-month year-to-date figure can never be mistaken for a quarter.
# Measured against AAPL (91/92-day quarters, 365/366-day years) and AVGO
# (4-4-5 fiscal calendar, November year end); the 371-day 52/53-week case is
# also inside the annual window.
_SPANS = (("quarter", 80, 100), ("ytd", 165, 290), ("annual", 350, 380))


def classify_span(start: str, end: str) -> str:
    """'instant' | 'quarter' | 'ytd' | 'annual' | 'other'."""
    if not start:
        return "instant"
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    for name, lo, hi in _SPANS:
        if lo <= days <= hi:
            return name
    return "other"


def parse_concept(ticker: str, tag: str, raw: bytes) -> list[Fact]:
    """Flatten one companyconcept document into Facts.

    Every unit is taken, not only the first: a concept can be reported in more
    than one unit and picking one arbitrarily silently loses the rest. Values
    that are not finite (measured: AAPL NetIncomeLoss carries nulls on some
    dei-only frames) write no Fact, matching the store's write boundary --
    "never observed" must stay distinct from a stored zero.
    """
    payload = json.loads(raw)
    out: list[Fact] = []
    for entries in payload.get("units", {}).values():
        for e in entries:
            value = e.get("val")
            if not isinstance(value, (int, float)) or value != value:
                continue
            if not e.get("end") or not e.get("filed"):
                continue
            out.append(Fact(ticker, tag, e.get("start") or "", e["end"],
                            e.get("fy"), e.get("fp"), e.get("form", ""),
                            e["filed"], float(value)))
    return out


def pick_tag(ticker: str, chain: tuple[str, ...],
             fetched: dict[str, bytes]) -> str | None:
    """The chain member carrying the most facts, or None.

    Held fixed for a ticker's whole series. Mixing tags mid-series renders as a
    revision that never happened: Oracle populates both revenue tags --
    Revenues carries 141 facts and RevenueFromContractWithCustomerExcluding-
    AssessedTax carries 104 -- so a fallback taken per-period would jump
    between two different definitions of the same line depending on which tag
    happened to have a fact for that quarter.
    """
    best, best_n = None, 0
    for tag in chain:
        raw = fetched.get(tag)
        if not raw:
            continue
        n = len(parse_concept(ticker, tag, raw))
        if n > best_n:
            best, best_n = tag, n
    return best


def _sum_first_three(year_facts: list[Fact]) -> tuple[float, str] | None:
    quarters = {f.fp: f for f in year_facts if f.fp in ("Q1", "Q2", "Q3")}
    if len(quarters) != 3:
        return None
    total = sum(f.value for f in quarters.values())
    return total, max(f.period_end for f in quarters.values())


def quarterly(facts: list[Fact]) -> list[Fact]:
    """Three-month facts only, with Q4 reconstructed from the annual figure.

    A 10-K reports the year, never its fourth quarter, so without this every Q4
    is a hole and a trailing-twelve-month sum silently spans five quarters.
    The synthesized Q4 is filed on the 10-K's date, because that is when it
    became knowable -- which is the whole point of a point-in-time series.
    """
    kept = [f for f in facts
            if classify_span(f.period_start, f.period_end) == "quarter"]
    seen = {(f.period_start, f.period_end, f.filed) for f in kept}

    by_year: dict[int, list[Fact]] = {}
    for f in kept:
        if f.fy is not None:
            by_year.setdefault(f.fy, []).append(f)

    for f in facts:
        if classify_span(f.period_start, f.period_end) != "annual":
            continue
        if f.fy is None:
            continue
        parts = _sum_first_three(by_year.get(f.fy, []))
        if parts is None:
            continue
        total, q3_end = parts
        key = (q3_end, f.period_end, f.filed)
        if key in seen:
            continue
        seen.add(key)
        kept.append(Fact(f.ticker, f.concept, q3_end, f.period_end,
                         f.fy, "Q4", f.form, f.filed, f.value - total))
    return kept


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


def fetch_concept(cik: str, tag: str) -> bytes:
    """One companyconcept document. Measured 18-51 KB for AAPL/ORCL tags."""
    return sec_get(CONCEPT_URL.format(cik=cik, concept=tag))
