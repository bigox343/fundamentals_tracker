"""Portfolio construction: alpha composite, factor risk model, cvxpy solve.

Reads the store and returns frames. Never fetches, never writes HTML, and
never writes SQL -- persistence goes through history.py, which is where SQL
knowledge lives.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import NamedTuple

import cvxpy as cp

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


# --------------------------------------------------------------------------- #
# Factor risk model                                                            #
# --------------------------------------------------------------------------- #
FF_FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD"]
VAR_FLOOR = 1e-8              # no name is riskless
TRADING_DAYS = 252


class RiskModel(NamedTuple):
    """Sigma = B F B' + D, with the pieces kept separate.

    This tuple is the substitution seam: a commercial vendor model, or a PCA
    alternative, can be dropped in here without the optimizer changing.
    """
    B: pd.DataFrame           # tickers x factors
    F: np.ndarray             # factors x factors
    D: np.ndarray             # per-ticker specific variance
    tickers: list[str]
    factor_names: list[str]


def sector_factors(rets: pd.DataFrame, sectors: pd.Series,
                   base: str | None = None) -> pd.DataFrame:
    """Universe-relative sector return series, one column short of the sectors.

    Raw sector returns correlate ~0.9 with the market, so regressing on both
    gives unstable loadings; subtracting the universe mean removes most of
    that. The relative series are near-collinear by construction, so one
    sector is dropped and absorbed into the intercept.

    `base` defaults to the alphabetically first sector rather than a hardcoded
    label. Matching on a fixed string is how this silently returned all three
    columns: the store's sectors are full labels like
    "Consumer (Staples - Discretionary)", not the bare word they start with.
    """
    universe_mean = rets.mean(axis=1)
    present = sorted(set(sectors.reindex(rets.columns).dropna()))
    if base is None and present:
        base = present[0]
    names = [s for s in present if s != base]
    columns = {}
    for sector in names:
        members = [t for t in rets.columns if sectors.get(t) == sector]
        if members:
            columns[f"SEC_{sector}"] = rets[members].mean(axis=1) - universe_mean
    return pd.DataFrame(columns, index=rets.index)


def estimate_risk(rets: pd.DataFrame, factor_returns: pd.DataFrame,
                  sectors: pd.Series, min_obs: int = MIN_OBS) -> RiskModel:
    """B by time-series regression, F from factor history, D from residuals."""
    ff = factor_returns.reindex(rets.index)[FF_FACTORS]
    panel = pd.concat([ff, sector_factors(rets, sectors)], axis=1).dropna()
    if panel.empty:
        raise ValueError("no overlap between returns and factor history")
    aligned = rets.loc[panel.index]

    X = np.column_stack([np.ones(len(panel)), panel.values])
    tickers, loadings, specific = [], [], []

    for ticker in aligned.columns:
        y = aligned[ticker]
        mask = y.notna().values
        if mask.sum() < min(min_obs, len(panel)):
            continue
        coef, *_ = np.linalg.lstsq(X[mask], y.values[mask], rcond=None)
        residual = y.values[mask] - X[mask] @ coef
        tickers.append(ticker)
        loadings.append(coef[1:])                     # drop the intercept
        specific.append(max(residual.var(ddof=1), VAR_FLOOR) * TRADING_DAYS)

    B = pd.DataFrame(loadings, index=tickers, columns=panel.columns)
    F = np.cov(panel.values.T) * TRADING_DAYS
    return RiskModel(B, F, np.array(specific), tickers, list(panel.columns))


def covariance(rm: RiskModel) -> np.ndarray:
    """Sigma = B F B' + D, positive-definite by construction."""
    return rm.B.values @ rm.F @ rm.B.values.T + np.diag(rm.D)


# --------------------------------------------------------------------------- #
# Optimization                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptimizerConfig:
    vol_target: float = 0.08          # annualized
    gross_cap: float = 2.0
    position_cap: float = 0.04
    subindustry_band: float = 0.10
    turnover_penalty: float = 0.001


class Solution(NamedTuple):
    weights: pd.Series
    status: str                       # 'optimal' | 'relaxed:...' | 'infeasible'
    relaxations: list[str]


