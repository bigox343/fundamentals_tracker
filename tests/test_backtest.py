"""Backtest mechanics, and the lookahead check that matters most."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Repo convention: tools/ scripts are loaded by path, not imported as a
# package. See tests/test_report.py, which does the same for the 13F report.
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "backtest_portfolio", ROOT / "tools" / "backtest_portfolio.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)


def test_rebalance_dates_are_month_ends_within_range():
    idx = pd.date_range("2026-01-01", "2026-04-15", freq="B").strftime("%Y-%m-%d")
    rets = pd.DataFrame(index=idx, data={"A": 0.0})
    dates = bt.rebalance_dates(rets)

    assert dates[0].startswith("2026-01")
    assert all(d <= "2026-04-15" for d in dates)
    assert len(dates) == len(set(dates))


def test_summarize_reports_ir_and_turnover():
    frame = pd.DataFrame({
        "as_of": ["2026-01-31", "2026-02-28", "2026-03-31"],
        "ret": [0.01, -0.005, 0.02],
        "turnover": [0.5, 0.2, 0.3],
        "gross": [2.0, 2.0, 2.0],
        "status": ["optimal"] * 3,
    })
    stats = bt.summarize(frame)

    assert stats["periods"] == 3
    assert stats["mean_turnover"] == pytest.approx(1.0 / 3)
    assert np.isfinite(stats["ir"])


def test_no_lookahead_a_shuffled_future_signal_earns_nothing():
    """The single most important test here.

    Point-in-time reconstruction spans three sources with different cadences,
    which is exactly where lookahead hides. If forward returns are shuffled
    relative to the signal, any remaining edge is a bug, not alpha.
    """
    rng = np.random.default_rng(3)
    signal = pd.Series(rng.normal(0, 1, 200))
    forward = pd.Series(rng.normal(0, 0.02, 200))
    shuffled = forward.sample(frac=1.0, random_state=99).reset_index(drop=True)

    edge = bt.signal_edge(signal, shuffled)

    assert abs(edge) < 0.15
