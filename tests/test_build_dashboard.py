"""Tests for build_dashboard's price-history helpers.

No network: yf.download is monkeypatched to return a synthetic frame shaped
like yfinance's real auto_adjust=False response (a 'Adj Close'/'Close'/...
MultiIndex over tickers), so these tests exercise the column-selection logic
without ever leaving the machine.
"""
import pandas as pd
import pytest

import build_dashboard as bd


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
