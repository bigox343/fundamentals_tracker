"""The cvxpy problem: does every constraint actually hold in the solution."""
import numpy as np
import pandas as pd
import pytest

import portfolio


@pytest.fixture
def setup():
    rng = np.random.default_rng(7)
    n = 30
    tickers = [f"T{i}" for i in range(n)]
    sectors = pd.Series({t: ["TMT", "Industrials", "Consumer"][i % 3]
                         for i, t in enumerate(tickers)})
    subind = pd.Series({t: f"sub{i % 6}" for i, t in enumerate(tickers)})

    factor_names = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD",
                    "SEC_Industrials", "SEC_TMT"]
    B = pd.DataFrame(rng.normal(0, 0.5, (n, 8)), index=tickers,
                     columns=factor_names)
    F = np.diag(rng.uniform(0.01, 0.04, 8))
    D = rng.uniform(0.02, 0.06, n)
    rm = portfolio.RiskModel(B, F, D, tickers, factor_names)
    mu = pd.Series(rng.normal(0, 1, n), index=tickers)
    return mu, rm, sectors, subind


def test_solution_is_dollar_and_factor_neutral(setup):
    mu, rm, sectors, subind = setup
    sol = portfolio.solve(mu, rm, sectors, subind)
    w = sol.weights.values

    assert abs(w.sum()) < 1e-6
    assert np.abs(rm.B.values.T @ w).max() < 1e-6


def test_solution_respects_position_gross_and_vol_caps(setup):
    mu, rm, sectors, subind = setup
    cfg = portfolio.OptimizerConfig(position_cap=0.04, gross_cap=2.0,
                                    vol_target=0.08)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)
    w = sol.weights.values
    cov = portfolio.covariance(rm)

    assert np.abs(w).max() <= 0.04 + 1e-6
    assert np.abs(w).sum() <= 2.0 + 1e-6
    assert np.sqrt(w @ cov @ w) <= 0.08 + 1e-6


def test_solution_is_sector_neutral(setup):
    mu, rm, sectors, subind = setup
    sol = portfolio.solve(mu, rm, sectors, subind)

    for sector in sectors.unique():
        members = [t for t in sol.weights.index if sectors[t] == sector]
        assert abs(sol.weights[members].sum()) < 1e-6


def test_turnover_penalty_pulls_toward_previous_weights(setup):
    mu, rm, sectors, subind = setup
    prev = pd.Series(0.0, index=rm.tickers)
    prev.iloc[0] = 0.04

    loose = portfolio.solve(mu, rm, sectors, subind, prev=prev,
                            config=portfolio.OptimizerConfig(turnover_penalty=0.0))
    tight = portfolio.solve(mu, rm, sectors, subind, prev=prev,
                            config=portfolio.OptimizerConfig(turnover_penalty=5.0))

    assert (tight.weights - prev).abs().sum() < (loose.weights - prev).abs().sum()
