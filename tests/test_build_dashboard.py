"""Tests for build_dashboard's price-history helpers.

No network: yf.download is monkeypatched to return a synthetic frame shaped
like yfinance's real auto_adjust=False response (a 'Adj Close'/'Close'/...
MultiIndex over tickers), so these tests exercise the column-selection logic
without ever leaving the machine.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

import build_dashboard as bd
import valuation

# own_history() imports prove_xbrl the same way build_dashboard.py does --
# by adding tools/ to sys.path and importing it by name -- so tests that
# monkeypatch prove_xbrl.report must reach the very same module object.
_TOOLS = Path(bd.__file__).resolve().parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
import prove_xbrl


def _fake_auto_adjust_false_response() -> pd.DataFrame:
    """Two tickers, two dates, six OHLC-style columns -- yfinance's real shape
    for a multi-symbol auto_adjust=False download."""
    idx = pd.to_datetime(["2026-09-03", "2026-09-04"])
    columns = ["Adj Close", "Close", "High", "Low", "Open", "Volume"]
    tickers = ["AAPL", "VZ"]
    data = {}
    for col in columns:
        for tk in tickers:
            # Adj Close and Close deliberately differ (dividend adjustment);
            # the other columns are filler and never read by fetch_close_pair.
            if col == "Adj Close":
                data[(col, tk)] = [100.0, 101.0] if tk == "AAPL" else [40.0, 40.5]
            elif col == "Close":
                data[(col, tk)] = [100.0, 101.0] if tk == "AAPL" else [55.0, 55.5]
            else:
                data[(col, tk)] = [1.0, 1.0]
    return pd.DataFrame(data, index=idx)


def test_fetch_close_pair_returns_frames_with_identical_index_and_columns(monkeypatch):
    """The daily record path relies on this: two series derived from one
    response can never disagree on which tickers or which dates are covered,
    unlike two independent downloads that could each drop a different name."""
    monkeypatch.setattr(bd.yf, "download",
                        lambda *a, **k: _fake_auto_adjust_false_response())
    adjusted, raw = bd.fetch_close_pair(["AAPL", "VZ"])
    assert list(adjusted.columns) == list(raw.columns)
    assert adjusted.index.equals(raw.index)
    assert not adjusted.empty and not raw.empty


def test_fetch_close_pair_maps_adj_close_and_close_to_the_right_series(monkeypatch):
    """Guards against a swap: `adjusted` must come from 'Adj Close' (the
    series that matches the existing `close` metric) and `raw` from 'Close'
    (split-adjusted only, the basis a market cap needs)."""
    monkeypatch.setattr(bd.yf, "download",
                        lambda *a, **k: _fake_auto_adjust_false_response())
    adjusted, raw = bd.fetch_close_pair(["AAPL", "VZ"])
    assert adjusted.loc["2026-09-03", "VZ"] == pytest.approx(40.0)
    assert raw.loc["2026-09-03", "VZ"] == pytest.approx(55.0)
    # VZ's dividend-adjusted price reads well below what the market actually
    # paid, same direction as the measured 37.2% gap fetch_close_pair's
    # docstring cites.
    assert raw.loc["2026-09-03", "VZ"] > adjusted.loc["2026-09-03", "VZ"]


def test_fetch_close_pair_of_no_symbols_is_two_empty_frames():
    adjusted, raw = bd.fetch_close_pair([])
    assert adjusted.empty and raw.empty


def _long_enough(values):
    # own_percentile needs a full trading year (MIN_HISTORY=250) before it
    # returns anything -- a shorter series is exactly what the second test
    # below is checking gets skipped.
    return pd.Series(values, dtype=float)


def test_own_history_keeps_only_pairs_that_passed_the_proof(monkeypatch):
    series = {
        "AAPL": pd.DataFrame({"trailingPE": _long_enough(range(300))}),
        "MSFT": pd.DataFrame({"trailingPE": _long_enough(range(300))}),
    }
    monkeypatch.setattr(valuation, "build_all", lambda conn, tickers: series)
    monkeypatch.setattr(prove_xbrl, "report", lambda conn, built=None: pd.DataFrame([
        {"ticker": "AAPL", "metric": "trailingPE", "median_error": 0.001, "passed": True},
        {"ticker": "MSFT", "metric": "trailingPE", "median_error": 0.5, "passed": False},
    ]))
    df = pd.DataFrame({"ticker": ["AAPL", "MSFT"]})
    out = bd.own_history(object(), df)
    assert ("AAPL", "trailingPE") in out
    assert ("MSFT", "trailingPE") not in out, "a pair that failed the proof must not render"


def test_own_history_skips_a_pair_that_passed_but_lacks_enough_history(monkeypatch):
    series = {"AAPL": pd.DataFrame({"trailingPE": _long_enough(range(10))})}
    monkeypatch.setattr(valuation, "build_all", lambda conn, tickers: series)
    monkeypatch.setattr(prove_xbrl, "report", lambda conn, built=None: pd.DataFrame([
        {"ticker": "AAPL", "metric": "trailingPE", "median_error": 0.0, "passed": True},
    ]))
    df = pd.DataFrame({"ticker": ["AAPL"]})
    out = bd.own_history(object(), df)
    assert out == {}, "MIN_HISTORY is not to be re-floored, but it must still be honored"


def test_own_history_hands_its_series_to_report_instead_of_letting_it_rebuild(monkeypatch):
    # The whole point of Task 12's amendment 2: build_all() is ~11s, and
    # prove_xbrl.report() must reuse this call's result rather than
    # recomputing the identical series itself.
    series = {"AAPL": pd.DataFrame({"trailingPE": _long_enough(range(300))})}
    monkeypatch.setattr(valuation, "build_all", lambda conn, tickers: series)
    captured = {}

    def _fake_report(conn, built=None):
        captured["built"] = built
        return pd.DataFrame([{"ticker": "AAPL", "metric": "trailingPE",
                              "median_error": 0.0, "passed": True}])

    monkeypatch.setattr(prove_xbrl, "report", _fake_report)
    bd.own_history(object(), pd.DataFrame({"ticker": ["AAPL"]}))
    assert captured["built"] is series


def test_own_history_is_non_fatal_when_something_raises(monkeypatch, capsys):
    def _boom(conn, tickers):
        raise RuntimeError("SEC is down")

    monkeypatch.setattr(valuation, "build_all", _boom)
    out = bd.own_history(object(), pd.DataFrame({"ticker": ["AAPL"]}))
    assert out == {}
    assert "own-history frame unavailable" in capsys.readouterr().err
