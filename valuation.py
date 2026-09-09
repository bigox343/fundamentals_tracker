"""Point-in-time fundamentals plus prices, as daily multiple series.

Every figure is an aggregate -- a dollar total -- rather than a per-share
number, so that a split cannot corrupt it. The share basis enters exactly once,
in the market cap, and is reconciled there.

Knows nothing about fetching, HTML, or SQL writes. Frames go in, frames come
out, so every trap below has a test that does not touch a database.

Caller contract for `_known_at` / `ttm_at` / `ttm_series`: pass one fact stream
per period shape for a single concept -- i.e. the output of `xbrl.quarterly()`,
never a raw mix of quarterly, year-to-date and annual facts for the same
concept. `ttm_at` sums by `period_end` on the assumption that each period_end
appearing in the stream represents exactly one quarter; a year-to-date fact
sharing a period_end with the quarter it contains would otherwise be summed
alongside it. `_known_at` cannot repair a violation of that contract -- it has
no way to know which of two same-period_end facts the caller meant -- so it
raises rather than silently letting one of them consume a TTM slot it should
not.
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd

import history
from history import reported
import xbrl

TTM_QUARTERS = 4

# A split shows up as a near-integer jump in the diluted share count between
# consecutive filings. 1.5 is comfortably above any plausible issuance and
# below the smallest split anyone runs (2:1).
SPLIT_MIN_RATIO = 1.5


# How far two filings may disagree about where one quarter started before the
# two are treated as different period shapes rather than two spellings of the
# same period. Measured across the whole store: the largest real disagreement
# is 9 days (52/53-week filers such as ADBE, ADI and COST, whose fiscal
# calendar shifts a boundary by up to a week between filings), and there is
# nothing between 10 and 100 days. The shape this must still catch -- a
# year-to-date fact sharing a period_end with the quarter it contains -- sits
# at 93 days. 15 leaves both margins wide.
START_TOLERANCE_DAYS = 15


def _shape_conflict(lo: str, hi: str) -> bool:
    """Whether two spans ending on one date are different period shapes."""
    if bool(lo) != bool(hi):
        return True     # an instant and a duration are never the same period
    if not lo:
        return False
    return abs((date.fromisoformat(hi) - date.fromisoformat(lo)).days) \
        > START_TOLERANCE_DAYS


def _known_at(facts, on: str) -> dict:
    """Newest value per period among the facts filed on or before `on`.

    An amendment supersedes its original only once it has been filed. Before
    that date the market had the original number, and a point-in-time series
    must say so.

    Two guards run here rather than being trusted from upstream, because a
    single corrupted fact reaching this function can poison a TTM sum for
    essentially any ticker with more than a couple years of filing history:

    - A duration fact (non-empty period_start) must have period_start <
      period_end. xbrl's parser now enforces this at the source as a hard
      invariant, but data/reported.csv.gz and the store were built partly
      before that guarantee existed, and nothing stops a future bad fact from
      reaching here. The Task 5 fixture produced exactly this shape: a
      synthesized Q4 spanning 2011-06-25..2009-09-26 valued at -$11.06B --
      chronologically impossible, and large enough to swing a TTM sum by
      itself. Instant concepts (xbrl.INSTANT_CONCEPTS: cash, debtLT, debtST)
      legitimately carry an empty period_start -- they are levels on a date,
      not spans -- and are exempt from this check since there is no
      "backwards" for a level to be.
    - No two surviving facts may describe the same period_end with spans that
      are genuinely different shapes (see the module docstring's caller
      contract). A year-to-date fact sharing a period_end with the quarter it
      contains would otherwise be summed alongside it.

    The period is keyed on period_end alone, not on (period_start,
    period_end). Filers rewrite a quarter's start date between filings --
    measured across the whole store, 255 (ticker, concept, period_end) groups
    carry more than one spelling of the start, with a maximum spread of 9
    days and nothing at all between 10 and 100. Keying on the pair treats the
    two spellings as two periods, and 24 of those 255 are real restatements
    where the value changed as well: MSFT's 2016-09-30 net income by 17%,
    MSI's 2010-12-31 revenue by 61%, NTAP's 2022-01-28 revenue by 51%. Summing
    both spellings would count one restated quarter twice, at two different
    values. Keyed on period_end, newest filed wins, which is what a
    point-in-time series wants: the original before the restatement was
    published, the restated figure after.
    """
    best: dict = {}
    spans: dict[str, tuple[str, str]] = {}   # period_end -> (min, max) start
    for f in facts:
        if f.filed > on:
            continue
        # An empty period_start (instant concepts) is lexicographically below
        # any real date, so it never trips ">= period_end" on its own; the
        # explicit "f.period_start and" guard is belt-and-suspenders for a
        # fact that also has an empty period_end, which should not occur --
        # xbrl.parse_concept never writes a Fact without one -- but costs
        # nothing to guard against here too.
        if f.period_start and f.period_start >= f.period_end:
            continue

        lo, hi = spans.get(f.period_end, (f.period_start, f.period_start))
        lo, hi = min(lo, f.period_start), max(hi, f.period_start)
        if _shape_conflict(lo, hi):
            raise ValueError(
                f"period_end {f.period_end} carries spans starting {lo!r} and "
                f"{hi!r} -- more than {START_TOLERANCE_DAYS} days apart, so "
                "these are different period shapes. Pass one stream per shape "
                "(xbrl.quarterly() output).")
        spans[f.period_end] = (lo, hi)

        cur = best.get(f.period_end)
        if cur is None or f.filed > cur.filed:
            best[f.period_end] = f

    return best


def ttm_at(facts, on: str) -> float | None:
    """Trailing-twelve-month total as it was knowable on `on`.

    "The four newest facts by period_end" is not the same claim as "four
    quarters covering about a year" -- it is only safe when the concept has
    dense quarterly coverage. Cash-flow concepts do not: cfo has a median of
    28 stored facts per ticker and capex 32, against netIncome's 170 (MSFT
    alone publishes 76 quarterly cfo facts against 212 for net income), so
    with roughly 1.4 quarters available per year the four newest can be
    scattered across three years. Summed anyway, that produces a wrong number
    that looks exactly like a real TTM -- worse than an absent one, because it
    carries no signal that anything is off. classify_span's annual band
    (350-380 days) already accommodates 4-4-5 calendars and 52/53-week years,
    so it is reused here rather than inventing a second tolerance: if the four
    newest facts, taken together, do not span about a year, there is no TTM.
    """
    known = _known_at(facts, on)
    if len(known) < TTM_QUARTERS:
        return None
    newest = sorted(known.values(), key=lambda f: f.period_end,
                    reverse=True)[:TTM_QUARTERS]
    oldest = min(newest, key=lambda f: f.period_start)
    if xbrl.classify_span(oldest.period_start, newest[0].period_end) != "annual":
        return None
    return float(sum(f.value for f in newest))


def ttm_series(facts, dates) -> pd.Series:
    """A daily step function, moving only on filing dates.

    Computed once per distinct filing date rather than once per day: the value
    can only change when something is filed, and 1,270 dates against ~70
    filings is 18x more work for the same answer.
    """
    index = pd.DatetimeIndex(dates)
    if not len(facts):
        return pd.Series(index=index, dtype=float)
    steps = sorted({f.filed for f in facts})
    values = {s: ttm_at(facts, s) for s in steps}
    # pd.Series(dict, index=...) does not align string keys against a
    # DatetimeIndex -- it silently produces all-NaN. Build from the dict
    # alone, then convert the index, matching the pattern in shares_series
    # below.
    stepped = pd.Series(values)
    stepped.index = pd.DatetimeIndex(stepped.index)
    stepped = stepped.sort_index()
    return stepped.reindex(stepped.index.union(index)).ffill().reindex(index)


def _drop_impossible_shares(share_facts):
    """Drop a share fact that cannot be real: a share count is never <= 0.

    The root cause this guards against is fixed: xbrl.quarterly() used to
    synthesize a Q4 for WeightedAverageNumberOfDilutedSharesOutstanding as
    FY - (Q1+Q2+Q3), correct for an additive concept but wrong for a period
    AVERAGE, and came out near -2x the true count (Task 11 measured MSFT's
    synthesized Q4s at -14.9e9 against a true ~7.45e9). Task 11.5 moved that
    fix into xbrl.quarterly() itself (NON_ADDITIVE_CONCEPTS), verified by
    guard-mutation, so no *future* sweep can write one of these again.

    This filter stays anyway, because the fix does not reach backwards: the
    live store (data/history.db) was populated by the pre-fix code and still
    holds 4,526 such rows across 133 of 149 tickers -- upsert_reported only
    ever inserts or updates a row at (ticker, concept, period_end,
    period_start, filed), so a concept the fixed code no longer emits simply
    never overwrites the bad row already sitting at that key. Measured
    directly: with this filter removed, NKE and TEAM's reconstructed market
    cap for 2026-09-06 was off by 299% and 308% (their most recently filed
    quarter is currently one of these poisoned Q4s), against 0.0% and n/a
    with it restored. A one-time
    `DELETE FROM reported WHERE concept='WeightedAverageNumberOfDilutedSharesOutstanding' AND value<0`
    against the live store, followed by rewriting data/reported.csv.gz,
    would let this filter retire; this task's sandbox would not grant
    permission to run that statement (see task-11.5-report.md), so the
    cleanup is left for whoever next touches the store with that access.
    """
    return [f for f in share_facts if f.value > 0]


def split_factors(share_facts) -> pd.Series:
    """Multiplier taking each period's as-filed share count to today's basis.

    Recovered from the share count itself: a 10:1 split appears as a 10x jump
    between consecutive filings, which no issuance or buyback can imitate.
    Snapped to the nearest sensible ratio by history._snap_split, which exists
    for exactly this in the 13F path -- share counts there are as-filed while
    stored closes are back-adjusted, and a 25:1 split rendered as a manager
    adding 2,400%.

    Detection runs WITHIN a concept, never across two. A caller may hand in a
    stream spliced from more than one source -- shares_series does exactly
    that, preferring the dei cover-page count and falling back to the
    weighted average -- and the step where one source hands over to the other
    is not a corporate event. Measured on the live store, 12 tickers show such
    a step. Most are unit mismatches so extreme that history._snap_split
    already refuses them (MO 2,069 -> 2,071,359,145, a filer reporting the
    average in thousands against a count in units; CSX 958x). The dangerous
    ones are the plausible ratios: ALAB steps 52,532,000 -> 155,701,301, which
    is 2.96 and snaps to a clean 3:1, and CRDO steps 1.96 and snaps to 2:1.
    Neither split. Grouping by concept costs nothing for the single-source
    callers and makes the guard intrinsic, so no caller has to remember it.

    Filters through _drop_impossible_shares independently of shares_series --
    both are public functions callers may use directly (shares_series has its
    own tests calling it standalone below), so each must be robust to a
    poisoned fact on its own rather than relying on the other to have cleaned
    the input first.
    """
    share_facts = _drop_impossible_shares(share_facts)
    by_concept: dict[str, list] = {}
    for f in share_facts:
        by_concept.setdefault(f.concept, []).append(f)

    pieces = []
    for facts in by_concept.values():
        ordered = sorted({f.period_end: f for f in facts}.values(),
                         key=lambda f: f.period_end)
        ends = [f.period_end for f in ordered]
        factors = pd.Series(1.0, index=ends)
        cumulative = 1.0
        for i in range(len(ordered) - 1, 0, -1):
            prev, cur = ordered[i - 1].value, ordered[i].value
            ratio = (cur / prev) if prev else 1.0
            if ratio >= SPLIT_MIN_RATIO:
                cumulative *= history._snap_split(ratio)
            factors.iloc[i - 1] = cumulative
        pieces.append(factors)
    if not pieces:
        return pd.Series(dtype=float)
    out = pd.concat(pieces)
    return out[~out.index.duplicated(keep="first")]


def _prefer_live(preferred: list, fallback: list) -> list:
    """Facts from `preferred` wherever it is live, `fallback` elsewhere.

    This is the dei:EntityCommonStockSharesOutstanding vs WeightedAverage
    NumberOfDilutedSharesOutstanding problem, and it is the same shape
    xbrl.splice already solves for one concept's own chain members: a tag
    that stopped being filed must not be trusted just because it used to be
    the right one. CHTR's dei series stops at 2016-08-09 -- unguarded, its
    decade-old count forward-fills straight into 2026 and overstates market
    cap by 101%.

    Unlike splice's election (the live tag with the MOST facts wins), the
    preference order here is fixed: `preferred` wins whenever it is live,
    regardless of which side has more facts. A cover-page count tagged once
    per filing will almost always have fewer entries than a weighted-average
    figure re-filed as every later quarter's own comparative, and a
    fact-count contest would hand the market-cap basis right back to the
    average it exists to replace.
    """
    if not preferred:
        return fallback
    if not fallback:
        return preferred
    newest = max(f.period_end for f in preferred + fallback)
    preferred_newest = max(f.period_end for f in preferred)
    gap = (date.fromisoformat(newest)
           - date.fromisoformat(preferred_newest)).days
    if gap > xbrl.LIVE_WINDOW_DAYS:
        return fallback   # preferred has gone stale; none of it is usable
    cut = min(f.period_end for f in preferred)
    return preferred + [f for f in fallback if f.period_end < cut]


def _one_per_filing(facts: list) -> list:
    """One cover-page count per (filed, period_end).

    This function used to SUM the group, on the theory that a multi-class
    filer tags EntityCommonStockSharesOutstanding once per class and the
    market-cap basis wants the total. Checked against SEC across 70 tickers:
    every group with more than one entry -- 5 of them -- holds a filing and
    its own same-day amendment carrying the IDENTICAL number, never two
    classes. AMD's 10-K and 10-K/A both say 1,630,410,843 on 2026-02-04;
    CHTR and BKNG have the same shape. Zero groups anywhere had differing
    values.

    So summing would have doubled the share count and halved the market cap
    for those filers. The store's primary key -- (ticker, concept,
    period_end, period_start, filed) -- collapsed the siblings before this
    function ever saw them, which is the only reason the bug never fired.
    Relying on that is not a guard, so the collapse is explicit here and
    keeps the newest form rather than adding two copies of one number.

    If SEC ever does serve genuinely per-class entries, they are
    indistinguishable here: the companyconcept endpoint carries no class
    dimension. That would need the class in the store's key, not a sum at
    read time.
    """
    by_key: dict[tuple[str, str], object] = {}
    for f in facts:
        by_key[(f.filed, f.period_end)] = f
    return list(by_key.values())


def shares_series(share_facts, dates, dei_facts=None) -> pd.Series:
    """Split-adjusted shares outstanding, forward-filled onto `dates`.

    `dei_facts` (dei:EntityCommonStockSharesOutstanding, the actual count SEC
    requires on every filing's cover page) is preferred over `share_facts`
    (WeightedAverageNumberOfDilutedSharesOutstanding, correct for EPS but a
    period average rather than a point-in-time count) via _prefer_live,
    wherever dei is live; it falls back to `share_facts` for any stretch dei
    does not cover. Defaults to None so a caller with only the weighted-
    average series keeps behaving exactly as before.

    Filters through _drop_impossible_shares independently of split_factors --
    see that function's docstring for why the guard still has live rows to
    catch even though its root cause (xbrl.quarterly()'s Q4 synthesis) is
    fixed. dei_facts is not filtered here: it is never run through
    quarterly() (INSTANT_CONCEPTS), so it cannot carry this defect.
    """
    fallback = _drop_impossible_shares(share_facts)
    preferred = _one_per_filing(dei_facts or [])
    chosen = _prefer_live(preferred, fallback)
    # Split detection runs PER SOURCE, never across the splice. The two series
    # are not even on the same unit basis for every filer: measured on the live
    # store, 12 tickers show a cross-source step that split_factors would read
    # as a split, and the ratios are not subtle -- MO 2,069 -> 2,071,359,145
    # (a filer reporting the weighted average in thousands against a
    # cover-page count in units), CSX 958x, HUBS 5,344x. Snapping one of those
    # and applying it to every earlier period would rescale the whole
    # pre-boundary history by six orders of magnitude. A split is a corporate
    # event visible within one series; a step between two different series
    # measuring two different things is not evidence of one.
    factors = split_factors(chosen)
    # Preference has to survive the collapse to a step function, not just the
    # splice. 5,048 filed dates in the live store carry BOTH a cover-page
    # count and a weighted-average fact -- routine, because a 10-K republishes
    # older periods as comparatives under its own filing date. dict() keeps
    # the last write per key and sorted() is stable, so ordering by `filed`
    # alone handed those dates to whichever list _prefer_live concatenated
    # second, which is the fallback. Measured before this fix: 7 of 125
    # tickers (ACN, CHTR, CMCSA, NKE, QSR, UPS, WDAY) ended on the average
    # rather than the count, up to 10% wrong -- silently undoing the market
    # cap basis this argument exists to provide. Sorting preferred LAST within
    # a filed date makes it win the overwrite.
    preferred_set = set(preferred)
    adjusted = [
        (f.filed, f.value * float(factors.get(f.period_end, 1.0)))
        for f in sorted(chosen, key=lambda f: (f.filed, f in preferred_set))
    ]
    if not adjusted:
        return pd.Series(index=pd.DatetimeIndex(dates), dtype=float)
    stepped = pd.Series(dict(adjusted))
    stepped.index = pd.DatetimeIndex(stepped.index)
    stepped = stepped.sort_index()
    index = pd.DatetimeIndex(dates)
    return stepped.reindex(stepped.index.union(index)).ffill().reindex(index)


# Which aggregates each multiple needs. Every one is a dollar total, so none
# carries a share basis a split could corrupt.
METRIC_SPECS: dict[str, tuple[str, ...]] = {
    "trailingPE": ("netIncome",),
    "ps":         ("revenue",),
    "evEbitda":   ("opIncome", "dep", "debtLT", "debtST", "cash"),
    "fcfYield":   ("cfo", "capex"),
}

# Below this many observations an "own range" is not a range. Roughly one
# fiscal year of trading days.
#
# The Task 11 brief set this to 250 and then tested it with a 5-observation
# series, so the constant and its own test contradicted each other. That was
# resolved by dropping the floor to 3, on the reasoning that 3 points are the
# fewest with an interior position and that callers could impose their own
# calendar floor. The test was the wrong half to keep. A percentile drawn from
# 3 observations, painted onto a cell labelled "vs own history", is precisely
# the plausible-wrong-number this module exists to refuse -- and a floor that
# every caller must remember to re-apply is one a caller will forget.
#
# Restoring it costs nothing measurable: of the pairs carrying any value at
# all, ps has 134 with at least 250 observations and trailingPE 124, against a
# median of 1,255 -- the full five-year window. The floor excludes degenerate
# series, not real ones.
MIN_HISTORY = 250


def _ttm(facts_by_tag: dict, name: str, dates) -> pd.Series:
    return ttm_series(facts_by_tag.get(name, []), dates)


def latest_series(facts, dates) -> pd.Series:
    """The newest reported level as of each date, forward-filled.

    For balance-sheet items only. Cash is a level on a date, not a flow, so
    summing four quarters of it would count the same money four times -- and
    running it through quarterly() first would drop it entirely, since an
    instant fact has no duration to match.
    """
    index = pd.DatetimeIndex(dates)
    if not len(facts):
        return pd.Series(index=index, dtype=float)
    known = {}
    for f in sorted(facts, key=lambda f: (f.filed, f.period_end)):
        known[f.filed] = float(f.value)
    stepped = pd.Series(known)
    stepped.index = pd.DatetimeIndex(stepped.index)
    stepped = stepped.sort_index()
    return stepped.reindex(stepped.index.union(index)).ffill().reindex(index)


def _level(facts_by_tag: dict, name: str, dates) -> pd.Series:
    return latest_series(facts_by_tag.get(name, []), dates)


def multiple_series(ticker: str, facts_by_tag: dict,
                    closes_raw: pd.Series) -> pd.DataFrame:
    """Daily trailingPE, ps, evEbitda and fcfYield for one ticker.

    Undefined ratios are NaN rather than negative, the same rule the peer frame
    applies: a negative denominator makes the multiple absent, not cheap.
    """
    dates = pd.DatetimeIndex(closes_raw.index)
    # Two share bases, because these are two kinds of multiple.
    #
    # An aggregate multiple -- P/S, EV/EBITDA -- divides what the whole company
    # costs by what the whole company earns or sells, so it wants the actual
    # count outstanding. Measured against Yahoo's own market cap, that basis is
    # exact: median error 0.000004%, and P/S lands at a 0.00% median.
    #
    # P/E is not an aggregate multiple. It is price divided by earnings per
    # share, and EPS is defined on the WEIGHTED AVERAGE diluted count -- an
    # average over the period the earnings were earned, not a snapshot on the
    # day. Using the outstanding count here silently reprices a year of
    # earnings onto today's share base. Measured over 123 names, per-share
    # basis against Yahoo's trailingPE: median error 0.96% and 51% within 1%,
    # against 1.58% and 33% for the outstanding basis.
    #
    # Fixing market cap made P/E look WORSE before this split, which is how
    # the difference surfaced: a market cap 0.9% low had been cancelling most
    # of the per-share gap, and correcting one exposed the other.
    outstanding = shares_series(facts_by_tag.get("shares", []), dates,
                                dei_facts=facts_by_tag.get("sharesOutstanding", []))
    weighted = shares_series(facts_by_tag.get("shares", []), dates)
    cap = closes_raw.astype(float) * outstanding

    out = pd.DataFrame(index=dates)

    earnings = _ttm(facts_by_tag, "netIncome", dates)
    per_share_cap = closes_raw.astype(float) * weighted
    out["trailingPE"] = (per_share_cap / earnings).where(earnings > 0)

    revenue = _ttm(facts_by_tag, "revenue", dates)
    out["ps"] = (cap / revenue).where(revenue > 0)

    ebitda = (_ttm(facts_by_tag, "opIncome", dates)
              + _ttm(facts_by_tag, "dep", dates))
    # Zero-fill only where at least one debt tag reported. debtLT is stale
    # for 28 of 106 tickers and debtST for 27 of 99 -- filers who moved to
    # LongTermDebtAndCapitalLeaseObligations, or who report only LongTermDebt.
    # Treating "no debt data" as "no debt" shrinks EV and makes EV/EBITDA read
    # cheap, the same error class and the same direction as the
    # dividend-adjusted market cap caught in Task 9. Absent on both tags means
    # the multiple is unknown, not that the company is unlevered.
    lt = _level(facts_by_tag, "debtLT", dates)
    st = _level(facts_by_tag, "debtST", dates)
    debt = lt.fillna(0.0) + st.fillna(0.0)
    debt = debt.where(lt.notna() | st.notna())
    cash = _level(facts_by_tag, "cash", dates).fillna(0.0)
    out["evEbitda"] = ((cap + debt - cash) / ebitda).where(ebitda > 0)

    fcf = (_ttm(facts_by_tag, "cfo", dates)
           - _ttm(facts_by_tag, "capex", dates))
    out["fcfYield"] = (fcf / cap * 100).where(cap > 0)

    return out


def own_percentile(series: pd.Series) -> float | None:
    """Where the newest value sits in its own history, 0 = cheapest ever."""
    clean = pd.Series(series).dropna()
    if len(clean) < MIN_HISTORY:
        return None
    return float((clean < clean.iloc[-1]).mean())


_CHAINS = xbrl.CONCEPTS
_DEI_CHAINS = xbrl.DEI_CONCEPTS

# SEC's Pay-versus-Performance rule (Item 402(v)) requires a proxy statement
# to disclose "Net Income" in its executive-compensation table, and filers'
# XBRL tags that disclosure with the plain us-gaap:NetIncomeLoss concept --
# the exact same tag the 10-Q/10-K uses for the real figure, for the exact
# same period_end. The PvP number is a different thing entirely (routinely
# far off and often negative even when the company was profitable), and
# because a proxy statement is filed after the annual report it describes,
# _known_at's newest-filed-wins rule lets it silently replace the real
# quarter until the next 10-Q arrives. Measured on the live store: 304 such
# facts across 56 of 149 tickers, all under NetIncomeLoss (no other concept
# carries a PvP table). FDX's 2026-05-31 Q4 is the worked example: 10-K
# reports 1,597,000,000; the DEF 14A filed a month later retags the same
# period at -2,835,996,000, and picking it up drops FDX's TTM net income to
# 4,433 dollars, close enough to zero to make trailingPE meaningless in the
# report generated while writing this comment. This is a fact-selection
# problem valuation.py owns -- xbrl.py and the reported table hold both
# facts faithfully, as designed; a proxy filing did file a real number under
# that tag, so it is not a corrupt row for xbrl.py's write boundary to
# reject, only the wrong number for a point-in-time financial series to use.
_PROXY_FORMS = frozenset({"DEF 14A", "PRE 14A", "PREC14A", "PRER14A",
                          "DEFC14A"})


def _facts_for(ticker_facts: pd.DataFrame, chain) -> list:
    """This ticker's series for one chain, spliced.

    The store holds every chain member now, so the election happens here.
    Do NOT re-derive it with value_counts().idxmax() -- that is exactly the
    rule Task 10.5 removed from xbrl.pick_tag, and it elects a tag that
    stopped being filed in 2018 for 57 of 143 tickers on revenue.
    """
    present = ticker_facts[ticker_facts.concept.isin(chain)
                           & ~ticker_facts.form.isin(_PROXY_FORMS)]
    if present.empty:
        return []
    facts = [xbrl.Fact(r.ticker, r.concept, r.period_start, r.period_end,
                       None if pd.isna(r.fy) else int(r.fy), r.fp, r.form,
                       r.filed, float(r.value))
             for r in present.itertuples()]
    return xbrl.splice(facts, chain)


def build_all(conn, tickers) -> dict:
    """Every ticker's series, from the store. ~1s for 153 x 1,270 x 4.

    A single ticker whose facts violate _known_at's period-shape invariant
    must not abort the other 148 -- the whole point of Task 10.5's guard is to
    surface that defect class loudly, not to take the dashboard down with it.
    Caught per ticker and printed with the concept, never swallowed silently.
    """
    prices = pd.read_sql_query(
        "SELECT ticker, as_of, value FROM metrics "
        "WHERE period_type='daily' AND metric='closeRaw'", conn)
    facts = history.reported(conn)
    out = {}
    for ticker in tickers:
        px = prices[prices.ticker == ticker]
        if px.empty:
            continue
        series = pd.Series(px.value.values,
                           index=pd.DatetimeIndex(px.as_of)).sort_index()
        tf = facts[facts.ticker == ticker]
        by_tag = {name: _facts_for(tf, chain)
                  for name, chain in _CHAINS.items()}
        by_tag.update({name: _one_per_filing(_facts_for(tf, chain))
                      for name, chain in _DEI_CHAINS.items()})
        try:
            out[ticker] = multiple_series(ticker, by_tag, series)
        except ValueError as exc:
            # _known_at raises inside whichever concept's ttm_series call hits
            # a genuine period-shape conflict, but the exception itself names
            # only the period_end -- not the concept -- because _known_at has
            # no concept to report. Re-run each concept's TTM in isolation,
            # only on this rare failure path, so the printed message names
            # exactly which chain tripped it rather than leaving the operator
            # to guess across ten concepts for 149 tickers.
            culprit = "unknown"
            dates = pd.DatetimeIndex(series.index)
            for name in by_tag:
                if name in ("debtLT", "debtST", "cash", "sharesOutstanding"):
                    continue  # these use latest_series, not ttm -- cannot raise
                try:
                    _ttm(by_tag, name, dates)
                except ValueError:
                    culprit = name
                    break
            print(f"build_all: skipping {ticker} -- {culprit}: {exc}")
    return out


# Trading sessions, matching the convention already used for ret1m/ret6m.
WINDOWS = {"c1w": 5, "c1m": 22}


def change(series: pd.Series, sessions: int, points: bool = False) -> float | None:
    """Change over `sessions`, or None when the history is too short.

    Two kinds, because the metrics are two kinds. A multiple, a price or a
    dollar total moves multiplicatively, and a log ratio is the quantity that
    decomposes -- dln(multiple) = dln(price) - dln(fundamental) -- which is
    what separates a market move from a source revision. A margin, a growth
    rate or a return is already a percentage, and the question a reader asks
    of it is "how many points did it move", not "by what factor". A log ratio
    is also undefined for the ones that go negative, and revGrowth, epsGrowth,
    roe and the margins all do -- so a log-only implementation would go silent
    on exactly the names most worth noticing.
    """
    clean = pd.Series(series).dropna()
    if len(clean) < sessions + 1:
        return None
    now, then = float(clean.iloc[-1]), float(clean.iloc[-1 - sessions])
    if points:
        return now - then
    if now <= 0 or then <= 0:
        return None
    return math.log(now / then)


def changes(built: dict, proven: set, points_metrics: frozenset) -> dict:
    """(ticker, metric, window) -> change, for proven pairs only.

    The proof filter is not optional. Without it this walks every column of
    every frame and emits a change for anything with enough history --
    measured on the live store, 652 values including 104 evEbitda and 26
    fcfYield, whose LEVELS this branch refuses to render because they
    reconstruct to a 10.6% and 43% median error. A change reads like news, so
    it is more persuasive than a level and worse to get wrong.
    """
    out: dict = {}
    for ticker, frame in built.items():
        for metric in frame.columns:
            if (ticker, metric) not in proven:
                continue
            for name, sessions in WINDOWS.items():
                value = change(frame[metric], sessions,
                               points=metric in points_metrics)
                if value is not None:
                    out[(ticker, metric, name)] = value
    return out


def snapshot_changes(conn, points_metrics: frozenset,
                     skip: frozenset = frozenset()) -> dict:
    # `skip` holds (ticker, metric) pairs already answered by a derived daily
    # series -- see the note at its call site in build_dashboard.own_history.
    """The same, for metrics that only exist in the snapshot table.

    These have no derived daily series, so their depth is whatever `snapshot`
    has accrued -- 16 dates as of 2026-09-06, which covers c1w and cannot
    cover c1m. A window with too little depth yields no entry at all rather
    than a zero: "we have not watched long enough" and "it did not move" are
    different statements and must not render alike.
    """
    frame = pd.read_sql_query(
        "SELECT ticker, as_of, metric, value FROM metrics "
        "WHERE period_type = 'snapshot'", conn)
    out: dict = {}
    for (ticker, metric), group in frame.groupby(["ticker", "metric"]):
        # (ticker, metric), because the derived series that supersedes this one
        # is proven per pair. A bare metric name would let one ticker's proof
        # suppress every other ticker's snapshot change for that metric.
        if (ticker, metric) in skip:
            continue
        series = group.sort_values("as_of").value
        for name, sessions in WINDOWS.items():
            value = change(series, sessions, points=metric in points_metrics)
            if value is not None:
                out[(ticker, metric, name)] = value
    return out


# How close a data event must fall to a filing to be explained by it. A 10-Q
# reaches a data vendor within a few days, not the same afternoon.
FILING_WINDOW_DAYS = 5

# How recently an event must have happened to still be worth marking on a cell.
# Long enough to survive a weekend and a stale build, short enough that a mark
# means "look at this now" rather than "something happened once".
EVENT_RECENT_DAYS = 7


def data_events(values: pd.Series, filings, basis: pd.Series | None = None,
                deadband: float = 0.005) -> list:
    """Days the implied fundamental moved, and whether a filing explains it.

    A fundamental does not change daily, so any daily change in it is a data
    event rather than a market event. Filing dates split those in two, and the
    difference matters: one is information, the other is the source changing
    its mind about the past.

    `basis` is the price-like series to divide out for a ratio metric, so an
    ordinary market move does not register as a data event -- dln M = dln P -
    dln F, and without removing dln P every trading day looks like news. Pass
    None for a metric that carries no price at all (a margin, a growth rate, a
    balance), where the value IS the fundamental.

    Deliberately not applied to EV/EBITDA. Its implied fundamental needs an
    enterprise value, and dividing by price or market cap instead leaves the
    debt term in the residual: measured on the live snapshot table that yields
    1,588 "revisions" against 43 for P/S, which is the debt moving, not the
    source. A mark that fires on ordinary balance-sheet drift would say the
    opposite of what this mark is for.
    """
    series = pd.Series(values).dropna()
    if basis is not None:
        pair = pd.concat([pd.Series(basis).rename("b"), series.rename("m")],
                         axis=1, join="inner").dropna()
        pair = pair[(pair.b > 0) & (pair.m > 0)]
        if len(pair) < 2:
            return []
        series = pair.b / pair.m
    if len(series) < 2:
        return []
    filed = pd.DatetimeIndex(sorted(filings)) if len(filings) else None

    out = []
    previous = None
    for when, value in series.items():
        if previous is not None:
            scale = abs(previous) if abs(previous) > 1e-9 else 1.0
            if abs(value - previous) / scale >= deadband:
                explained = filed is not None and len(filed) and any(
                    0 <= gap <= FILING_WINDOW_DAYS for gap in (when - filed).days)
                out.append((when.strftime("%Y-%m-%d"),
                            "report" if explained else "revision"))
        previous = value
    return out


def revision_marks(conn, ratio_metrics: frozenset, direct_metrics: frozenset,
                   asof: str | None = None) -> dict:
    """(ticker, metric) -> "report" | "revision", for the most recent event.

    Only the newest event within EVENT_RECENT_DAYS marks a cell. A metric that
    was revised two years ago is history, not a warning, and marking every cell
    that ever moved would make the mark mean nothing.
    """
    frame = pd.read_sql_query(
        "SELECT ticker, as_of, metric, value FROM metrics "
        "WHERE period_type = 'snapshot'", conn)
    if frame.empty:
        return {}
    wide = frame.pivot_table(index=["ticker", "as_of"], columns="metric",
                             values="value")
    latest = pd.Timestamp(asof) if asof else pd.Timestamp(
        frame.as_of.max())
    filings = {}
    for ticker, group in reported(conn).groupby("ticker"):
        filings[ticker] = pd.DatetimeIndex(sorted(set(group.filed)))

    out: dict = {}
    for ticker, group in wide.groupby(level=0):
        group = group.copy()
        group.index = pd.DatetimeIndex([d for _t, d in group.index])
        cap = group["marketCap"] if "marketCap" in group else None
        for metric in list(ratio_metrics) + list(direct_metrics):
            if metric not in group:
                continue
            basis = cap if metric in ratio_metrics else None
            if metric in ratio_metrics and basis is None:
                continue
            events = data_events(group[metric], filings.get(ticker, []),
                                 basis=basis)
            recent = [e for e in events
                      if (latest - pd.Timestamp(e[0])).days <= EVENT_RECENT_DAYS]
            if recent:
                out[(ticker, metric)] = recent[-1][1]
    return out
