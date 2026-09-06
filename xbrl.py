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
               "CIK{cik}/{taxonomy}/{concept}.json")

# Concept chains. Multiples are built from aggregates rather than per-share
# figures, so that a split cannot corrupt them: a dollar total carries no share
# basis. epsDiluted is fetched only as an independent cross-check in the proof
# harness -- it is never a numerator's source.
#
# Every tag here is a us-gaap concept: a foreign private issuer filing 20-F
# under IFRS carries none of them. Observed on the real universe as zero
# facts for INFY, SPOT and TSM -- a structural limit of this data source,
# not a sweep failure.
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

# Entity-cover-page facts, from the dei taxonomy rather than us-gaap -- a
# different namespace on the same companyconcept endpoint (CONCEPT_URL takes
# `taxonomy` as a parameter for exactly this). EntityCommonStockShares
# Outstanding is the actual share count SEC requires on every filing's cover
# page, an instant, not an average -- the correct basis for market cap, where
# CONCEPTS["shares"] (WeightedAverageNumberOfDilutedSharesOutstanding) is
# correct for EPS but wrong here for the same reason a period average is
# never a point-in-time count.
DEI_CONCEPTS: dict[str, tuple[str, ...]] = {
    "sharesOutstanding": ("EntityCommonStockSharesOutstanding",),
}

# Balance-sheet concepts are reported as an instant -- a level on a date -- not
# as a duration. They must never go through quarterly(), which keeps only
# three-month spans and would drop every one of them, nor be summed over four
# quarters, which would count the same cash four times. sharesOutstanding is
# the same shape: a count on the filing's cover date, not a span.
INSTANT_CONCEPTS = frozenset({"cash", "debtLT", "debtST", "sharesOutstanding"})

# A concept whose annual figure is not the sum of its quarters -- a rate, a
# ratio, an average, or a per-share figure -- must never have its Q4
# synthesized as FY - (Q1+Q2+Q3): that arithmetic assumes four quarters add up
# to the year, which is true for a flow (net income, revenue) and false for
# these. WeightedAverageNumberOfDilutedSharesOutstanding is a period AVERAGE:
# a filer's annual average sits close to any one quarter's average, not four
# times it, so FY-3Q comes out near -2x the true count. Measured live: MSFT's
# synthesized FY2024/2025/2026 Q4 share facts were all -14.9e9 against a true
# ~7.45e9, and 13 of 149 tickers carried at least one such fact. Task 11
# worked around this downstream in valuation.py (a share count can never be
# <= 0, so a negative one was dropped); the real defect is here, in
# quarterly()'s uniform reconstruction, which had no way to know a tag was
# non-additive. EarningsPerShareDiluted is the same kind of figure for the
# same reason, even though nothing in this codebase sums it today -- it is
# fetched only as an independent cross-check (see CONCEPTS's comment) and a
# future caller that starts using it must not inherit this trap.
NON_ADDITIVE_CONCEPTS = frozenset({
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "EarningsPerShareDiluted",
})


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


# A filer on a 10-K-only cadence can be fourteen months past its last period
# end and still be current, so anything tighter than this would call a live
# annual filer dead. ~15 months.
LIVE_WINDOW_DAYS = 450


