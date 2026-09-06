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


def split_factors(share_facts) -> pd.Series:
    """Multiplier taking each period's as-filed share count to today's basis.

    Recovered from the share count itself: a 10:1 split appears as a 10x jump
    between consecutive filings, which no issuance or buyback can imitate.
    Snapped to the nearest sensible ratio by history._snap_split, which exists
    for exactly this in the 13F path -- share counts there are as-filed while
    stored closes are back-adjusted, and a 25:1 split rendered as a manager
    adding 2,400%.
    """
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
    """Split-adjusted diluted shares, forward-filled onto `dates`."""
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
