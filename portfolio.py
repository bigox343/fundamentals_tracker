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


def insider_signal(conn, as_of: str, months: int = 12) -> pd.Series:
    """Open-market insider purchases over a trailing window, log1p-scaled.

    Purchases only, deliberately not net: sales outnumber purchases 24:1 in
    this store and insiders sell for diversification, taxes and liquidity, so
    a net measure would be dominated by the uninformative side. Grants, gifts
    and derivative conversions are excluded for the same reason.

    log1p rather than market-cap scaling because marketCap exists only as a
    snapshot metric with days of history, and so cannot be reconstructed
    point-in-time for the backtest.

    Expect this to be empty for roughly two thirds of names: only 32% of the
    universe has any purchase within 12 months. That sparsity is why the
    caller standardizes this leg universe-wide rather than within sub-industry.
    """
    rows = pd.read_sql_query(
        "SELECT ticker, value FROM insider_txns "
        "WHERE txn_type LIKE 'Purchase%' AND as_of <= ? "
        "AND as_of >= date(?, ?)",
        conn, params=[as_of, as_of, f"-{months} months"])
    if rows.empty:
        return pd.Series(dtype=float)

    totals = rows.groupby("ticker")["value"].sum()
    return np.log1p(totals[totals > 0])


LEG_WEIGHTS = {"momentum": 1 / 3, "13f": 1 / 3, "insider": 1 / 3}


def zscore(series: pd.Series, groups: pd.Series | None = None,
           clip: float = 3.0) -> pd.Series:
    """Standardize, optionally within groups, winsorized at +/- `clip`.

    A group whose values are all identical yields zero, not NaN: "no
    dispersion" is neutral information, and a NaN would silently drop the name
    from the optimization.
    """
    if series.empty:
        return series

    def _z(block: pd.Series) -> pd.Series:
        spread = block.std(ddof=0)
        if not np.isfinite(spread) or spread == 0:
            return pd.Series(0.0, index=block.index)
        return (block - block.mean()) / spread

    if groups is None:
        out = _z(series)
    else:
        out = series.groupby(groups.reindex(series.index)).transform(_z)
    return out.clip(-clip, clip)


def blend(legs: dict[str, pd.Series], universe: list[str],
          weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Combine standardized legs into mu, keeping per-leg contributions.

    Every name in `universe` gets a row. A name missing from a sparse leg
    contributes zero for that leg -- neutral -- rather than being dropped,
    which matters because the insider leg is empty for roughly two thirds of
    names by construction.
    """
    weights = LEG_WEIGHTS if weights is None else weights
    frame = pd.DataFrame(index=pd.Index(universe, name="ticker"))

    for name, series in legs.items():
        if series.empty:
            frame[f"contrib_{name}"] = 0.0
        else:
            frame[f"contrib_{name}"] = (
                series.reindex(universe).fillna(0.0) * weights[name])

    frame["mu"] = frame[[f"contrib_{n}" for n in legs]].sum(axis=1)
    return frame


def build_alpha(conn, as_of: str, universe: list[str],
                subindustry: pd.Series) -> pd.DataFrame:
    """The full composite: three legs, standardized, blended.

    Momentum and 13F standardize within sub-industry, matching how the
    dashboard scores. The insider leg standardizes across the full universe
    instead: sub-industry groups have a median of 7 names and roughly 70% of
    them are zero, which does not support a within-group moment estimate.
    """
    legs = {
        "momentum": zscore(momentum_signal(conn, as_of).reindex(universe).dropna(),
                           groups=subindustry),
        "13f": zscore(thirteenf_signal(conn, as_of).reindex(universe).dropna(),
                      groups=subindustry),
        "insider": zscore(insider_signal(conn, as_of).reindex(universe).dropna()),
    }
    return blend(legs, universe)
