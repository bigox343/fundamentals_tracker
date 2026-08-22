"""Alpha legs: momentum, 13F positioning, insider purchases."""
import numpy as np
import pandas as pd
import pytest

import history
import portfolio
from history import MetricRow


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    history.ensure_schema(c)
    return c


def _ramp(conn, ticker, days, start=100.0, step=1.0):
    rows = []
    for i in range(days):
        day = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)
        rows.append(MetricRow(ticker, day.strftime("%Y-%m-%d"), "daily",
                              "close", "", start + i * step))
    history.upsert_rows(conn, rows)


def test_momentum_skips_the_most_recent_month(conn):
    # Flat for 300 days, then a spike in the final 10. A 12-1 signal must not
    # see the spike, because the skip window excludes it.
    rows = []
    for i in range(300):
        day = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)
        price = 100.0 if i < 290 else 500.0
        rows.append(MetricRow("SPIKE", day.strftime("%Y-%m-%d"), "daily",
                              "close", "", price))
    history.upsert_rows(conn, rows)

    end = (pd.Timestamp("2024-01-01") + pd.Timedelta(days=299)).strftime("%Y-%m-%d")
    signal = portfolio.momentum_signal(conn, end, lookback=252, skip=21)

    assert signal["SPIKE"] == pytest.approx(0.0, abs=1e-9)


def test_momentum_is_positive_for_a_riser(conn):
    _ramp(conn, "UP", 300, start=100.0, step=1.0)
    end = (pd.Timestamp("2024-01-01") + pd.Timedelta(days=299)).strftime("%Y-%m-%d")

    assert portfolio.momentum_signal(conn, end)["UP"] > 0


def test_thirteenf_signal_aggregates_managers(conn):
    from history import FilingRow, ThirteenFRow

    def filed(cik, quarter):
        history.upsert_filings(conn, [FilingRow(
            cik, f"Fund{cik}", "Tiger", quarter, f"acc-{cik}-{quarter}",
            "2026-08-14", 10, 1, 1_000_000.0, "ok")])

    def pos(cik, quarter, ticker, shares):
        return ThirteenFRow(cik, f"Fund{cik}", quarter, f"CU{ticker}0010",
                            ticker, ticker, "COM", shares, shares * 10.0)

    for q in ("2026-03-31", "2026-06-30"):
        filed("1", q)
        filed("2", q)

    # Two managers each add 50%: aggregate change is +50%.
    history.upsert_thirteenf(conn, [
        pos("1", "2026-03-31", "AAA", 100), pos("2", "2026-03-31", "AAA", 100),
        pos("1", "2026-06-30", "AAA", 150), pos("2", "2026-06-30", "AAA", 150),
    ])

    signal = portfolio.thirteenf_signal(conn, "2026-08-21")
    assert signal["AAA"] == pytest.approx(0.5)


def test_thirteenf_signal_is_split_adjusted(conn):
    """A 10:1 split must not read as a manager adding 900%."""
    from history import FilingRow, ThirteenFRow

    for q in ("2026-03-31", "2026-06-30"):
        history.upsert_filings(conn, [FilingRow(
            "1", "Fund1", "Tiger", q, f"acc-{q}", "2026-08-14",
            10, 1, 1_000_000.0, "ok")])

    # Stored closes are back-adjusted, so BOTH quarters sit on the post-split
    # basis of 100. The split is visible only as the gap between the filed
    # price-per-share (1000 in Q1, 100 in Q2) and that adjusted close.
    history.upsert_rows(conn, [
        MetricRow("SPL", "2026-03-31", "daily", "close", "", 100.0),
        MetricRow("SPL", "2026-06-30", "daily", "close", "", 100.0),
    ])
    history.upsert_thirteenf(conn, [
        ThirteenFRow("1", "Fund1", "2026-03-31", "CUSPL0010", "SPL", "SPL",
                     "COM", 100, 100_000.0),
        ThirteenFRow("1", "Fund1", "2026-06-30", "CUSPL0010", "SPL", "SPL",
                     "COM", 1000, 100_000.0),
    ])

    signal = portfolio.thirteenf_signal(conn, "2026-08-21")
    assert signal["SPL"] == pytest.approx(0.0, abs=1e-9)
