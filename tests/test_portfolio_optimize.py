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


def test_a_zero_book_is_reported_as_degenerate_not_optimal(setup):
    """w=0 satisfies every constraint, so the trivial book always "solves".

    Every constraint is an equality to zero or an upper bound, which means
    this problem is effectively never infeasible -- the realistic failure is
    the solver returning nothing worth holding. That must not read as success.
    """
    mu, rm, sectors, subind = setup
    cfg = portfolio.OptimizerConfig(subindustry_band=0.0, vol_target=1e-9)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)

    assert sol.status == "degenerate"
    assert sol.weights.abs().sum() < portfolio.DEGENERATE_GROSS


def test_infeasible_problem_relaxes_subindustry_first(setup):
    mu, rm, sectors, subind = setup
    # A negative band is genuinely unsatisfiable for any w, including zero,
    # which is what it takes to reach the ladder at all.
    cfg = portfolio.OptimizerConfig(subindustry_band=-0.01)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)

    assert sol.relaxations[0] == "subindustry_band"
    assert sol.status.startswith("relaxed:")


def test_neutrality_is_never_relaxed(setup):
    mu, rm, sectors, subind = setup
    cfg = portfolio.OptimizerConfig(vol_target=1e-9, gross_cap=1e-9,
                                    subindustry_band=0.0)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)

    # Whatever happened, the book is still neutral -- or it failed outright.
    if sol.status != "infeasible":
        assert abs(sol.weights.sum()) < 1e-6
        assert np.abs(rm.B.values.T @ sol.weights.values).max() < 1e-6
    assert "sector_neutral" not in sol.relaxations
    assert "factor_neutral" not in sol.relaxations


def test_hopeless_problem_returns_infeasible_not_garbage(setup):
    mu, rm, sectors, subind = setup
    cfg = portfolio.OptimizerConfig(position_cap=-1.0)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)

    assert sol.status == "infeasible"
    assert (sol.weights == 0).all()
