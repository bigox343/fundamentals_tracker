"""Portfolio construction: alpha composite, factor risk model, cvxpy solve.

Reads the store and returns frames. Never fetches, never writes HTML, and
never writes SQL -- persistence goes through history.py, which is where SQL
knowledge lives.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WINDOW = 504      # trading days for covariance and loadings
MIN_OBS = 400     # seasoning floor within that window


def returns_matrix(conn, end: str, window: int = WINDOW) -> pd.DataFrame:
    """Daily simple returns, dates x tickers, ending on or before `end`."""
    closes = pd.read_sql_query(
        "SELECT ticker, as_of, value FROM metrics "
        "WHERE period_type = 'daily' AND metric = 'close' AND as_of <= ? "
        "ORDER BY as_of", conn, params=[end])
    if closes.empty:
        return pd.DataFrame()

    wide = closes.pivot(index="as_of", columns="ticker", values="value")
    wide = wide.tail(window + 1)
    return wide.pct_change().iloc[1:]


def eligible_universe(rets: pd.DataFrame, min_obs: int = MIN_OBS) -> list[str]:
    """Names with enough observations to support a variance estimate.

    Counted as present observations, not calendar span: a name with two
    prices six months apart has a span but no estimable variance.
    """
    if rets.empty:
        return []
    counts = rets.notna().sum()
    return sorted(counts[counts >= min_obs].index)
