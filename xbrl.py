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
import time
import urllib.error
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
# dep's chain, measured against the first 25 tickers of the real sweep:
# DepreciationDepletionAndAmortization alone resolved only 16/25 (64%) of
# names carrying OperatingIncomeLoss, against 80-96% for every other concept.
# DepreciationAndAmortization is the single most complete tag for the biggest
# group of the misses (e.g. ADSK 163 facts, AMAT 158) and leads the chain
# because pick_tag selects by fact count, not chain order -- a filer carrying
# both DepreciationAndAmortization and Depreciation resolves to whichever has
# more facts, no special-casing needed. Depreciation is a knowingly weak last
# resort: ADI splits its disclosure into Depreciation (155 facts) and
# AmortizationOfIntangibleAssets (184 facts) with no combined tag at all, so
# resolving to Depreciation alone understates D&A and therefore overstates
# EBITDA. That is acceptable only because the proof harness reconstructs the
# multiple and rejects any (ticker, metric) pair missing Yahoo's published
# value by more than 1% median -- a name like ADI ends up with no EV/EBITDA
# history rather than a wrong one. Summing Depreciation and
# AmortizationOfIntangibleAssets would fix ADI, but that is a structural
# change to the one-tag-per-chain model and belongs in its own task if the
# proof harness later shows it is worth it.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "netIncome": ("NetIncomeLoss",),
    "revenue":   ("RevenueFromContractWithCustomerExcludingAssessedTax",
                  "Revenues", "SalesRevenueNet"),
    "opIncome":  ("OperatingIncomeLoss",),
    "dep":       ("DepreciationAndAmortization",
                  "DepreciationDepletionAndAmortization",
                  "DepreciationAmortizationAndAccretionNet",
                  "Depreciation"),
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
#
# The gaps between bands (101-164 and 291-349 days) are deliberate, not an
# oversight: they fall through to "other" and are dropped rather than
# misclassified. A stub period from a fiscal-year-end change (a filer
# shortening or lengthening one year to move its year-end) would land in one
# of these gaps -- excluding it is the intended, conservative behavior, since
# a stub is neither a clean quarter, half-year, nor full year and has no safe
# duration bucket to be forced into.
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

    On an exact tie the strict `n > best_n` keeps the earlier chain member,
    which is the right default: CONCEPTS lists each chain with the preferred
    tag first (e.g. the ASC 606 revenue tag before the legacy SalesRevenueNet).
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


def _first_three_within(annual: Fact, quarters: list[Fact]) -> tuple[float, str] | None:
    """The three quarters an annual fact's Q4 is built from, or None.

    Grouping by the SEC's own fy/fp labels is a trap: those labels describe
    the *filing's* fiscal context, not the period the fact covers, so every
    filing's prior-year comparative carries the filing's fy/fp, not its own.
    Measured on the committed AAPL fixture: accn 0001193125-10-012085 tags
    both its 2008-12-27 prior-year comparative (val 2,255,000,000) and its
    real 2009-12-26 quarter (val 3,378,000,000) as fy=2010/fp=Q1. Grouping by
    fy/fp let a dict comprehension pick one arbitrarily and produced, on this
    exact fixture, a synthesized Q4 spanning 2011-06-25..2009-09-26 with a
    value of -11,064,000,000 -- 34 of 51 synthesized AAPL netIncome quarters
    were corrupt this way (14/21 AAPL revenue, 18/27 ORCL revenue).

    Containment on the periods themselves needs no label: a candidate quarter
    must fall inside the annual span, end before the annual span ends, and be
    knowable no later than the annual fact itself (filed <= annual.filed --
    a quarter filed after the 10-K cannot have informed it). Duplicates at the
    same (start, end) collapse to the latest filed version, since that is what
    was known when the annual fact was filed. Anything other than exactly
    three such quarters means reconstruction is not safe, so the year is
    skipped rather than guessed at.
    """
    candidates: dict[tuple[str, str], Fact] = {}
    for q in quarters:
        if not (q.period_start >= annual.period_start
                and q.period_end <= annual.period_end
                and q.period_end < annual.period_end
                and q.filed <= annual.filed):
            continue
        key = (q.period_start, q.period_end)
        cur = candidates.get(key)
        if cur is None or q.filed > cur.filed:
            candidates[key] = q
    if len(candidates) != 3:
        return None
    total = sum(f.value for f in candidates.values())
    return total, max(f.period_end for f in candidates.values())


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

    for f in facts:
        if classify_span(f.period_start, f.period_end) != "annual":
            continue
        parts = _first_three_within(f, kept)
        if parts is None:
            continue
        total, q3_end = parts
        key = (q3_end, f.period_end, f.filed)
        if key in seen:
            continue
        # Hard invariant: nothing emitted here may run backwards. Containment
        # above already forces q3_end < f.period_end, but this is the one
        # check that would have caught the fy/fp bug immediately instead of
        # three digits deep in a downstream valuation series -- it belongs in
        # the code, not only in a test.
        assert q3_end < f.period_end, (
            f"synthesized Q4 would span {q3_end}..{f.period_end} for "
            f"{f.ticker}/{f.concept} filed {f.filed}")
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


def fetch_concept(cik: str, tag: str, tries: int = 3) -> bytes:
    """One companyconcept document. Measured 18-51 KB for AAPL/ORCL tags.

    A 404 means "this filer does not use this tag" -- the normal, expected
    outcome for roughly 4-6 of the ~14 tags a full sweep probes per ticker:
    the revenue chain alone has 3 members and a filer uses 1, the dep chain
    has 2, and LongTermDebtCurrent is frequently absent outright. That is the
    whole point of a chain -- pick_tag's fallback only works if the other
    members are allowed to come back empty.

    edgar.sec_get's default retry (3 tries, 1.5+3.0+4.5s backoff) is correct
    for its own 13F callers, where a 404 is anomalous, but is wrong here: it
    turns every absent tag into a measured 9.2s of pure backoff on top of the
    real ~0.2s 404 latency. Across a 153-ticker sweep that is on the order of
    600 absent tags, the actual cause of a run measured at 25 tickers in 25
    minutes projecting to roughly 3 hours against a 2.3-minute estimate. So
    the retry decision is made here, per exception, instead of inside
    sec_get: a 404 returns on the first attempt, while a timeout, 503 or
    reset -- genuinely transient, unlike a missing tag -- still gets the same
    3-try, 1.5/3.0/4.5s-backoff schedule sec_get itself would have given it.
    """
    url = CONCEPT_URL.format(cik=cik, concept=tag)
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return sec_get(url, tries=1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except Exception as exc:        # noqa: BLE001 - retried like sec_get
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]
