import importlib.util
from pathlib import Path

import pandas as pd
import pytest

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
    assert not ok
