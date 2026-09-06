import sqlite3

import pandas as pd
import pytest

import history
import valuation
import xbrl
from xbrl import Fact


def _q(end, filed, value, start=None, fy=2026, fp="Q1"):
    return Fact("T", "NetIncomeLoss", start or "", end, fy, fp,
                "10-Q", filed, value)


def _four_quarters():
    return [
        _q("2025-09-30", "2025-10-30", 100.0, "2025-07-01", 2025, "Q3"),
        _q("2025-12-31", "2026-02-15", 110.0, "2025-10-01", 2025, "Q4"),
        _q("2026-03-31", "2026-04-30", 120.0, "2026-01-01", 2026, "Q1"),
        _q("2026-06-30", "2026-07-31", 130.0, "2026-04-01", 2026, "Q2"),
    ]


def test_ttm_sums_the_four_most_recent_filed_quarters():
    assert valuation.ttm_at(_four_quarters(), "2026-08-01") == pytest.approx(460.0)


def test_ttm_uses_only_what_had_been_filed_on_the_date():
    # On 2026-07-15 the June quarter had not been filed yet.
    facts = _four_quarters() + [
        _q("2025-06-30", "2025-07-30", 90.0, "2025-04-01", 2025, "Q2")]
    assert valuation.ttm_at(facts, "2026-07-15") == pytest.approx(420.0)


def test_a_date_before_any_filing_has_no_ttm():
    assert valuation.ttm_at(_four_quarters(), "2020-01-01") is None


def test_fewer_than_four_filed_quarters_has_no_ttm():
    assert valuation.ttm_at(_four_quarters()[:3], "2026-08-01") is None


def test_an_amendment_supersedes_the_original_only_after_it_is_filed():
    facts = _four_quarters() + [
        Fact("T", "NetIncomeLoss", "2026-04-01", "2026-06-30", 2026, "Q2",
             "10-Q/A", "2026-09-20", 900.0)]
    assert valuation.ttm_at(facts, "2026-09-01") == pytest.approx(460.0)
    assert valuation.ttm_at(facts, "2026-10-01") == pytest.approx(1230.0)


def test_four_contiguous_quarters_still_produce_a_ttm():
    # The guard added for the "scattered across three years" defect below
    # must not reject the ordinary case: _four_quarters() already spans about
    # a year (2025-07-01..2026-06-30, 364 days), which is exactly what the
    # guard is supposed to let through.
    assert valuation.ttm_at(_four_quarters(), "2026-08-01") == pytest.approx(460.0)


def test_four_quarters_scattered_across_three_years_yield_no_ttm():
    # cfo and capex are cumulative, sparsely-covered concepts: a median of
    # 28-32 stored facts per ticker against netIncome's 170. With roughly 1.4
    # quarters available per year, "the four newest by period_end" can be
    # four quarters from four different years rather than four consecutive
    # ones -- summed anyway, that is a wrong TTM, not an absent one.
    facts = [
        _q("2023-06-30", "2023-07-30", 10.0, "2023-04-01"),
        _q("2024-06-30", "2024-07-30", 20.0, "2024-04-01"),
        _q("2025-06-30", "2025-07-30", 30.0, "2025-04-01"),
        _q("2026-06-30", "2026-07-30", 40.0, "2026-04-01"),
    ]
    assert valuation.ttm_at(facts, "2026-08-01") is None


def test_a_52_53_week_filers_four_quarters_still_produce_a_ttm():
    # classify_span's annual band (350-380 days) exists precisely so a long
    # fiscal year does not get rejected by a guard meant to catch scattered
    # quarters. 2025-09-29..2026-10-05 is the same 371-day span
    # test_a_53_week_year_is_still_an_annual_span in test_xbrl.py uses.
    facts = [
        _q("2025-12-29", "2026-01-20", 10.0, "2025-09-29"),
        _q("2026-03-30", "2026-04-20", 20.0, "2025-12-30"),
        _q("2026-06-29", "2026-07-15", 30.0, "2026-03-31"),
        _q("2026-10-05", "2026-10-20", 40.0, "2026-06-30"),
    ]
    assert valuation.ttm_at(facts, "2026-11-01") == pytest.approx(100.0)


