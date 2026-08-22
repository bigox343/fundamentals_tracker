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


def test_sector_factors_are_universe_relative_and_drop_one():
    rets = pd.DataFrame({
        "T1": [0.02, 0.01], "T2": [0.02, 0.01],     # TMT
        "I1": [-0.01, 0.03], "I2": [-0.01, 0.03],   # Industrials
        "C1": [0.00, 0.00], "C2": [0.00, 0.00],     # Consumer
    })
    sectors = pd.Series({"T1": "TMT", "T2": "TMT", "I1": "Industrials",
                         "I2": "Industrials", "C1": "Consumer", "C2": "Consumer"})
    sf = portfolio.sector_factors(rets, sectors)

    # Consumer is the implicit base: only two columns survive.
    assert sorted(sf.columns) == ["SEC_Industrials", "SEC_TMT"]
    # Universe-relative: TMT beat the universe mean on day 0.
    assert sf.iloc[0]["SEC_TMT"] > 0


def _synthetic(n_names=40, n_days=504, seed=0):
    rng = np.random.default_rng(seed)
    factor_returns = pd.DataFrame(
        rng.normal(0, 0.01, (n_days, 6)),
        columns=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD"])
    loadings = rng.normal(1.0, 0.3, (n_names, 6))
    noise = rng.normal(0, 0.01, (n_days, n_names))
    rets = pd.DataFrame(factor_returns.values @ loadings.T + noise,
                        columns=[f"T{i}" for i in range(n_names)])
    sectors = pd.Series({f"T{i}": ["TMT", "Industrials", "Consumer"][i % 3]
                         for i in range(n_names)})
    return rets, factor_returns, sectors


def test_covariance_is_positive_definite_and_better_conditioned():
    rets, factor_returns, sectors = _synthetic()
    rm = portfolio.estimate_risk(rets, factor_returns, sectors)
    cov = portfolio.covariance(rm)

    assert np.all(np.linalg.eigvalsh(cov) > 0)
    assert np.linalg.cond(cov) < np.linalg.cond(np.cov(rets.values.T))


def test_specific_variance_is_floored():
    rng = np.random.default_rng(1)
    factor_returns = pd.DataFrame(rng.normal(0, 0.01, (504, 6)),
                                  columns=["Mkt-RF", "SMB", "HML", "RMW",
                                           "CMA", "UMD"])
    # A name that is an exact linear function of the factors has zero residual.
    exact = factor_returns.sum(axis=1)
    rets = pd.DataFrame({"EXACT": exact,
                         "NOISY": exact + rng.normal(0, 0.01, 504)})
    sectors = pd.Series({"EXACT": "TMT", "NOISY": "TMT"})

    rm = portfolio.estimate_risk(rets, factor_returns, sectors)

    assert rm.D.min() > 0


def test_sector_base_is_dropped_for_real_world_labels():
    """Regression: a hardcoded base label matched nothing and kept all three.

    The store's sectors are full labels, not the bare word they begin with,
    so matching on "Consumer" silently left every sector column in B and
    reintroduced the collinearity the base is meant to remove.
    """
    rets = pd.DataFrame({
        "T1": [0.02, 0.01], "I1": [-0.01, 0.03], "C1": [0.00, 0.00],
    })
    sectors = pd.Series({
        "T1": "TMT (Tech · Media · Telecom)",
        "I1": "Industrials",
        "C1": "Consumer (Staples · Discretionary)",
    })
    sf = portfolio.sector_factors(rets, sectors)

    assert len(sf.columns) == 2
    assert not any("Consumer" in c for c in sf.columns)