def splice(facts: list[Fact], chain: tuple[str, ...]) -> list[Fact]:
    """One series per ticker, spliced from whichever chain members are live.

    `pick_tag` held one tag fixed for a ticker's whole series, chosen by fact
    count. For revenue that reliably elects a tag that stopped being filed in
    2018: eleven years of pre-ASC-606 SalesRevenueNet history outnumbers eight
    years of the modern tag, so the count rule wins the fact-count race and
    loses the one that matters -- whether the tag is still being filed. LMT
    shows the newest tag is not always right either: its ASC 606 tag has only
    7 facts before the filer reverted to Revenues, so "prefer the newest
    chain member" would elect a stub. The discriminator has to be measured per
    ticker, not declared in CONCEPTS: take the live tag with the most facts as
    primary, then fill periods it does not cover from the next-live tag,
    strictly before the primary's oldest period so a period is never reported
    by two tags. That was pick_tag's real justification for holding one tag
    fixed -- Oracle populates both revenue tags across the same years, and a
    period reported by two tags at once renders as a revision that never
    happened. Splicing at a single non-overlapping cut keeps that guarantee
    while still following the tag that is actually current.
    """
    by_tag: dict[str, list[Fact]] = {}
    for f in facts:
        by_tag.setdefault(f.concept, []).append(f)
    if not by_tag:
        return []

    newest = max(f.period_end for fs in by_tag.values() for f in fs)
    newest_d = date.fromisoformat(newest)

    def is_live(tag: str) -> bool:
        tag_newest = max(f.period_end for f in by_tag[tag])
        gap = (newest_d - date.fromisoformat(tag_newest)).days
        return gap <= LIVE_WINDOW_DAYS

    # Primary: the live tag with the most facts. Chain order is the tiebreak,
    # not fact count, so CONCEPTS' preferred-first ordering stays meaningful
    # on a tie.
    primary = None
    for tag in chain:
        if tag not in by_tag or not is_live(tag):
            continue
        if primary is None or len(by_tag[tag]) > len(by_tag[primary]):
            primary = tag
    if primary is None:
        return []

    out = list(by_tag[primary])
    cut = min(f.period_end for f in out)

    # Remaining tags, newest-first, each contributing only strictly earlier
    # periods than anything accumulated so far -- never the same period end
    # twice, and never a phantom revision from an overlapping tag.
    remaining = [t for t in chain if t != primary and t in by_tag]
    remaining.sort(key=lambda t: max(f.period_end for f in by_tag[t]),
                   reverse=True)
    for tag in remaining:
        contributed = [f for f in by_tag[tag] if f.period_end < cut]
        if not contributed:
            continue
        out.extend(contributed)
        cut = min(f.period_end for f in out)

    return sorted(out, key=lambda f: (f.period_end, f.filed))


# Filers on 52/53-week fiscal calendars write a quarter's period_start as
# either the previous period's period_end or the day after it -- both are
# seen in the wild. Zero tolerance would call the same-day filers' own real
# quarters non-tiling; this has to be at least 1. Widened to 4 for slack
# around holiday-adjusted fiscal calendars without opening the window wide
# enough to accept a genuinely disjoint span as if it tiled.
TILE_TOLERANCE_DAYS = 4


def _close(a: str, b: str) -> bool:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) \
        <= TILE_TOLERANCE_DAYS


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

    Containment alone is not enough: it only requires a candidate to fall
    somewhere inside the annual span, not to actually tile it. AMZN publishes
    rolling twelve-month facts alongside its real quarters, and three real
    quarters can fall inside one of those without covering it end-to-end --
    decomposing that produced a Q4 of -518,000,000 against the filer's own
    +82,000,000 Q1. So the three candidates must tile the span: the earliest
    starts within TILE_TOLERANCE_DAYS of annual.period_start, and each next
    one starts within that same tolerance of the previous one's end. A
    rolling-year fact whose real quarters do not begin where it begins fails
    this and is left alone.
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

    ordered = sorted(candidates.values(), key=lambda f: f.period_start)
    if not _close(ordered[0].period_start, annual.period_start):
        return None
    for prev, cur in zip(ordered, ordered[1:]):
        if not _close(cur.period_start, prev.period_end):
            return None

    total = sum(f.value for f in ordered)
    return total, ordered[-1].period_end


