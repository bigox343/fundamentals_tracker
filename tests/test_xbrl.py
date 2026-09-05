import json
from pathlib import Path

import pytest

import xbrl

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_ticker_map_zero_pads_to_ten_digits():
    raw = (FIXTURES / "company_tickers_sample.json").read_bytes()
    m = xbrl.parse_ticker_map(raw)
    assert m["AAPL"] == "0000320193"
    assert m["NVDA"] == "0001045810"
    assert len(m["NET"]) == 10


def test_parse_ticker_map_covers_every_entry():
    raw = (FIXTURES / "company_tickers_sample.json").read_bytes()
    assert set(xbrl.parse_ticker_map(raw)) == {
        "AAPL", "MSFT", "NVDA", "AVGO", "NET"}