def test_ttm_series_is_a_step_function_that_moves_on_filing_dates():
    dates = pd.to_datetime(["2026-07-30", "2026-07-31", "2026-08-01"])
    s = valuation.ttm_series(_four_quarters(), dates)
    assert s.iloc[0] != s.iloc[1], "it must step on the filing date"
    assert s.iloc[1] == s.iloc[2]


def test_split_factors_recover_a_ten_for_one_from_the_share_count():
    shares = [
        _q("2024-03-31", "2024-04-30", 2.47e9, "2024-01-01", 2024, "Q1"),
        _q("2024-06-30", "2024-07-30", 24.6e9, "2024-04-01", 2024, "Q2"),
        _q("2024-09-30", "2024-10-30", 24.7e9, "2024-07-01", 2024, "Q3"),
    ]
    f = valuation.split_factors(shares)
    # Pre-split filings must be multiplied by 10 to reach today's basis.
    assert f.loc["2024-03-31"] == pytest.approx(10.0)
    assert f.loc["2024-06-30"] == pytest.approx(1.0)


def test_the_root_cause_no_longer_synthesizes_a_bad_q4_to_drop():
    # Task 11 found xbrl.quarterly()'s uniform FY-(Q1+Q2+Q3) reconstruction
    # synthesizing a Q4 close to -2x the true diluted share count (measured
    # live: MSFT's stored Q4 share fact was -14.9e9 against a true ~7.45e9)
    # and worked around it here with a "drop a non-positive share fact"
    # filter. Task 11.5 moved the real fix into xbrl.quarterly() itself
    # (NON_ADDITIVE_CONCEPTS) -- this test proves that end to end: run the
    # filer's real quarters plus its annual figure through quarterly() itself
    # (as backfill_xbrl.sweep_ticker does before anything reaches the store),
    # and confirm no bad Q4 is even produced for shares_series to need to
    # filter. The filter itself (_drop_impossible_shares) stays in
    # valuation.py regardless -- see the tests below and its own docstring --
    # because the live store still holds 4,526 rows the pre-fix code wrote
    # before this task, which the fix cannot reach backwards.
    tag = "WeightedAverageNumberOfDilutedSharesOutstanding"
    filed = "2026-07-29"
    raw = [
        Fact("T", tag, "2025-07-01", "2025-09-30", 2025, "Q3", "10-Q",
             "2025-10-30", 7.40e9),
        Fact("T", tag, "2025-10-01", "2025-12-31", 2025, "Q4", "10-Q",
             "2026-02-15", 7.42e9),
        Fact("T", tag, "2026-01-01", "2026-03-31", 2026, "Q1", "10-Q",
             "2026-04-30", 7.46e9),
        Fact("T", tag, "2025-07-01", "2026-06-30", 2026, "FY", "10-K",
             filed, 7.45e9),
    ]
    quarters = xbrl.quarterly(raw)
    assert [f for f in quarters if f.period_end == "2026-06-30"] == [], \
        "no Q4 should ever be synthesized for a non-additive concept"

    dates = pd.to_datetime(["2026-08-01"])
    s = valuation.shares_series(quarters, dates)
    assert s.iloc[0] == pytest.approx(7.46e9), \
        "forward-fills from the last real quarter; there is no Q4 to use"


def test_a_legacy_poisoned_share_count_already_in_the_store_is_dropped_not_used():
    # The fix above stops any FUTURE sweep from writing one of these. The
    # store that carried 4,526 of them across 133 of 149 tickers has since
    # been cleared and refetched, and data/reported.csv.gz rewritten from it,
    # so neither the live store nor the rebuild path holds one today. The
    # guard stays regardless: it is cheap, and a fact of exactly this shape --
    # a synthesized Q4 for a non-additive concept -- must be refused wherever
    # it comes from, including an older archive someone restores by hand.
    good_q3 = _q("2026-03-31", "2026-04-30", 7.46e9, "2026-01-01", 2026, "Q3")
    bad_q4 = _q("2026-06-30", "2026-07-29", -14.9e9, "2026-03-31", 2026, "Q4")
    dates = pd.to_datetime(["2026-08-01"])
    s = valuation.shares_series([good_q3, bad_q4], dates)
    assert s.iloc[0] == pytest.approx(7.46e9), \
        "a legacy negative fact must not overwrite the real quarter"


