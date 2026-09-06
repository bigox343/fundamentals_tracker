import sqlite3

import pandas as pd
import pytest

import history
import valuation
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


def test_a_negative_synthesized_q4_share_count_is_dropped_not_used():
    # WeightedAverageNumberOfDilutedSharesOutstanding is an AVERAGE, not a
    # sum, so xbrl.quarterly()'s uniform FY-(Q1+Q2+Q3) reconstruction -- built
    # for additive concepts like net income -- synthesizes a Q4 close to -2x
    # the true count whenever a filer's annual average is close to any one
    # quarter's average. Measured live: MSFT's stored Q4 share fact is
    # -14.9e9 against a true ~7.45e9. valuation.py cannot un-corrupt the
    # number, but it must not turn a market cap negative either -- the last
    # genuine quarter's count should carry forward instead.
    good_q3 = _q("2026-03-31", "2026-04-30", 7.46e9, "2026-01-01", 2026, "Q3")
    bad_q4 = _q("2026-06-30", "2026-07-29", -14.9e9, "2026-03-31", 2026, "Q4")
    dates = pd.to_datetime(["2026-08-01"])
    s = valuation.shares_series([good_q3, bad_q4], dates)
    assert s.iloc[0] == pytest.approx(7.46e9), \
        "the negative synthesized fact must not overwrite the real quarter"


def test_split_factors_ignores_a_negative_synthesized_share_count_too():
    # split_factors is called directly by callers other than shares_series
    # (it has its own tests above), so it must filter a negative synthesized
    # Q4 on its own rather than relying on a caller to have done it first.
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
    s = pd.Series(range(100), dtype=float)
    assert valuation.own_percentile(s) == pytest.approx(0.99, abs=0.02)
    assert valuation.own_percentile(pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])) \
        == pytest.approx(0.0, abs=0.01)


def test_own_percentile_needs_a_real_history():
    assert valuation.own_percentile(pd.Series([1.0, 2.0])) is None
    assert valuation.own_percentile(pd.Series(dtype=float)) is None


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
