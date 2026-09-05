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