def test_split_factors_ignores_a_legacy_poisoned_share_count_too():
    # split_factors is called directly by callers other than shares_series
    # (it has its own tests above), so it must filter a poisoned fact on its
    # own rather than relying on the other to have cleaned the input first.
    good_q3 = _q("2026-03-31", "2026-04-30", 7.46e9, "2026-01-01", 2026, "Q3")
    bad_q4 = _q("2026-06-30", "2026-07-29", -14.9e9, "2026-03-31", 2026, "Q4")
    factors = valuation.split_factors([good_q3, bad_q4])
    assert list(factors.index) == ["2026-03-31"], \
        "the negative fact's period_end must not appear at all"


def test_split_factors_ignore_ordinary_buybacks():
    shares = [
        _q("2025-03-31", "2025-04-30", 15.0e9, "2025-01-01", 2025, "Q1"),
        _q("2025-06-30", "2025-07-30", 14.8e9, "2025-04-01", 2025, "Q2"),
        _q("2025-09-30", "2025-10-30", 14.7e9, "2025-07-01", 2025, "Q3"),
    ]
    assert set(valuation.split_factors(shares).round(6)) == {1.0}


def _dei(end, filed, value):
    return Fact("T", "EntityCommonStockSharesOutstanding", "", end, None,
               None, "10-Q", filed, value)


def test_prefer_live_uses_the_preferred_series_wherever_it_is_live():
    preferred = [_dei("2026-06-30", "2026-07-31", 1000.0)]
    fallback = [
        _q("2026-06-30", "2026-07-31", 950.0, "2026-04-01", 2026, "Q2"),
        _q("2025-06-30", "2025-07-31", 900.0, "2025-04-01", 2025, "Q2"),
    ]
    out = valuation._prefer_live(preferred, fallback)
    # The period dei already covers (2026-06-30) must come from dei alone --
    # reporting it from both would render as a revision that never happened.
    assert out == [preferred[0], fallback[1]]


def test_prefer_live_ignores_a_preferred_series_that_has_gone_stale():
    # CHTR's real shape: dei stops at 2016-08-09 while the weighted-average
    # series keeps being filed to the present. Unguarded, forward-filling the
    # decade-old dei count into today overstated market cap by 101%.
    # LIVE_WINDOW_DAYS is the same staleness tolerance xbrl.splice already
    # uses for exactly this "has a tag stopped being filed" question.
    preferred = [_dei("2016-08-09", "2016-08-09", 400.0e6)]
    fallback = [_q("2026-06-30", "2026-07-31", 150.0e6, "2026-04-01",
                   2026, "Q2")]
    out = valuation._prefer_live(preferred, fallback)
    assert out == fallback, "a decade-stale dei count must not be used at all"


def test_prefer_live_with_nothing_on_one_side_returns_the_other_untouched():
    facts = [_q("2026-06-30", "2026-07-31", 950.0, "2026-04-01", 2026, "Q2")]
    assert valuation._prefer_live([], facts) == facts
    assert valuation._prefer_live(facts, []) == facts


def test_sum_same_filing_adds_multiple_share_classes_on_one_filing():
    # dei:EntityCommonStockSharesOutstanding is tagged once per class of
    # stock for a multi-class filer -- CHTR carries two entries sharing one
    # filed date for 1 of its 22 filings. Market cap wants their sum.
    class_a = _dei("2026-06-30", "2026-07-31", 100.0)
    class_b = _dei("2026-06-30", "2026-07-31", 50.0)
    out = valuation._sum_same_filing([class_a, class_b])
    assert len(out) == 1
    assert out[0].value == pytest.approx(150.0)


