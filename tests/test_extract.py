import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import extract

FIXTURES = Path(__file__).parent / "fixtures"

AS_OF = date(2026, 8, 15)

# NVDA's fiscal calendar, verified 2026-08-15 against yfinance 1.4.1.
NVDA_ANNUAL = [date(2022, 1, 31), date(2023, 1, 31), date(2024, 1, 31),
               date(2025, 1, 31), date(2026, 1, 31)]
NVDA_QUARTER = [date(2025, 4, 30), date(2025, 7, 31), date(2025, 10, 31),
                date(2026, 1, 31), date(2026, 4, 30)]
NVDA_FYE = date(2026, 1, 25)      # info.lastFiscalYearEnd, the true 52/53-week date
NVDA_LAST_REPORT = date(2026, 5, 20)


def _frame(name):
    data = json.loads((FIXTURES / name).read_text())
    return pd.DataFrame.from_dict(data, orient="index")


@pytest.fixture
def eps_trend():
    return _frame("nvda_eps_trend.json")


@pytest.fixture
def nvda_refs():
    return extract.resolve_ref_periods(
        AS_OF, NVDA_ANNUAL, NVDA_QUARTER,
        last_fiscal_year_end=NVDA_FYE, last_report=NVDA_LAST_REPORT,
    )


# --------------------------------------------------------------------------- #
# month arithmetic                                                             #
# --------------------------------------------------------------------------- #

def test_add_months_preserves_month_end():
    """Fiscal quarters sit on month ends; 30 April plus 3 months is 31 July."""
    assert extract.add_months(date(2026, 4, 30), 3) == date(2026, 7, 31)
    assert extract.add_months(date(2026, 1, 31), 3) == date(2026, 4, 30)


def test_add_months_handles_mid_month_dates():
    assert extract.add_months(date(2026, 1, 15), 3) == date(2026, 4, 15)


def test_add_months_handles_leap_year_month_end():
    assert extract.add_months(date(2027, 2, 28), 12) == date(2028, 2, 29)


def test_add_months_goes_backwards():
    assert extract.add_months(date(2026, 1, 31), -3) == date(2025, 10, 31)


@pytest.mark.parametrize("raw,snapped", [
    (date(2026, 7, 31), date(2026, 7, 31)),   # already a month end
    (date(2026, 7, 30), date(2026, 7, 31)),   # DateOffset drift off a 30-day month
    (date(2026, 1, 25), date(2026, 1, 31)),   # NVDA's true 52/53-week FY end
    (date(2025, 11, 2), date(2025, 10, 31)),  # AVGO's: snaps back, not forward
    (date(2025, 8, 28), date(2025, 8, 31)),   # MU's
    (date(2026, 7, 3), date(2026, 6, 30)),    # WDC's
])
def test_snap_month_end(raw, snapped):
    assert extract.snap_month_end(raw) == snapped


# --------------------------------------------------------------------------- #
# fiscal period resolution                                                     #
# --------------------------------------------------------------------------- #

# Every expectation below was established independently of the resolver, by
# matching earnings_estimate.yearAgoEps against reported EPS history and the
# next scheduled earnings date (probe A2/A4, 2026-08-15). Fixtures for all of
# these live in tests/fixtures/20260815/.
#
#   ticker, annual ends, quarter ends, info FY end, last report, 0y, 0q
GROUND_TRUTH = [
    ("NVDA", [date(2026, 1, 31)], [date(2026, 4, 30)], date(2026, 1, 25),
     date(2026, 5, 20), "FY2027-01-31", "FQ2026-07-31"),
    ("AVGO", [date(2025, 10, 31)], [date(2026, 4, 30)], date(2025, 11, 2),
     date(2026, 6, 3), "FY2026-10-31", "FQ2026-07-31"),
    ("COST", [date(2025, 8, 31)], [date(2026, 5, 31)], date(2025, 8, 31),
     date(2026, 5, 28), "FY2026-08-31", "FQ2026-08-31"),
    ("WMT", [date(2026, 1, 31)], [date(2026, 4, 30)], date(2026, 1, 31),
     date(2026, 5, 21), "FY2027-01-31", "FQ2026-07-31"),
    ("INFY", [date(2026, 3, 31)], [date(2026, 6, 30)], date(2026, 3, 31),
     date(2026, 7, 23), "FY2027-03-31", "FQ2026-09-30"),
    ("CSX", [date(2025, 12, 31)], [date(2026, 6, 30)], date(2025, 12, 31),
     date(2026, 7, 22), "FY2026-12-31", "FQ2026-09-30"),
    # both statement frames stale: FY reported 08-12, income_stmt still at 2025
    ("CSCO", [date(2025, 7, 31)], [date(2026, 4, 30)], date(2026, 7, 25),
     date(2026, 8, 12), "FY2027-07-31", "FQ2026-10-31"),
    ("SMCI", [date(2025, 6, 30)], [date(2026, 3, 31)], date(2026, 6, 30),
     date(2026, 8, 11), "FY2027-06-30", "FQ2026-09-30"),
]


