import pandas as pd
import pytest

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
    assert known[("", "2026-06-30")].value == 500.0


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