def test_sum_same_filing_leaves_separate_filings_alone():
    older = _dei("2026-03-31", "2026-04-30", 90.0)
    newer = _dei("2026-06-30", "2026-07-31", 100.0)
    out = valuation._sum_same_filing([older, newer])
    assert {f.value for f in out} == {90.0, 100.0}


def test_shares_series_prefers_dei_over_the_weighted_average_when_live():
    dates = pd.to_datetime(["2026-08-01"])
    dei = [_dei("2026-06-30", "2026-07-31", 1000.0)]
    weighted = [_q("2026-06-30", "2026-07-31", 950.0, "2026-04-01",
                   2026, "Q2")]
    s = valuation.shares_series(weighted, dates, dei_facts=dei)
    assert s.iloc[0] == pytest.approx(1000.0)


def test_shares_series_defaults_to_the_weighted_average_with_no_dei_facts():
    # dei_facts=None must reproduce the pre-Task-11.5 behavior exactly, for
    # every caller that has not been updated to supply it.
    dates = pd.to_datetime(["2026-08-01"])
    weighted = [_q("2026-06-30", "2026-07-31", 950.0, "2026-04-01",
                   2026, "Q2")]
    s = valuation.shares_series(weighted, dates)
    assert s.iloc[0] == pytest.approx(950.0)


def test_a_reversed_duration_fact_is_dropped_from_ttm():
    # The Task 5 shape exactly: a synthesized Q4 with period_start AFTER
    # period_end. Its period_end (2026-07-15) is the most recent of all five
    # facts here, so an unguarded sort-by-period_end would let it bump the
    # oldest real quarter (2025-09-30) out of the trailing four and fold its
    # -$11.06B into the sum. The clean four-quarter total must survive intact.
    corrupted = Fact("T", "NetIncomeLoss", "2026-08-01", "2026-07-15",
                      2026, "Q3", "10-Q", "2026-07-20", -11.06e9)
    facts = _four_quarters() + [corrupted]
    assert valuation.ttm_at(facts, "2026-08-01") == pytest.approx(460.0)


def test_an_instant_fact_with_no_period_start_is_not_treated_as_reversed():
    # Instant concepts (cash, debt) legitimately carry period_start == "".
    # The reversed-period guard must not reject them for lacking a start.
    instant = Fact("T", "CashAndCashEquivalentsAtCarryingValue", "",
                    "2026-06-30", 2026, "Q2", "10-Q", "2026-07-31", 500.0)
    known = valuation._known_at([instant], "2026-08-01")
    assert known["2026-06-30"].value == 500.0


def test_two_spellings_of_one_quarters_start_are_the_same_period():
    # Filers rewrite a quarter's start between filings: ADBE reported the
    # quarter ending 2009-05-29 as starting 2009-02-27 in its 10-Q and
    # 2009-02-28 when it republished, same value both times. Measured across
    # the store, 255 groups do this, spread at most 9 days. Treating the two
    # spellings as two periods lets one quarter occupy two of the four TTM
    # slots and pushes a real quarter out.
    original = _q("2026-06-30", "2026-07-31", 130.0, "2026-04-01", 2026, "Q2")
    respelled = _q("2026-06-30", "2027-02-10", 130.0, "2026-03-31", 2026, "Q2")
    facts = _four_quarters() + [respelled]
    known = valuation._known_at(facts, "2027-03-01")
    assert len(known) == 4, "one quarter, not two"
    assert valuation.ttm_at(facts, "2027-03-01") == pytest.approx(460.0)
    assert original.period_start != respelled.period_start


def test_a_restatement_supersedes_the_original_it_respells():
    # 24 of those 255 groups changed the value too -- MSFT restated its
    # 2016-09-30 net income by 17% while also moving the start by a day. The
    # restated figure must win once filed, and must not be summed alongside
    # the original.
    restated = _q("2026-06-30", "2027-02-10", 900.0, "2026-03-31", 2026, "Q2")
    facts = _four_quarters() + [restated]
    assert valuation.ttm_at(facts, "2026-12-01") == pytest.approx(460.0), \
        "before the restatement is filed, the market had the original"
    assert valuation.ttm_at(facts, "2027-03-01") == pytest.approx(1230.0), \
        "after, the restated quarter replaces it rather than adding to it"


