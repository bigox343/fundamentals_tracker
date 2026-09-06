import math

import pandas as pd
import pytest

import render


def out_v(entry):
    return entry["v"][0]


def test_encode_series_downsamples_to_weekly():
    idx = pd.date_range("2021-09-01", periods=1270, freq="B")
    frame = pd.DataFrame({"trailingPE": range(1270)}, index=idx, dtype=float)
    out = render.encode_series(frame, every=5)
    assert len(out["trailingPE"]["v"]) == pytest.approx(254, abs=2)
    assert out["trailingPE"]["t0"] == "2021-09-01"


def test_encode_series_rounds_to_four_significant_figures():
    # Not four decimal places: round(1.23456789, 4) is 1.2346, which is what
    # a naive port of the brief's own Step-3 snippet produces and which this
    # assertion is written to catch -- 1.235 is the four-significant-figure
    # answer.
    idx = pd.date_range("2021-09-01", periods=10, freq="B")
    frame = pd.DataFrame({"ps": [1.23456789] * 10}, index=idx)
    out = render.encode_series(frame, every=1)
    assert out_v(out["ps"]) == 1.235


def test_encode_series_keeps_gaps_as_null_not_zero():
    idx = pd.date_range("2021-09-01", periods=5, freq="B")
    frame = pd.DataFrame({"ps": [1.0, float("nan"), 3.0, 4.0, 5.0]}, index=idx)
    assert render.encode_series(frame, every=1)["ps"]["v"][1] is None


def test_encode_series_omits_a_column_that_is_entirely_gaps():
    # dropna().empty guards this -- an all-NaN column would otherwise embed a
    # t0 and a v list of nulls for a metric nobody can chart.
    idx = pd.date_range("2021-09-01", periods=5, freq="B")
    frame = pd.DataFrame({"ps": [float("nan")] * 5}, index=idx)
    assert "ps" not in render.encode_series(frame, every=1)


def test_the_drilldown_reports_the_percentile_already_on_the_cell_not_a_recomputed_one():
    # encode_series' own comment says percentiles are computed from the full
    # daily series server-side; own_percentile (valuation.py) is what
    # actually does that, from the un-downsampled frame. SERIES only ever
    # carries the downsampled weekly points for the picture. If drawDrill
    # re-derived a percentile from those weekly points (as the brief's Step 4
    # snippet did, filtering pts for below/valid), the tint painted on the
    # cell from data-oh and the number printed in the panel could disagree on
    # screen at the same moment. Pinning this at the JS-source level because
    # no JS runtime is available to execute drawDrill in this test suite.
    assert "data-oh" in render.JS_TMPL
    assert "below/valid" not in render.JS_TMPL
    assert "v<last" not in render.JS_TMPL
    assert "pts.filter" not in render.JS_TMPL


def test_a_clickable_valuation_cell_gets_a_pointer_cursor_and_nothing_else():
    # Exact string match, not a substring search for "cursor:pointer" alone --
    # this pins that the rule carries no other declaration that could nudge
    # cell width/height/padding, which the brief's no-new-columns constraint
    # forbids.
    assert "td.num.g-val[data-oh]{cursor:pointer}" in render.CSS


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


def test_every_frame_option_is_selectable_now_that_changes_exist():
    # Task 12 shipped c1w and c1m disabled with a "(soon)" suffix, because
    # data-c1w/data-c1m did not exist and selecting them would have painted an
    # entirely blank table -- indistinguishable from a bug. This is the task
    # that earns them, so the placeholder has to go with it: a control left
    # greyed out after its data arrives is worse than one that was never there.
    for live in ("peers", "own", "c1w", "c1m"):
        opt = render.TOOLBAR[render.TOOLBAR.index(f'value="{live}"'):]
        assert "disabled" not in opt[:opt.index(">")], live
        assert "(soon)" not in opt[:opt.index("</option>")], live


