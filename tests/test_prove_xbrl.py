import importlib.util
import math
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import history
import valuation

_spec = importlib.util.spec_from_file_location(
    "prove_xbrl", Path(__file__).parent.parent / "tools" / "prove_xbrl.py")
prove = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prove)


def test_an_exact_reconstruction_passes():
    s = pd.Series([10.0, 11.0, 12.0], index=["a", "b", "c"])
    err, ok = prove.proof(s, s)
    assert err == pytest.approx(0.0)
    assert ok


def test_a_reconstruction_inside_tolerance_passes():
    ref = pd.Series([100.0, 100.0, 100.0])
    err, ok = prove.proof(pd.Series([100.5, 99.6, 100.4]), ref)
    assert err < prove.PASS_TOLERANCE
    assert ok


def test_one_bad_day_does_not_reject_a_good_mapping():
    # The median, not the mean: a single source glitch must not fail a ticker.
    ref = pd.Series([100.0] * 9 + [100.0])
    recon = pd.Series([100.0] * 9 + [4000.0])
    err, ok = prove.proof(recon, ref)
    # The outlier must not even dent the reported error -- nine of ten days
    # sit at the median, so the 40x glitch on the tenth is invisible to it.
    assert err == pytest.approx(0.0)
    assert ok, "nine exact days and one outlier should still pass"


def test_a_wrong_tag_is_rejected():
    ref = pd.Series([100.0, 100.0, 100.0])
    err, ok = prove.proof(pd.Series([250.0, 249.0, 251.0]), ref)
    assert err > prove.PASS_TOLERANCE
    assert not ok


def test_too_few_overlapping_days_cannot_pass():
    err, ok = prove.proof(pd.Series([100.0]), pd.Series([100.0]))
    assert not ok, "one day is not evidence"


def test_no_overlap_at_all_is_not_a_pass():
    err, ok = prove.proof(pd.Series(dtype=float), pd.Series(dtype=float))
    assert not ok


def test_disjoint_indices_leave_nothing_to_compare():
    # Same shape as the empty-series case above, but via non-empty series
    # whose calendars never intersect -- the realistic version of "no
    # overlap": a reconstruction computed on different snapshot days than
    # the reference it is meant to be checked against.
    recon = pd.Series([100.0, 101.0], index=["2026-01-01", "2026-01-02"])
    reference = pd.Series([100.0, 101.0], index=["2026-02-01", "2026-02-02"])
    err, ok = prove.proof(recon, reference)
    # No overlap means no median was ever computed, not a median of nothing
    # that happens to read as falsy -- the error must come back as nan.
    assert math.isnan(err)
    assert not ok


def test_a_zero_reference_value_does_not_raise_and_is_excluded():
    # A zero in the reference would make (r - y) / y divide by zero. It must
    # be dropped rather than raise or poison the median with inf/NaN -- the
    # remaining two exact days are still enough evidence to pass.
    ref = pd.Series([0.0, 100.0, 100.0])
    recon = pd.Series([999.0, 100.0, 100.0])
    err, ok = prove.proof(recon, ref)
    assert err == pytest.approx(0.0)
    assert ok


def test_all_zero_reference_values_cannot_pass():
    # Once every reference value is excluded there is nothing left to prove
    # anything with, regardless of how well recon matches the (invalid) zeros.
    ref = pd.Series([0.0, 0.0, 0.0])
    recon = pd.Series([0.0, 0.0, 0.0])
    err, ok = prove.proof(recon, ref)
    # Same reasoning as the no-overlap case: the floor is hit before any
    # median is computed, so the error must be nan, not a numeric fluke.
    assert math.isnan(err)
    assert not ok


def test_report_marks_a_failing_pair_as_not_passed():
    frame = prove.summarize({
        ("AAPL", "trailingPE"): (0.002, True),
        ("NET", "evEbitda"): (0.51, False),
    })
    assert set(frame.columns) == {"ticker", "metric", "median_error", "passed"}
    assert not frame.set_index(["ticker", "metric"]).loc[
        ("NET", "evEbitda"), "passed"]


def _store_with_two_snapshots():
    # MIN_OVERLAP is 2: a single overlapping day is not evidence (see
    # test_too_few_overlapping_days_cannot_pass above), so a fixture with only
    # one snapshot date would fail the proof for a reason unrelated to what
    # these two tests are checking. Two dates keep the fixture honest.
    conn = sqlite3.connect(":memory:")
    history.ensure_schema(conn)
    conn.execute(
        "INSERT INTO metrics VALUES "
        "('AAPL','2026-01-02','snapshot','trailingPE','',30.0,'x')"
    )
    conn.execute(
        "INSERT INTO metrics VALUES "
        "('AAPL','2026-01-05','snapshot','trailingPE','',31.0,'x')"
    )
    conn.commit()
    return conn


def _built_frame():
    return {"AAPL": pd.DataFrame(
        {"trailingPE": [30.0, 31.0]},
        index=pd.DatetimeIndex(["2026-01-02", "2026-01-05"]))}


def test_report_uses_a_prebuilt_series_instead_of_running_build_all(monkeypatch):
    # own_history in build_dashboard.py already ran build_all() once (it is
    # ~11s over the full universe) to compute the own-history percentile;
    # report() must reuse that result rather than paying for it again.
    conn = _store_with_two_snapshots()

    def _boom(*_a, **_k):
        raise AssertionError("build_all must not run when `built` is supplied")

    monkeypatch.setattr(valuation, "build_all", _boom)
    out = prove.report(conn, built=_built_frame())
    assert out.set_index(["ticker", "metric"]).loc[
        ("AAPL", "trailingPE"), "passed"]


def test_report_builds_its_own_series_when_none_is_supplied(monkeypatch):
    # The standalone call site (running prove_xbrl.py on its own) has no
    # prebuilt series to hand in, so report() must still compute one itself.
    conn = _store_with_two_snapshots()
    calls = []

    def _fake_build_all(_conn, tickers):
        calls.append(list(tickers))
        return _built_frame()

    monkeypatch.setattr(valuation, "build_all", _fake_build_all)
    out = prove.report(conn)
    assert calls == [["AAPL"]], "build_all must run exactly once when built=None"
    assert out.set_index(["ticker", "metric"]).loc[
        ("AAPL", "trailingPE"), "passed"]