def quarterly(facts: list[Fact]) -> list[Fact]:
    """Three-month facts only, with Q4 reconstructed from the annual figure.

    A 10-K reports the year, never its fourth quarter, so without this every Q4
    is a hole and a trailing-twelve-month sum silently spans five quarters.
    The synthesized Q4 is filed on the 10-K's date, because that is when it
    became knowable -- which is the whole point of a point-in-time series.

    Reconstruction runs per concept. Harmless while a caller always handed in
    a single tag, but splice() now hands in several tags spliced into one
    stream, and subtracting one tag's quarters from another tag's annual
    figure would emit a fabricated "Q4" that is really the gap between two
    different definitions of the same line.
    """
    kept = [f for f in facts
            if classify_span(f.period_start, f.period_end) == "quarter"]
    seen = {(f.period_start, f.period_end, f.filed) for f in kept}

    # Ends a kept quarter already covers, per concept, checked on period_end
    # alone. The filer's own Q4 starts the day after Q3 ends; the synthesized
    # one starts on the day Q3 ends -- a one-day gap that defeats any key
    # built from (period_start, period_end, filed), which is exactly how a
    # filer's own Q4 and a synthesized duplicate of it used to both survive.
    #
    # Built once, from the filer's own quarters, and never added to below. SEC
    # republishes the same annual figure in every later filing that carries it
    # as a comparative, and each of those is a separate point in time: the same
    # Q4 becomes knowable again on each filing date, and _known_at picks the
    # newest filed version as of any date. Marking the end covered when the
    # first one is synthesized would keep exactly one Q4 per period, chosen by
    # whichever filing this dict happened to iterate first -- so the surviving
    # `filed` date, and with it two years of TTM history, would depend on the
    # order SEC's JSON arrived in.
    covered_ends: dict[str, set[str]] = {}
    for f in kept:
        covered_ends.setdefault(f.concept, set()).add(f.period_end)

    by_concept: dict[str, list[Fact]] = {}
    for f in facts:
        by_concept.setdefault(f.concept, []).append(f)

    for concept, concept_facts in by_concept.items():
        if concept in NON_ADDITIVE_CONCEPTS:
            continue  # FY - (Q1+Q2+Q3) is meaningless for a non-additive tag
        # Snapshot before the loop below appends to `kept`: a Q4 synthesized
        # earlier in this same pass must not become a candidate quarter for a
        # later annual fact of the same concept.
        concept_quarters = [f for f in kept if f.concept == concept]
        annuals = [f for f in concept_facts
                   if classify_span(f.period_start, f.period_end) == "annual"]
        for f in annuals:
            if f.period_end in covered_ends.get(concept, set()):
                continue  # the filer already published this quarter
            parts = _first_three_within(f, concept_quarters)
            if parts is None:
                continue
            total, q3_end = parts
            # Re-check what is about to be emitted rather than trusting the
            # containment above: AMZN DepreciationDepletionAndAmortization
            # filed 2020-05-01 left a 183-day remainder here, which
            # classify_span itself would call "ytd", not "quarter".
            if classify_span(q3_end, f.period_end) != "quarter":
                continue
            key = (q3_end, f.period_end, f.filed)
            if key in seen:
                continue
            # Hard invariant: nothing emitted here may run backwards.
            # Containment above already forces q3_end < f.period_end, but
            # this is the one check that would have caught the fy/fp bug
            # immediately instead of three digits deep in a downstream
            # valuation series -- it belongs in the code, not only in a test.
            assert q3_end < f.period_end, (
                f"synthesized Q4 would span {q3_end}..{f.period_end} for "
                f"{f.ticker}/{f.concept} filed {f.filed}")
            seen.add(key)
            kept.append(Fact(f.ticker, concept, q3_end, f.period_end,
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


def fetch_concept(cik: str, tag: str, taxonomy: str = "us-gaap",
                  tries: int = 3) -> bytes:
    """One companyconcept document. Measured 18-51 KB for AAPL/ORCL tags.

    `taxonomy` defaults to "us-gaap" so every pre-existing call site is
    unchanged; DEI_CONCEPTS' cover-page facts live under "dei" on the same
    endpoint.

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
    url = CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, concept=tag)
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