@pytest.mark.parametrize(
    "ticker,annual,quarter,fye,report,exp_0y,exp_0q",
    GROUND_TRUTH,
    ids=[c[0] for c in GROUND_TRUTH],
)
def test_resolve_ref_periods_matches_verified_ground_truth(
    ticker, annual, quarter, fye, report, exp_0y, exp_0q
):
    got = extract.resolve_ref_periods(
        AS_OF, annual, quarter, last_fiscal_year_end=fye, last_report=report
    )
    assert got["0y"] == exp_0y
    assert got["0q"] == exp_0q


def test_zero_q_counts_from_the_last_reported_quarter_not_from_as_of():
    """NVDA's 0q on 2026-08-15 is the quarter ending 07-31, reporting on the 26th.

    Resolving to the next quarter end after as_of gives 2026-10-31 -- a full
    quarter late, and wrong for every company between quarter close and
    earnings. Guards the exact regression.
    """
    got = extract.resolve_ref_periods(
        AS_OF, NVDA_ANNUAL, NVDA_QUARTER,
        last_fiscal_year_end=NVDA_FYE, last_report=NVDA_LAST_REPORT,
    )
    assert got["0q"] == "FQ2026-07-31"
    assert got["0q"] != "FQ2026-10-31"
    assert got["+1q"] == "FQ2026-10-31"


def test_info_fiscal_year_beats_a_stale_statement_frame():
    """CSCO reported FY2026 on 08-12; income_stmt still ends at 2025-07-31.

    Trusting the frame puts 0y a full year early, and every estimate captured
    against it joins to the wrong actual.
    """
    stale_only = extract.resolve_ref_periods(
        AS_OF, [date(2025, 7, 31)], [date(2026, 4, 30)]
    )
    with_info = extract.resolve_ref_periods(
        AS_OF, [date(2025, 7, 31)], [date(2026, 4, 30)],
        last_fiscal_year_end=date(2026, 7, 25), last_report=date(2026, 8, 12),
    )
    assert stale_only["0y"] == "FY2026-07-31"   # what the frame alone claims
    assert with_info["0y"] == "FY2027-07-31"    # what is actually true


def test_stale_quarterly_frame_is_advanced_past_the_missing_filing():
    """SMCI reported its June quarter on 08-11; the frame still ends in March."""
    got = extract.resolve_ref_periods(
        AS_OF, [date(2025, 6, 30)], [date(2026, 3, 31)],
        last_fiscal_year_end=date(2026, 6, 30), last_report=date(2026, 8, 11),
    )
    assert got["0q"] == "FQ2026-09-30"


def test_on_time_reporting_is_not_mistaken_for_staleness():
    """COST reports ~18 days after quarter end; that must not advance anything."""
    got = extract.resolve_ref_periods(
        AS_OF, [date(2025, 8, 31)], [date(2026, 5, 31)],
        last_fiscal_year_end=date(2025, 8, 31), last_report=date(2026, 5, 28),
    )
    assert got["0q"] == "FQ2026-08-31"


def test_fiscal_year_end_passed_but_unreported_keeps_zero_y_on_it():
    """A FY that has ended but not reported is still the one consensus forecasts.

    If `info` rolls lastFiscalYearEnd at the year end rather than at the report,
    counting forward from it would skip the year that estimates actually refer
    to.
    """
    got = extract.resolve_ref_periods(
        date(2026, 6, 10), [date(2025, 6, 30)], [date(2026, 3, 31)],
        last_fiscal_year_end=date(2026, 6, 30), last_report=date(2026, 5, 10),
    )
    assert got["0y"] == "FY2026-06-30"


def test_resolve_ref_periods_across_year_rollover():
    """After the FY is reported, 0y advances a year rather than showing a revision."""
    before = extract.resolve_ref_periods(
        date(2027, 1, 20), NVDA_ANNUAL, NVDA_QUARTER,
        last_fiscal_year_end=date(2026, 1, 25), last_report=date(2026, 11, 18),
    )
    after = extract.resolve_ref_periods(
        date(2027, 3, 1), NVDA_ANNUAL, NVDA_QUARTER,
        last_fiscal_year_end=date(2027, 1, 24), last_report=date(2027, 2, 25),
    )
    assert before["0y"] == "FY2027-01-31"
    assert after["0y"] == "FY2028-01-31"


