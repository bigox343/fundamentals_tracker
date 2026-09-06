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


def _one_band(**extra):
    # marketCap is required by render_sector's per-band sort (sort_values on
    # it); the brief's original fixture omitted it, which fails with a bare
    # KeyError rather than exercising anything about own-history -- exactly
    # the hollow-fixture trap flagged for this task.
    df = pd.DataFrame({
        "ticker": ["ORCL", "MSFT", "FTNT"],
        "name": ["Oracle", "Microsoft", "Fortinet"],
        "subindustry": ["Infra"] * 3,
        "marketCap": [3e11, 3e12, 6e10],
        "trailingPE": [19.18, 19.77, 42.84],
    }).set_index("ticker", drop=False)
    return df


def test_a_cell_carries_its_own_history_percentile():
    html = render.render_sector(
        "TMT", _one_band(), {}, [("trailingPE", "Trail P/E", "val", "x", False)],
        {"TMT": ["Infra"]}, {"val": "Valuation"}, {},
        own={("ORCL", "trailingPE"): 0.12},
    )
    row = html[html.index('data-tk="ORCL"'):]
    assert "data-oh='0.1200'" in row[:row.index("</tr>")]


def test_a_pair_that_failed_the_proof_carries_no_own_history():
    html = render.render_sector(
        "TMT", _one_band(), {}, [("trailingPE", "Trail P/E", "val", "x", False)],
        {"TMT": ["Infra"]}, {"val": "Valuation"}, {}, own={},
    )
    assert "data-oh" not in html


def test_the_toolbar_offers_every_frame():
    for value in ("peers", "own", "c1w", "c1m"):
        assert f'value="{value}"' in render.TOOLBAR


def test_the_change_frame_options_are_disabled_until_task_14():
    # data-c1w/data-c1m do not exist yet, so painting the table from them
    # would render every cell blank -- indistinguishable from a bug. The
    # options stay in the markup (the test above still proves every frame is
    # offered) but disabled, with a "(soon)" suffix, so the gap reads as
    # deliberate.
    soon = render.TOOLBAR[render.TOOLBAR.index('value="c1w"'):]
    assert "disabled" in soon[:soon.index(">")]
    assert "(soon)" in soon[:soon.index("</option>")]
    soon = render.TOOLBAR[render.TOOLBAR.index('value="c1m"'):]
    assert "disabled" in soon[:soon.index(">")]
    assert "(soon)" in soon[:soon.index("</option>")]
    for live in ("peers", "own"):
        opt = render.TOOLBAR[render.TOOLBAR.index(f'value="{live}"'):]
        assert "disabled" not in opt[:opt.index(">")]
