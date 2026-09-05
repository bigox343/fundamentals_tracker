import math

import pandas as pd
import pytest

import render


def test_fmt_handles_each_kind():
    assert render.fmt(12.345, "x") == "12.3x"
    assert render.fmt(1.5e12, "bigusd") == "$1.50T"
    assert render.fmt(42.0, "pct") == "42.0%"
    assert render.fmt(9.5, "usd") == "$9.50"


def test_fmt_renders_missing_as_a_dash():
    assert "na" in render.fmt(None, "x")
    assert "na" in render.fmt(float("nan"), "x")


def test_cell_style_is_blue_above_zero_and_red_below():
    assert "37,106,191" in render.cell_style(0.8)
    assert "208,59,59" in render.cell_style(-0.8)
    assert render.cell_style(None) == ""


def test_cell_style_alpha_saturates_at_the_documented_ceiling():
    assert "0.340" in render.cell_style(1.0)
    assert "0.340" in render.cell_style(5.0)


def test_an_out_of_domain_cell_is_marked_and_explained():
    df = pd.DataFrame({
        "ticker": ["NET", "ORCL", "MSFT", "FTNT"],
        "name": ["Cloudflare", "Oracle", "Microsoft", "Fortinet"],
        "subindustry": ["Infra"] * 4,
        "marketCap": [3e10, 3e11, 3e12, 6e10],
        "evEbitda": [-27887.75, 19.18, 19.77, 42.84],
    }).set_index("ticker", drop=False)
    metrics = [("evEbitda", "EV/EBITDA", "val", "x", False)]
    html = render.render_sector(
        "TMT", df, {}, metrics, {"TMT": ["Infra"]}, {"val": "Valuation"},
        {"evEbitda": lambda d: pd.to_numeric(d["evEbitda"], errors="coerce") > 0},
    )
    net = html[html.index('data-tk="NET"'):]
    net = net[:net.index("</tr>")]
    assert "undef" in net
    assert "title=" in net
    assert "data-ss" not in net


def test_an_in_domain_cell_is_not_marked():
    df = pd.DataFrame({
        "ticker": ["ORCL", "MSFT", "FTNT"],
        "name": ["Oracle", "Microsoft", "Fortinet"],
        "subindustry": ["Infra"] * 3,
        "marketCap": [3e11, 3e12, 6e10],
        "evEbitda": [19.18, 19.77, 42.84],
    }).set_index("ticker", drop=False)
    metrics = [("evEbitda", "EV/EBITDA", "val", "x", False)]
    html = render.render_sector(
        "TMT", df, {}, metrics, {"TMT": ["Infra"]}, {"val": "Valuation"},
        {"evEbitda": lambda d: pd.to_numeric(d["evEbitda"], errors="coerce") > 0},
    )
    assert "undef" not in html


def test_a_thin_valid_band_leaves_a_well_defined_cell_unmarked():
    # Mirrors test_domains.py's test_a_band_that_falls_under_three_valid_names_
    # scores_nothing: 3 names, 2 negative EV/EBITDA (out of domain) leave only
    # one valid value (12.0), which is under relative_scores' floor of three,
    # so the whole band scores {} -- ss is None for A, B, *and* C alike. C's
    # 12.0 is a real, well-defined ratio; it just has no peers to be scored
    # against, the existing "thin peer set" case. Only A and B, whose own
    # values fail `> 0`, may carry .undef.
    df = pd.DataFrame({
        "ticker": ["A", "B", "C"],
        "name": ["Alpha", "Beta", "Gamma"],
        "subindustry": ["Infra"] * 3,
        "marketCap": [1e10, 2e10, 3e10],
        "evEbitda": [-5.0, -3.0, 12.0],
    }).set_index("ticker", drop=False)
    metrics = [("evEbitda", "EV/EBITDA", "val", "x", False)]
    html = render.render_sector(
        "TMT", df, {}, metrics, {"TMT": ["Infra"]}, {"val": "Valuation"},
        {"evEbitda": lambda d: pd.to_numeric(d["evEbitda"], errors="coerce") > 0},
    )

    def row(tk):
        chunk = html[html.index(f'data-tk="{tk}"'):]
        return chunk[:chunk.index("</tr>")]

    assert "undef" in row("A")
    assert "undef" in row("B")
    assert "undef" not in row("C")
    assert "data-ss" not in row("C")