def test_a_cell_carries_both_the_raw_change_and_the_scaled_one():
    # Two attributes because they answer different questions: the raw value
    # drives the direction arrow, the scaled one drives the tint. Feeding the
    # raw log change to the tint would paint a large weekly move (0.09) at 9%
    # of full strength.
    html = render.render_sector(
        "TMT", _one_band(), {}, [("trailingPE", "Trail P/E", "val", "x", False)],
        {"TMT": ["Infra"]}, {"val": "Valuation"}, {},
        changes={("ORCL", "trailingPE", "c1w"): 0.045},
    )
    row = html[html.index('data-tk="ORCL"'):]
    row = row[:row.index("</tr>")]
    assert "data-c1w='0.04500'" in row
    # lower-is-better, so a RISING multiple is unfavorable: 0.045/0.09 = 0.5,
    # negated to -0.5
    assert "data-c1ws='-0.5000'" in row


def test_the_change_tint_is_signed_by_whether_higher_is_better():
    # The same +4.5% move on a higher-is-better metric must read favorable.
    html = render.render_sector(
        "TMT", _one_band(), {}, [("trailingPE", "Rev Gr", "grow", "x", True)],
        {"TMT": ["Infra"]}, {"val": "Valuation", "grow": "Growth"}, {},
        changes={("ORCL", "trailingPE", "c1w"): 0.045},
    )
    row = html[html.index('data-tk="ORCL"'):]
    assert "data-c1ws='0.5000'" in row[:row.index("</tr>")]


def test_a_percentage_metric_is_scaled_on_the_points_span_not_the_log_one():
    # 1.5 points against the 3.0-point span is half a tint. Scaled on the log
    # span (0.09) it would saturate more than sixteen times over.
    html = render.render_sector(
        "TMT", _one_band(), {}, [("trailingPE", "Net %", "prof", "pct", True)],
        {"TMT": ["Infra"]}, {"val": "Valuation", "prof": "Profitability"}, {},
        changes={("ORCL", "trailingPE", "c1w"): 1.5},
    )
    row = html[html.index('data-tk="ORCL"'):]
    assert "data-c1ws='0.5000'" in row[:row.index("</tr>")]


def test_a_cell_with_no_change_carries_no_change_attribute():
    # Absent must stay distinct from zero: "we have not watched long enough"
    # and "it did not move" are different statements.
    html = render.render_sector(
        "TMT", _one_band(), {}, [("trailingPE", "Trail P/E", "val", "x", False)],
        {"TMT": ["Infra"]}, {"val": "Valuation"}, {}, changes={},
    )
    assert "data-c1w" not in html


def test_the_change_frames_paint_from_the_scaled_attribute():
    # Pinned at the JS-source level: reading data-c1w instead of data-c1ws
    # would tint a large move at a tenth of its strength.
    assert "data-c1ws" in render.JS_TMPL
    assert "data-c1ms" in render.JS_TMPL


def test_the_direction_arrow_reads_the_raw_change_and_has_a_deadband():
    assert "function arrows()" in render.JS_TMPL
    assert "getAttribute('data-c1w')" in render.JS_TMPL
    assert "DEADBAND" in render.JS_TMPL
    assert "arrows();" in render.JS_TMPL, "defined but never called"


def test_the_arrow_style_cannot_change_a_cell_metric():
    # No-new-columns applies to height too. line-height:1 and a font smaller
    # than the row's own keep the span from growing the cell it sits in.
    rule = render.CSS[render.CSS.index(".arw{"):]
    rule = rule[:rule.index("}")]
    assert "line-height:1" in rule
    assert "font-size:8px" in rule
    # Split on ";" so a bare "height" declaration is caught without matching
    # the "height:" inside "line-height:", which is the one that makes it safe.
    props = {d.split(":")[0].strip() for d in rule.split("{")[1].split(";") if d}
    for forbidden in ("padding", "height", "width", "margin-top", "float"):
        assert forbidden not in props, forbidden
    assert props <= {"font-size", "line-height", "margin-left", "color",
                     "display", "vertical-align"}, props