def test_plus_one_year_is_twelve_months_after_zero_year(nvda_refs):
    assert nvda_refs["0y"] == "FY2027-01-31"
    assert nvda_refs["+1y"] == "FY2028-01-31"


@pytest.mark.parametrize("ticker,annual,quarter,fye,report", [
    # +1q and 0y both end 2026-10-31
    ("AVGO", [date(2025, 10, 31)], [date(2026, 4, 30)], date(2025, 11, 2),
     date(2026, 6, 3)),
    # 0q and 0y both end 2026-08-31
    ("COST", [date(2025, 8, 31)], [date(2026, 5, 31)], date(2025, 8, 31),
     date(2026, 5, 28)),
    # +1q and 0y both end 2026-12-31
    ("CSX", [date(2025, 12, 31)], [date(2026, 6, 30)], date(2025, 12, 31),
     date(2026, 7, 22)),
])
def test_annual_and_quarterly_horizons_never_share_a_ref_period(
    ticker, annual, quarter, fye, report
):
    """Four horizons must yield four distinct keys even when two share a date.

    Estimates all carry period_type='estimate', so period_type cannot separate
    a fiscal year from a quarter ending the same day -- only ref_period can.
    Captured live before the FY/FQ prefix existed, AVGO's annual estimate
    overwrote its quarterly one in place: 11.62543 stored where the quarter's
    3.87377 belonged, finite and plausible and wrong.
    """
    refs = extract.resolve_ref_periods(
        AS_OF, annual, quarter, last_fiscal_year_end=fye, last_report=report
    )
    assert len(refs) == 4
    assert len(set(refs.values())) == 4


def test_resolve_ref_periods_omits_unresolvable_horizons():
    """A ticker with no statements yet yields no estimate rows, never a guess."""
    assert extract.resolve_ref_periods(AS_OF, [], []) == {}
    annual_only = extract.resolve_ref_periods(AS_OF, NVDA_ANNUAL, [])
    assert set(annual_only) == {"0y", "+1y"}
    quarter_only = extract.resolve_ref_periods(AS_OF, [], NVDA_QUARTER)
    assert set(quarter_only) == {"0q", "+1q"}


# --------------------------------------------------------------------------- #
# eps_trend: the 90-day revision history                                       #
# --------------------------------------------------------------------------- #

def test_trend_offsets_are_calendar_days():
    assert extract.TREND_OFFSETS == {
        "current": 0, "7daysAgo": 7, "30daysAgo": 30,
        "60daysAgo": 60, "90daysAgo": 90,
    }


def test_eps_trend_rows_span_four_horizons_and_five_dates(eps_trend, nvda_refs):
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    assert len(rows) == 20
    assert {r.metric for r in rows} == {"epsEst"}
    assert {r.period_type for r in rows} == {"estimate"}
    assert len({r.ref_period for r in rows}) == 4


def test_eps_trend_rows_are_dated_backwards(eps_trend, nvda_refs):
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    fy = sorted(r for r in rows if r.ref_period == "FY2027-01-31")
    assert [r.as_of for r in fy] == [
        "2026-05-17", "2026-06-16", "2026-07-16", "2026-08-08", "2026-08-15",
    ]


def test_eps_trend_rows_capture_the_revision(eps_trend, nvda_refs):
    """NVDA FY consensus moved 8.38 -> 8.96 over 90 days: a +6.9% revision."""
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    fy = {r.as_of: r.value for r in rows if r.ref_period == "FY2027-01-31"}
    assert fy["2026-05-17"] == pytest.approx(8.38125)
    assert fy["2026-08-15"] == pytest.approx(8.95773)


def test_eps_trend_rows_skip_unresolvable_horizons(eps_trend):
    """With no annual statements, the two yearly horizons produce nothing."""
    refs = extract.resolve_ref_periods(
        AS_OF, [], NVDA_QUARTER, last_report=NVDA_LAST_REPORT
    )
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, refs)
    assert len(rows) == 10
    assert {r.ref_period for r in rows} == {"FQ2026-07-31", "FQ2026-10-31"}


def test_eps_trend_rows_skip_missing_values(eps_trend, nvda_refs):
    eps_trend.loc["0y", "30daysAgo"] = float("nan")
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    assert len(rows) == 19


