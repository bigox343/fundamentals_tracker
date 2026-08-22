"""Fama-French factor parsing: preamble, percent units, trailing blocks."""
from pathlib import Path

import pytest

import factors

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_only_the_dated_rows():
    text = (FIXTURES / "ff5_daily_sample.csv").read_text()
    df = factors.parse_french_csv(text)

    assert list(df.index) == ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert list(df.columns) == ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]


def test_converts_percent_to_decimal():
    text = (FIXTURES / "ff5_daily_sample.csv").read_text()
    df = factors.parse_french_csv(text)

    # 1.05 percent must land as 0.0105, not 1.05.
    assert df.loc["2026-01-02", "Mkt-RF"] == pytest.approx(0.0105)
    assert df.loc["2026-01-05", "SMB"] == pytest.approx(0.0031)


def test_ignores_copyright_footer_and_blank_lines():
    text = (FIXTURES / "ff5_daily_sample.csv").read_text()
    df = factors.parse_french_csv(text)

    assert len(df) == 3
    assert df.notna().all().all()
