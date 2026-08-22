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


# --------------------------------------------------------------------------- #
# Alpha legs                                                                   #
# --------------------------------------------------------------------------- #
def momentum_signal(conn, as_of: str, lookback: int = 252,
                    skip: int = 21) -> pd.Series:
    """12-1 momentum: return over `lookback` days, excluding the last `skip`.

    The skip is not optional. The most recent month carries short-term
    reversal, which works against medium-term momentum; including it degrades
    the signal rather than sharpening it.
    """
    closes = pd.read_sql_query(
        "SELECT ticker, as_of, value FROM metrics "
        "WHERE period_type = 'daily' AND metric = 'close' AND as_of <= ? "
        "ORDER BY as_of", conn, params=[as_of])
    if closes.empty:
        return pd.Series(dtype=float)

    wide = closes.pivot(index="as_of", columns="ticker", values="value")
    if len(wide) < lookback + 1:
        lookback = len(wide) - 1

    end = wide.iloc[-(skip + 1)]
    start = wide.iloc[-(lookback + 1)]
    return (end / start - 1.0).dropna()


def thirteenf_signal(conn, as_of: str) -> pd.Series:
    """Aggregate quarter-over-quarter change in tracked-manager ownership.

    Delegates to history.thirteenf_changes, which already puts last quarter's
    counts on this quarter's share basis. Differencing raw as-filed counts
    would render a 25:1 split as a manager adding 2,400%, and the error is
    worse in a difference than in a level.
    """
    import history

    # Sorted explicitly: thirteenf_quarters returns newest-first, and relying
    # on that ordering silently inverts the sign of every delta.
    quarters = sorted(q for q in history.thirteenf_quarters(conn) if q <= as_of)
    if len(quarters) < 2:
        return pd.Series(dtype=float)

    changes = history.thirteenf_changes(conn, quarters[-1], quarters[-2])
    if changes.empty:
        return pd.Series(dtype=float)

    grouped = changes.groupby("ticker")[["shares", "prev_shares"]].sum()
    prior = grouped["prev_shares"].where(grouped["prev_shares"] > 0)
    return ((grouped["shares"] - grouped["prev_shares"]) / prior).dropna()
