"""Walk-forward backtest of the market-neutral book.

Fetches nothing and renders nothing: it reads the store, replays the
optimizer month by month, and reports what happened.

Read section 9 of the design spec before quoting any number this produces.
Twenty-four monthly observations with seven independent 13F changes detects a
broken signal; it does not distinguish a good one from a mediocre one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import factors           # noqa: E402
import history           # noqa: E402
import portfolio         # noqa: E402

COST_PER_TURNOVER = 0.0010     # 10 bps per unit of turnover


def rebalance_dates(rets: pd.DataFrame, freq: str = "M") -> list[str]:
    """Last available trading day of each period in the return index."""
    idx = pd.to_datetime(pd.Series(rets.index))
    return sorted(idx.groupby(idx.dt.to_period(freq)).max()
                     .dt.strftime("%Y-%m-%d").tolist())


def signal_edge(signal: pd.Series, forward: pd.Series) -> float:
    """Rank correlation between a signal and the return that followed it."""
    return float(signal.corr(forward, method="spearman"))


def run(conn, start: str, end: str, verbose: bool = False) -> pd.DataFrame:
    """Replay the optimizer at each month end, holding to the next."""
    factor_returns = factors.load_cached()
    companies = pd.read_sql_query(
        "SELECT ticker, sector, subindustry FROM companies", conn)
    sectors = companies.set_index("ticker")["sector"]
    subind = companies.set_index("ticker")["subindustry"]

    full = portfolio.returns_matrix(conn, end, window=10_000)
    dates = [d for d in rebalance_dates(full) if start <= d <= end]

    prev, records = None, []
    for i, as_of in enumerate(dates[:-1]):
        rets = portfolio.returns_matrix(conn, as_of)
        universe = portfolio.eligible_universe(rets)
        if len(universe) < 20:
            continue

        rm = portfolio.estimate_risk(rets[universe], factor_returns, sectors)
        alpha = portfolio.build_alpha(conn, as_of, rm.tickers, subind)
        sol = portfolio.solve(alpha["mu"], rm, sectors, subind, prev=prev)

        # Held from the day after this rebalance through the next one.
        forward = full.loc[(full.index > as_of) & (full.index <= dates[i + 1])]
        realized = float(
            (forward[rm.tickers].fillna(0.0) @ sol.weights.values).sum())
        base = prev.reindex(rm.tickers).fillna(0.0) if prev is not None else 0.0
        turnover = float((sol.weights - base).abs().sum())

        records.append({"as_of": as_of,
                        "ret": realized - turnover * COST_PER_TURNOVER,
                        "gross_ret": realized,
                        "turnover": turnover,
                        "gross": float(sol.weights.abs().sum()),
                        "status": sol.status})
        if verbose:
            print(f"{as_of}  ret {realized:+.3%}  turnover {turnover:.2f}  "
                  f"{sol.status}")
        prev = sol.weights

    return pd.DataFrame(records)


def summarize(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"periods": 0}
    rets = frame["ret"]
    vol = rets.std(ddof=1)
    curve = (1 + rets).cumprod()
    return {
        "periods": len(frame),
        "mean_return": float(rets.mean()),
        "ir": float(rets.mean() / vol * np.sqrt(12)) if vol > 0 else float("nan"),
        "mean_turnover": float(frame["turnover"].mean()),
        "max_drawdown": float((curve / curve.cummax() - 1).min()),
        "relaxed_periods": int((frame["status"] != "optimal").sum()),
    }


if __name__ == "__main__":
    conn = history.connect()
    result = run(conn, "2024-09-01", "2026-08-21", verbose=True)
    print()
    for key, value in summarize(result).items():
        print(f"{key:20s} {value}")