def test_a_start_further_apart_than_the_tolerance_is_still_rejected():
    # The real hazard sits at 93 days (a YTD fact against the quarter it
    # contains) and nothing legitimate was measured between 10 and 100, so
    # the tolerance must not have swallowed the check it exists for.
    far = _q("2026-06-30", "2026-07-31", 250.0, "2026-05-01", 2026, "Q2")
    with pytest.raises(ValueError, match="different period shapes"):
        valuation.ttm_at(_four_quarters() + [far], "2026-08-01")


def test_a_period_end_shared_by_two_different_period_starts_is_rejected():
    # A YTD fact and the quarterly fact it contains can carry the same
    # period_end with different period_start values. The (period_start,
    # period_end) dedupe key does not collapse this -- both survive as
    # distinct entries -- so ttm_at's assumption that each period_end is one
    # quarter's worth would silently double count. valuation requires callers
    # to hand it one fact stream per period shape (xbrl.quarterly() output);
    # this is the cheap check that catches a caller who did not.
    ytd = _q("2026-06-30", "2026-07-31", 250.0, "2026-01-01", 2026, "Q2")
    facts = _four_quarters() + [ytd]
    with pytest.raises(ValueError):
        valuation.ttm_at(facts, "2026-08-01")


def test_market_cap_uses_the_unadjusted_close_and_adjusted_shares():
    dates = pd.to_datetime(["2026-08-01"])
    facts = {
        "netIncome": _four_quarters(),
        "shares": [_q("2026-06-30", "2026-07-31", 1000.0, "2026-04-01",
                      2026, "Q2")],
    }
    closes = pd.Series([50.0], index=dates)
    out = valuation.multiple_series("T", facts, closes)
    # cap = 50 x 1000 = 50,000; TTM net income = 460 -> P/E = 108.7
    assert out.loc[dates[0], "trailingPE"] == pytest.approx(50000 / 460, rel=1e-6)


def test_pe_uses_the_weighted_average_basis_and_ps_uses_shares_outstanding():
    # Two kinds of multiple, two share bases. P/S divides what the whole
    # company costs by what it sells, so it takes the actual count
    # outstanding. P/E is price over earnings PER SHARE, and EPS is defined on
    # the weighted average diluted count -- using the outstanding count there
    # reprices a year of earnings onto today's share base. The two counts are
    # deliberately far apart here so a single-basis implementation cannot pass.
    dates = pd.to_datetime(["2026-08-01"])
    facts = {
        "netIncome": _four_quarters(),
        "revenue": _four_quarters(),
        "shares": [_q("2026-06-30", "2026-07-31", 1000.0, "2026-04-01",
                      2026, "Q2")],
        "sharesOutstanding": [Fact(
            "T", "EntityCommonStockSharesOutstanding", "", "2026-06-30",
            2026, "Q2", "10-Q", "2026-07-31", 2000.0)],
    }
    out = valuation.multiple_series("T", facts, pd.Series([50.0], index=dates))
    assert out.loc[dates[0], "ps"] == pytest.approx(50 * 2000 / 460, rel=1e-6), \
        "P/S takes the outstanding count"
    assert out.loc[dates[0], "trailingPE"] == pytest.approx(
        50 * 1000 / 460, rel=1e-6), "P/E takes the weighted average count"


def test_a_negative_ttm_earnings_yields_no_pe_rather_than_a_negative_one():
    dates = pd.to_datetime(["2026-08-01"])
    losses = [_q(e, f, -50.0, s, y, p) for e, f, s, y, p in [
        ("2025-09-30", "2025-10-30", "2025-07-01", 2025, "Q3"),
        ("2025-12-31", "2026-02-15", "2025-10-01", 2025, "Q4"),
        ("2026-03-31", "2026-04-30", "2026-01-01", 2026, "Q1"),
        ("2026-06-30", "2026-07-31", "2026-04-01", 2026, "Q2")]]
    facts = {"netIncome": losses,
             "shares": [_q("2026-06-30", "2026-07-31", 1000.0, "2026-04-01",
                           2026, "Q2")]}
    out = valuation.multiple_series("T", facts, pd.Series([50.0], index=dates))
    assert pd.isna(out.loc[dates[0], "trailingPE"]), \
        "the same domain rule as the peer frame: undefined, not negative"


