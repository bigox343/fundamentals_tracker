"""Returns matrix, seasoning filter, and the factor risk model."""
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


def _closes(conn, ticker, prices, start_day=1):
    rows = [MetricRow(ticker, f"2026-01-{start_day + i:02d}", "daily",
                      "close", "", p) for i, p in enumerate(prices)]
    history.upsert_rows(conn, rows)


def test_builds_simple_returns(conn):
    _closes(conn, "AAA", [100.0, 110.0, 121.0])
    rets = portfolio.returns_matrix(conn, end="2026-01-03", window=10)

    assert rets["AAA"].tolist() == pytest.approx([0.10, 0.10])


def test_excludes_names_below_the_seasoning_floor(conn):
    _closes(conn, "OLD", [100.0 + i for i in range(12)])
    _closes(conn, "NEW", [100.0, 101.0])
    rets = portfolio.returns_matrix(conn, end="2026-01-12", window=20)

    assert portfolio.eligible_universe(rets, min_obs=10) == ["OLD"]


def test_seasoning_floor_uses_observation_count_not_span(conn):
    # A name present on the first and last day only must still be excluded.
    _closes(conn, "GAPPY", [100.0, 101.0])
    history.upsert_rows(conn, [MetricRow("GAPPY", "2026-01-12", "daily",
                                         "close", "", 150.0)])
    rets = portfolio.returns_matrix(conn, end="2026-01-12", window=20)

    assert "GAPPY" not in portfolio.eligible_universe(rets, min_obs=10)
