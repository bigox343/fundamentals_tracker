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
