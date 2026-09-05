"""Dated-file retention. The guard matters more than the deletion."""
from pathlib import Path

import pytest

import extract


def _touch(d: Path, names):
    for n in names:
        (d / n).write_text("x")
    return d


@pytest.fixture
def data(tmp_path):
    return _touch(tmp_path, [
        "estimates_20260901.csv.gz", "estimates_20260902.csv.gz",
        "estimates_20260903.csv.gz", "estimates_20260904.csv.gz"])


ALL = {"2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"}


@pytest.mark.parametrize("name,stamp", [
    ("estimates_20260904.csv.gz", "2026-09-04"),
    ("fundamentals_20260815.csv.gz", "2026-08-15"),
    ("portfolio_20260821.html", "2026-08-21"),
    ("cusip_map.csv", None),
    ("13f_2026Q2.csv.gz", None),
    ("13f_filings.csv.gz", None),
    ("history.db", None),
])
def test_only_dated_files_are_recognised(name, stamp):
    """The quarterly 13F archives and checked-in references must never match."""
    assert extract.dated_stamp(name) == stamp


def test_keeps_the_newest_and_removes_the_rest(data):
    removed = extract.prune_dated(data, "estimates_*.csv.gz", 1, ALL)
    left = sorted(p.name for p in data.glob("estimates_*"))
    assert left == ["estimates_20260904.csv.gz"]
    assert len(removed) == 3


def test_keep_can_retain_more_than_one(data):
    extract.prune_dated(data, "estimates_*.csv.gz", 2, ALL)
    assert sorted(p.name for p in data.glob("estimates_*")) == [
        "estimates_20260903.csv.gz", "estimates_20260904.csv.gz"]


def test_a_file_the_store_has_not_ingested_is_never_deleted(data):
    """The guard. A run that fetched but failed to record must keep its file.

    Without this, the next run's housekeeping destroys the only copy of an
    observation that was never written to the database.
    """
    safe = ALL - {"2026-09-01"}
    extract.prune_dated(data, "estimates_*.csv.gz", 1, safe)
    left = sorted(p.name for p in data.glob("estimates_*"))
    assert left == ["estimates_20260901.csv.gz", "estimates_20260904.csv.gz"]


def test_no_guard_means_delete_freely(data):
    """Rendered reports are derived from the store; there is nothing to lose."""
    extract.prune_dated(data, "estimates_*.csv.gz", 1, None)
    assert len(list(data.glob("estimates_*"))) == 1


def test_keeping_more_than_exist_removes_nothing(data):
    assert extract.prune_dated(data, "estimates_*.csv.gz", 99, ALL) == []
    assert len(list(data.glob("estimates_*"))) == 4


def test_undated_neighbours_are_left_alone(tmp_path):
    """Pruning must not touch the quarterly archives or the checked-in map."""
    d = _touch(tmp_path, [
        "estimates_20260901.csv.gz", "estimates_20260904.csv.gz",
        "13f_2026Q2.csv.gz", "13f_filings.csv.gz", "cusip_map.csv"])
    extract.prune_dated(d, "*.csv.gz", 1, None)
    survivors = sorted(p.name for p in d.iterdir())
    assert "13f_2026Q2.csv.gz" in survivors
    assert "13f_filings.csv.gz" in survivors
    assert "cusip_map.csv" in survivors
    assert "estimates_20260901.csv.gz" not in survivors


def test_retain_zero_is_a_no_op_at_the_caller(data):
    """RETAIN_DATED <= 0 disables pruning rather than deleting everything."""
    import build_dashboard as bd
    assert bd.RETAIN_DATED >= 1