def test_eps_trend_rows_treat_a_zero_lookback_as_missing(eps_trend, nvda_refs):
    """Yahoo pads a lookback it has no data for with 0.0 rather than NaN.

    Observed on 13 of 148 tickers, always in 90daysAgo and never in a newer
    column. Stored, HD's FY2027 series reads 0.00 -> 14.96 and the revision is
    infinite. A missing point must write no row.
    """
    eps_trend.loc["0y", "90daysAgo"] = 0.0
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    fy = [r for r in rows if r.ref_period == "FY2027-01-31"]
    assert len(fy) == 4
    assert "2026-05-17" not in {r.as_of for r in fy}
    assert len(rows) == 19


def test_eps_trend_rows_keep_legitimate_small_values(eps_trend, nvda_refs):
    """Only exact zero is the sentinel; a genuinely tiny estimate must survive."""
    eps_trend.loc["0y", "90daysAgo"] = 0.01
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    assert len(rows) == 20


# --------------------------------------------------------------------------- #
# the point-in-time estimate frames                                            #
# --------------------------------------------------------------------------- #

def test_frame_fields_covers_three_frames():
    assert set(extract.FRAME_FIELDS) == {
        "earnings_estimate", "revenue_estimate", "eps_revisions",
    }


def test_frame_fields_uses_yahoos_inconsistent_casing():
    """yfinance ships upLast7days but downLast7Days. A silent typo drops data."""
    revisions = extract.FRAME_FIELDS["eps_revisions"]
    assert "upLast7days" in revisions
    assert "downLast7Days" in revisions
    assert "downLast30days" in revisions


def test_frame_rows_from_earnings_estimate(nvda_refs):
    frame = _frame("nvda_earnings_estimate.json")
    rows = extract.frame_rows("NVDA", "earnings_estimate", frame, AS_OF, nvda_refs)
    assert {r.as_of for r in rows} == {"2026-08-15"}
    assert {r.period_type for r in rows} == {"estimate"}
    by_metric = {(r.metric, r.ref_period): r.value for r in rows}
    assert by_metric[("epsEstAvg", "FY2027-01-31")] == pytest.approx(8.95773)
    assert by_metric[("epsEstLow", "FY2027-01-31")] == pytest.approx(8.20000)
    assert by_metric[("epsEstAnalysts", "FY2027-01-31")] == 48


def test_frame_rows_from_revenue_estimate(nvda_refs):
    frame = _frame("nvda_revenue_estimate.json")
    rows = extract.frame_rows("NVDA", "revenue_estimate", frame, AS_OF, nvda_refs)
    by_metric = {(r.metric, r.ref_period): r.value for r in rows}
    assert by_metric[("revEstAvg", "FY2027-01-31")] == pytest.approx(393928480900)


def test_frame_rows_from_eps_revisions(nvda_refs):
    frame = _frame("nvda_eps_revisions.json")
    rows = extract.frame_rows("NVDA", "eps_revisions", frame, AS_OF, nvda_refs)
    by_metric = {(r.metric, r.ref_period): r.value for r in rows}
    assert by_metric[("epsRevUp30", "FY2027-01-31")] == 4
    assert by_metric[("epsRevDown30", "FY2027-01-31")] == 0


def test_frame_rows_ignores_unknown_frame(nvda_refs):
    frame = _frame("nvda_eps_revisions.json")
    rows = extract.frame_rows("NVDA", "growth_estimates", frame, AS_OF, nvda_refs)
    assert rows == []


def test_frame_rows_skip_unresolvable_horizons():
    frame = _frame("nvda_earnings_estimate.json")
    refs = extract.resolve_ref_periods(
        AS_OF, NVDA_ANNUAL, [], last_fiscal_year_end=NVDA_FYE,
        last_report=NVDA_LAST_REPORT,
    )
    rows = extract.frame_rows("NVDA", "earnings_estimate", frame, AS_OF, refs)
    assert {r.ref_period for r in rows} == {"FY2027-01-31", "FY2028-01-31"}


def test_degenerate_estimate_row_is_dropped():
    """LITE's real 0y row: avg 8.22662 below its own low of 19.05.

    Every field is finite, so the NaN/inf guard passes it through. Left in, it
    lands as a plausible -66% revision against the next capture.
    """
    frame = _frame("lite_earnings_estimate.json")
    assert extract.is_degenerate_estimate(frame.loc["0y"]) is True
    assert extract.is_degenerate_estimate(frame.loc["0q"]) is False

    refs = {"0q": "FQ2026-09-30", "+1q": "FQ2026-12-31",
            "0y": "FY2026-06-30", "+1y": "FY2027-06-30"}
    rows = extract.frame_rows("LITE", "earnings_estimate", frame, AS_OF, refs)
    assert "FY2026-06-30" not in {r.ref_period for r in rows}
    assert "FQ2026-09-30" in {r.ref_period for r in rows}


