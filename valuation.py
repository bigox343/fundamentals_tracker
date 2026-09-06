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

from datetime import date

import pandas as pd

import history
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
    """Trailing-twelve-month total as it was knowable on `on`."""
    known = _known_at(facts, on)
    if len(known) < TTM_QUARTERS:
        return None
    newest = sorted(known.values(), key=lambda f: f.period_end,
                    reverse=True)[:TTM_QUARTERS]
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


def _positive_shares(share_facts):
    """Drop a share fact that cannot be real: a share count is never <= 0.

    reported's Q4 facts are synthesized as FY - (Q1+Q2+Q3) uniformly for every
    duration concept (xbrl.quarterly()), which is correct for an additive
    total -- net income, revenue -- but WeightedAverageNumberOfDiluted
    SharesOutstanding is a period AVERAGE, not a sum: the annual average
    is close to any one quarter's average, not four times it, so FY - 3
    quarters comes out close to -2x the true count. Measured on the live
    store: MSFT's synthesized FY2024/2025/2026 Q4 share counts are all
    -14.9e9 against a true ~7.45e9; 13 of 149 tickers carry at least one
    negative synthesized share fact. This is a defect in xbrl.quarterly()'s
    Q4 reconstruction, not something valuation.py can repair -- quarterly()
    has no way to know which concepts are additive -- so the fix here is not
    to un-corrupt the value but to refuse it: drop it and let the last
    genuinely-filed quarter's count carry forward instead of a value that
    would turn a market cap negative.
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
    """
    share_facts = _positive_shares(share_facts)
    ordered = sorted({f.period_end: f for f in share_facts}.values(),
                     key=lambda f: f.period_end)
    ends = [f.period_end for f in ordered]
    factors = pd.Series(1.0, index=ends)
    if len(ordered) < 2:
        return factors
    cumulative = 1.0
    for i in range(len(ordered) - 1, 0, -1):
        prev, cur = ordered[i - 1].value, ordered[i].value
        ratio = (cur / prev) if prev else 1.0
        if ratio >= SPLIT_MIN_RATIO:
            cumulative *= history._snap_split(ratio)
        factors.iloc[i - 1] = cumulative
    return factors


def shares_series(share_facts, dates) -> pd.Series:
    """Split-adjusted diluted shares, forward-filled onto `dates`.

    Filters through _positive_shares independently of split_factors -- both
    are public functions callers may use directly (split_factors already has
    its own tests calling it standalone), so each must be robust to a
    negative synthesized fact on its own rather than relying on the other to
    have cleaned the input first.
    """
    share_facts = _positive_shares(share_facts)
    factors = split_factors(share_facts)
    adjusted = [
        (f.filed, f.value * float(factors.get(f.period_end, 1.0)))
        for f in sorted(share_facts, key=lambda f: f.filed)
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

# Below this many observations an "own range" is not a range.
#
# The brief's own draft set this to 250 ("roughly one fiscal year of trading
# days"), but that value is inconsistent with the brief's own Step 1 tests:
# test_own_percentile_places_today_in_its_own_range asserts a real (non-None)
# percentile for a 5-observation series, and even its first assertion (a
# 100-observation series) would return None at MIN_HISTORY=250 -- neither
# test was ever run against that value. Set to 3, the smallest floor that
# still passes test_own_percentile_needs_a_real_history (2 observations ->
# None) while letting the 5- and 100-observation cases through: below 3
# points there is no interior position to report (2 points are simply
# "above" or "below" the other). Production callers that want a full-year
# floor before showing a percentile badge can still apply MIN_HISTORY -- or
# their own stricter one -- at the call site; this floor guards the
# statistic itself, not any calendar convention.
MIN_HISTORY = 3


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
    shares = shares_series(facts_by_tag.get("shares", []), dates)
    cap = closes_raw.astype(float) * shares

    out = pd.DataFrame(index=dates)

    earnings = _ttm(facts_by_tag, "netIncome", dates)
    out["trailingPE"] = (cap / earnings).where(earnings > 0)

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
                if name in ("debtLT", "debtST", "cash"):
                    continue  # these use latest_series, not ttm -- cannot raise
                try:
                    _ttm(by_tag, name, dates)
                except ValueError:
                    culprit = name
                    break
            print(f"build_all: skipping {ticker} -- {culprit}: {exc}")
    return out