def _masks(index, labels: pd.Series) -> list[np.ndarray]:
    out = []
    for value in sorted(set(labels.reindex(index).dropna())):
        out.append(np.array([1.0 if labels.get(t) == value else 0.0
                             for t in index]))
    return out


def _problem(mu, cov, B, sector_masks, subind_masks, prev, cfg):
    """Assemble the cvxpy problem.

    Split out so the relaxation ladder can rebuild it with a loosened config
    rather than mutating a solved problem in place.
    """
    n = len(mu)
    w = cp.Variable(n)

    objective = mu.values @ w
    if prev is not None and cfg.turnover_penalty > 0:
        objective = objective - cfg.turnover_penalty * cp.norm1(w - prev.values)

    constraints = [
        cp.sum(w) == 0,                                    # dollar neutral
        B.values.T @ w == 0,                               # factor neutral
        cp.quad_form(w, cp.psd_wrap(cov)) <= cfg.vol_target ** 2,
        cp.norm1(w) <= cfg.gross_cap,
        cp.abs(w) <= cfg.position_cap,
    ]
    for mask in sector_masks:
        constraints.append(mask @ w == 0)                  # sector neutral
    for mask in subind_masks:
        constraints.append(cp.abs(mask @ w) <= cfg.subindustry_band)

    return w, cp.Problem(cp.Maximize(objective), constraints)


def solve(mu: pd.Series, rm: RiskModel, sectors: pd.Series,
          subindustry: pd.Series, prev: pd.Series | None = None,
          config: OptimizerConfig | None = None) -> Solution:
    """Solve for target weights, relaxing in a documented order if infeasible."""
    cfg = OptimizerConfig() if config is None else config
    mu = mu.reindex(rm.tickers).fillna(0.0)
    cov = covariance(rm)
    prev = None if prev is None else prev.reindex(rm.tickers).fillna(0.0)

    sector_masks = _masks(rm.tickers, sectors)
    subind_masks = _masks(rm.tickers, subindustry)

    w, problem = _problem(mu, cov, rm.B, sector_masks, subind_masks, prev, cfg)
    problem.solve()

    if problem.status in ("optimal", "optimal_inaccurate"):
        weights = pd.Series(w.value, index=rm.tickers)
        return Solution(weights, _grade(weights), [])

    return _relax(mu, cov, rm, sector_masks, subind_masks, prev, cfg)


# Every constraint here is either an equality to zero or an upper bound, so
# w = 0 always satisfies all of them: this problem is effectively never
# infeasible. The failure that actually happens is the trivial book -- the
# solver finding nothing worth holding -- which looks like success unless it
# is named. A zero book must never be mistaken for a solved one.
DEGENERATE_GROSS = 1e-4


def _grade(weights: pd.Series) -> str:
    if float(weights.abs().sum()) < DEGENERATE_GROSS:
        return "degenerate"
    return "optimal"


# Relaxed in this order and no other. Dollar, sector and factor neutrality are
# absent by design: they define what the portfolio is, and a book that quietly
# stopped being neutral is worse than no book at all.
_LADDER = [
    ("subindustry_band",
     lambda c: replace(c, subindustry_band=min(c.subindustry_band * 4 + 0.05, 1.0))),
    ("gross_cap", lambda c: replace(c, gross_cap=c.gross_cap * 1.5)),
    ("vol_target", lambda c: replace(c, vol_target=c.vol_target * 1.5)),
]


def _relax(mu, cov, rm, sector_masks, subind_masks, prev, cfg) -> Solution:
    """Loosen one constraint at a time, recording each step that was taken."""
    applied: list[str] = []

    for name, loosen in _LADDER:
        cfg = loosen(cfg)
        applied.append(name)
        w, problem = _problem(mu, cov, rm.B, sector_masks, subind_masks, prev, cfg)
        try:
            problem.solve()
        except cp.error.SolverError:
            continue
        if problem.status in ("optimal", "optimal_inaccurate"):
            weights = pd.Series(w.value, index=rm.tickers)
            if _grade(weights) == "degenerate":
                continue
            return Solution(weights, f"relaxed:{'+'.join(applied)}", applied)

    # Still infeasible. The previous book stands; say so rather than invent one.
    return Solution(pd.Series(0.0, index=rm.tickers), "infeasible", applied)