def test_healthy_estimate_rows_survive_the_degeneracy_check():
    frame = _frame("nvda_earnings_estimate.json")
    for horizon in frame.index:
        assert extract.is_degenerate_estimate(frame.loc[horizon]) is False


# --------------------------------------------------------------------------- #
# the durable CSV record                                                       #
# --------------------------------------------------------------------------- #

def test_estimates_columns_match_metric_row_fields():
    from history import MetricRow
    assert extract.ESTIMATES_COLUMNS == MetricRow._fields


def test_write_then_read_estimates_csv_roundtrips(tmp_path, eps_trend, nvda_refs):
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    path = tmp_path / "estimates_20260815.csv"

    assert extract.write_estimates_csv(rows, path) == len(rows)
    back = extract.read_estimates_csv(path)

    assert back == rows


def test_estimates_csv_has_a_header(tmp_path, eps_trend, nvda_refs):
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    path = tmp_path / "estimates_20260815.csv"
    extract.write_estimates_csv(rows, path)

    header = path.read_text().splitlines()[0]
    assert header == ",".join(extract.ESTIMATES_COLUMNS)


def test_write_estimates_csv_of_empty_rows_still_writes_header(tmp_path):
    path = tmp_path / "estimates_20260815.csv"
    assert extract.write_estimates_csv([], path) == 0
    assert path.read_text().strip() == ",".join(extract.ESTIMATES_COLUMNS)


def test_read_estimates_csv_preserves_ref_period_as_text(tmp_path, eps_trend,
                                                        nvda_refs):
    """ref_period must stay a string; a date parsed to NaN loses the key."""
    rows = extract.eps_trend_rows("NVDA", eps_trend, AS_OF, nvda_refs)
    path = tmp_path / "estimates_20260815.csv"
    extract.write_estimates_csv(rows, path)

    back = extract.read_estimates_csv(path)
    assert all(isinstance(r.ref_period, str) for r in back)
    assert all(isinstance(r.as_of, str) for r in back)


# --------------------------------------------------------------------------- #
# ownership: holders and insider filings                                       #
# --------------------------------------------------------------------------- #

def _holders_frame():
    """Shaped like yfinance institutional_holders, with mixed report dates.

    The mixed dates are real: NVDA's frame carried both quarters at once.
    """
    return pd.DataFrame([
        {"Date Reported": "2026-06-30", "Holder": "Blackrock Inc.",
         "pctHeld": 0.0802, "Shares": 1941918386, "Value": 437242350903,
         "pctChange": 0.0085},
        {"Date Reported": "2026-03-31", "Holder": "FMR, LLC",
         "pctHeld": 0.0424, "Shares": 1026051548, "Value": 231025770305,
         "pctChange": 0.0324},
    ])


def test_holding_rows_date_each_holder_by_its_own_report():
    rows = extract.holding_rows("NVDA", "institution", _holders_frame())
    assert [r.as_of for r in rows] == ["2026-06-30", "2026-03-31"]
    assert {r.kind for r in rows} == {"institution"}
    assert rows[0].holder == "Blackrock Inc."
    assert rows[0].pct_change == pytest.approx(0.0085)


def test_holding_rows_skip_a_row_with_no_holder_or_date():
    f = _holders_frame()
    f.loc[0, "Holder"] = None
    f.loc[1, "Date Reported"] = None
    assert extract.holding_rows("NVDA", "institution", f) == []


def test_holding_rows_of_empty_frame():
    assert extract.holding_rows("NVDA", "institution", pd.DataFrame()) == []
    assert extract.holding_rows("NVDA", "institution", None) == []


def test_insider_rows_fall_back_to_the_text_column():
    """yfinance leaves Transaction blank and puts the action in Text."""
    f = pd.DataFrame([
        {"Shares": 2410, "Value": 0, "URL": "", "Text": "Stock Award(Grant) at price 0.00 per share.",
         "Insider": "NORA JOHNSON SUZANNE M", "Position": "Director",
         "Transaction": "", "Start Date": "2026-08-10", "Ownership": "D"},
    ])
    rows = extract.insider_rows("NVDA", f)
    assert len(rows) == 1
    assert rows[0].transaction.startswith("Stock Award")
    assert rows[0].as_of == "2026-08-10"
    assert rows[0].position == "Director"


def test_insider_rows_skip_lines_with_no_action_at_all():
    f = pd.DataFrame([
        {"Shares": 1, "Value": 0, "Text": "", "Insider": "X", "Position": "",
         "Transaction": "", "Start Date": "2026-08-10", "Ownership": "D"},
    ])
    assert extract.insider_rows("NVDA", f) == []