def test_a_balance_sheet_level_is_not_summed_over_four_quarters():
    # Cash is a level on a date. Summing four quarters of it would count the
    # same money four times and understate EV/EBITDA badly.
    cash = [_q(e, f, 50.0, None, 2026, p) for e, f, p in [
        ("2025-09-30", "2025-10-30", "Q3"), ("2025-12-31", "2026-02-15", "Q4"),
        ("2026-03-31", "2026-04-30", "Q1"), ("2026-06-30", "2026-07-31", "Q2")]]
    s = valuation.latest_series(cash, pd.to_datetime(["2026-08-01"]))
    assert s.iloc[0] == pytest.approx(50.0), "the level, not 200.0"


def test_ev_ebitda_is_undefined_rather_than_unlevered_when_no_debt_tag_reported():
    # debtLT is stale for 28 of 106 tickers and debtST for 27 of 99. Treating
    # "no debt data" as "no debt" shrinks EV and makes the multiple read
    # cheap -- the same error class, and the same direction, as the
    # dividend-adjusted market cap caught in Task 9. Absent on both tags must
    # withhold the multiple, not quietly assume zero leverage.
    dates = pd.to_datetime(["2026-08-01"])
    facts = {
        "opIncome": _four_quarters(),
        "dep": _four_quarters(),
        "shares": [_q("2026-06-30", "2026-07-31", 1000.0, "2026-04-01",
                      2026, "Q2")],
        # debtLT and debtST both absent.
    }
    out = valuation.multiple_series("T", facts, pd.Series([50.0], index=dates))
    assert pd.isna(out.loc[dates[0], "evEbitda"]), \
        "no debt data must withhold the multiple, not price it as unlevered"


def test_ev_ebitda_zero_fills_only_the_debt_tag_that_is_actually_missing():
    # One tag reporting (say debtLT, because the filer moved to
    # LongTermDebtAndCapitalLeaseObligations and no longer files debtST) is
    # real information and must still zero-fill the other side, not withhold
    # the whole multiple.
    dates = pd.to_datetime(["2026-08-01"])
    facts = {
        "opIncome": _four_quarters(),
        "dep": _four_quarters(),
        "shares": [_q("2026-06-30", "2026-07-31", 1000.0, "2026-04-01",
                      2026, "Q2")],
        "debtLT": [_q("2026-06-30", "2026-07-31", 200.0, None, 2026, "Q2")],
        # debtST absent.
    }
    out = valuation.multiple_series("T", facts, pd.Series([50.0], index=dates))
    assert not pd.isna(out.loc[dates[0], "evEbitda"]), \
        "one reported debt tag is enough to compute EV, treating the other as 0"
    # cap = 50,000; ebitda = TTM opIncome + TTM dep = 460 + 460 = 920;
    # debt = 200 (debtLT) + 0 (debtST absent, zero-filled); cash absent -> 0.
    assert out.loc[dates[0], "evEbitda"] == pytest.approx((50000 + 200) / 920)


def test_own_percentile_places_today_in_its_own_range():
    # Series long enough to clear MIN_HISTORY, which is a full year of trading
    # days: a percentile is only meaningful against a real range.
    rising = pd.Series(range(300), dtype=float)
    assert valuation.own_percentile(rising) == pytest.approx(0.997, abs=0.01)
    falling = pd.Series(range(300, 0, -1), dtype=float)
    assert valuation.own_percentile(falling) == pytest.approx(0.0, abs=0.01)


def test_own_percentile_needs_a_real_history():
    # A handful of points is not a range. Painting a percentile from three
    # observations onto a cell labelled "vs own history" would be a confident
    # answer drawn from nothing.
    assert valuation.own_percentile(pd.Series([1.0, 2.0])) is None
    assert valuation.own_percentile(pd.Series(dtype=float)) is None
    assert valuation.own_percentile(pd.Series([5.0, 4.0, 3.0])) is None
    assert valuation.own_percentile(pd.Series(range(249), dtype=float)) is None
    assert valuation.own_percentile(pd.Series(range(250), dtype=float)) is not None


def test_a_proxy_statements_pay_versus_performance_net_income_is_not_used():
    # SEC's Pay-versus-Performance rule makes proxy statements tag a "Net
    # Income" figure under the same us-gaap:NetIncomeLoss concept and the
    # same period_end as the real 10-K -- but it is a different number
    # entirely (FDX's worked case: 10-K reports 1,597,000,000 for
    # 2026-05-31; the DEF 14A filed a month later retags the same period at
    # -2,835,996,000). Because the proxy statement is filed after the annual
    # report, _known_at's newest-filed-wins rule would otherwise let the
    # garbage figure silently replace the real quarter.
    real = pd.DataFrame([{
        "ticker": "T", "concept": "NetIncomeLoss", "period_start": "2026-03-01",
        "period_end": "2026-05-31", "fy": 2026, "fp": "Q4", "form": "10-K",
        "filed": "2026-07-20", "value": 1_597_000_000.0,
    }, {
        "ticker": "T", "concept": "NetIncomeLoss", "period_start": "2026-03-01",
        "period_end": "2026-05-31", "fy": None, "fp": "Q4", "form": "DEF 14A",
        "filed": "2026-08-17", "value": -2_835_996_000.0,
    }])
    out = valuation._facts_for(real, ("NetIncomeLoss",))
    assert len(out) == 1, "the proxy statement's row must not survive"
    assert out[0].value == pytest.approx(1_597_000_000.0)
    assert out[0].form == "10-K"


def test_build_all_skips_a_ticker_whose_facts_trip_the_period_shape_guard():
    # One ticker's reported facts violate _known_at's period-shape invariant
    # (a quarter and a year-to-date fact sharing one period_end, spans more
    # than START_TOLERANCE_DAYS apart) -- the exact class of defect Task 10.5
    # existed to catch. build_all iterates 149 tickers in production; one bad
    # one raising must not take the other 148 down with it.
    conn = sqlite3.connect(":memory:")
    history.ensure_schema(conn)

    dates = pd.to_datetime(["2026-08-01"])
    closes = pd.DataFrame({"GOOD": [50.0], "BAD": [50.0]}, index=dates)
    history.ingest_prices(conn, closes, metric="closeRaw")

    good_facts = [
        Fact("GOOD", "NetIncomeLoss", s, e, 2025, p, "10-Q", f, v)
        for e, f, s, p, v in [
            ("2025-09-30", "2025-10-30", "2025-07-01", "Q3", 100.0),
            ("2025-12-31", "2026-02-15", "2025-10-01", "Q4", 110.0),
            ("2026-03-31", "2026-04-30", "2026-01-01", "Q1", 120.0),
            ("2026-06-30", "2026-07-31", "2026-04-01", "Q2", 130.0)]
    ] + [Fact("GOOD", "WeightedAverageNumberOfDilutedSharesOutstanding",
              "2026-04-01", "2026-06-30", 2026, "Q2", "10-Q",
              "2026-07-31", 1000.0)]
    bad_facts = [
        Fact("BAD", "NetIncomeLoss", "2026-04-01", "2026-06-30", 2026, "Q2",
             "10-Q", "2026-07-31", 130.0),
        Fact("BAD", "NetIncomeLoss", "2026-01-01", "2026-06-30", 2026, "Q2",
             "10-Q", "2026-07-31", 250.0),
    ]
    history.upsert_reported(conn, good_facts + bad_facts)

    out = valuation.build_all(conn, ["GOOD", "BAD"])
    assert "BAD" not in out, "the offending ticker must be skipped, not raised"
    assert "GOOD" in out, "a bad ticker must not take a good one down with it"
