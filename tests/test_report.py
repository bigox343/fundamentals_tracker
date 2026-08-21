"""The 13F report derives its wording from the store, never hardcodes it."""
import importlib.util
from pathlib import Path

import pytest

import history
from history import FilingRow, ThirteenFRow

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_13f_report", ROOT / "tools" / "build_13f_report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

Q2, Q1 = "2026-06-30", "2026-03-31"


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    history.ensure_schema(c)
    return c


def _filed(conn, cik, quarter, fund=None, status="ok"):
    history.upsert_filings(conn, [FilingRow(
        cik, fund or f"Fund{cik}", "Tiger", quarter, "acc", "2026-08-14",
        5, 1, 1_000_000.0, status)])


def _pos(conn, cik, quarter, ticker, shares, value):
    history.upsert_thirteenf(conn, [ThirteenFRow(
        cik, f"Fund{cik}", quarter, f"CU{ticker}0010", ticker, ticker,
        "COM", shares, value)])


def _close(conn, ticker, as_of, value):
    history.upsert_rows(conn, [history.MetricRow(
        ticker, as_of, "daily", "close", "", value)])


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

@pytest.mark.parametrize("quarter,label,tag", [
    ("2026-03-31", "Q1 2026", "2026q1"),
    ("2026-06-30", "Q2 2026", "2026q2"),
    ("2026-09-30", "Q3 2026", "2026q3"),
    ("2026-12-31", "Q4 2026", "2026q4"),
])
def test_quarter_labels(quarter, label, tag):
    assert report.quarter_label(quarter) == label
    assert report.quarter_tag(quarter) == tag


def test_money_and_share_formatting():
    assert report.usd(15.5e9) == "$15.5B"
    assert report.usd(2.4e6) == "$2M"
    assert report.num(-1_234_567) == "-1.2M"
    assert report.num(999) == "999"


# --------------------------------------------------------------------------
# The split footnote
# --------------------------------------------------------------------------

def test_split_note_names_the_actual_split(conn):
    """Booking 25:1 between Q1 and Q2 2026."""
    _filed(conn, "1", Q1); _filed(conn, "1", Q2)
    _pos(conn, "1", Q1, "BKNG", 2_062, 2_062 * 5000.0)
    _pos(conn, "1", Q2, "BKNG", 51_550, 51_550 * 200.0)
    _close(conn, "BKNG", Q1, 200.0); _close(conn, "BKNG", Q2, 200.0)

    note = report.describe_splits(conn, Q2, Q1)
    assert "BKNG 25-for-1" in note
    assert "2,400%" in note


def test_split_note_says_so_when_nothing_split(conn):
    """The quarters where no name splits are the common case."""
    _filed(conn, "1", Q1); _filed(conn, "1", Q2)
    _pos(conn, "1", Q1, "AAA", 100, 20_000.0)
    _pos(conn, "1", Q2, "AAA", 120, 24_000.0)
    _close(conn, "AAA", Q1, 200.0); _close(conn, "AAA", Q2, 200.0)

    note = report.describe_splits(conn, Q2, Q1)
    assert "No name in the universe split" in note
    assert "for-1" not in note


def test_split_note_handles_a_reverse_split(conn):
    _filed(conn, "1", Q1); _filed(conn, "1", Q2)
    _pos(conn, "1", Q1, "AAA", 4_000, 4_000 * 50.0)
    _pos(conn, "1", Q2, "AAA", 1_000, 1_000 * 200.0)
    _close(conn, "AAA", Q1, 200.0); _close(conn, "AAA", Q2, 200.0)

    assert "AAA 1-for-4" in report.describe_splits(conn, Q2, Q1)


# --------------------------------------------------------------------------
# The non-filer footnote
# --------------------------------------------------------------------------

def test_absent_note_names_the_manager_that_did_not_file(conn):
    _filed(conn, "1", Q1, fund="Pershing Square")
    _filed(conn, "1", Q2, fund="Pershing Square", status="no-filing")
    _pos(conn, "1", Q1, "AAA", 100, 100)
    _pos(conn, "1", Q1, "BBB", 200, 200)

    note = report.describe_absent(conn, Q2, Q1, n_exit=147)
    assert "Pershing Square" in note
    assert "2 prior positions" in note
    assert "147 exits are real" in note


def test_absent_note_says_so_when_everyone_filed(conn):
    _filed(conn, "1", Q2)
    note = report.describe_absent(conn, Q2, Q1, n_exit=12)
    assert "Every manager filed for Q2 2026" in note
    assert "all 12 exits are real" in note


def test_absent_note_uses_the_quarter_it_was_given(conn):
    """The wording must follow the quarter, not a value baked in at authoring."""
    _filed(conn, "9", "2026-09-30", fund="Someone", status="no-filing")
    note = report.describe_absent(conn, "2026-09-30", "2026-06-30", n_exit=3)
    assert "Q3 2026" in note and "Q2 2026" not in note


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

def test_report_renders_for_the_quarter_asked_for(conn, tmp_path):
    conn.execute("INSERT INTO companies (ticker, name) VALUES ('AAA','Alpha Inc')")
    for q in (Q1, Q2):
        _filed(conn, "1", q)
        _pos(conn, "1", q, "AAA", 100, 20_000.0)
    out = report.build(conn, Q2, tmp_path)
    page = out.read_text()

    assert out.name == "13f_2026q2.html"
    assert "Q2 2026 13F Positioning" in page
    assert "{{" not in page                      # every placeholder filled
    assert "14 Aug 2026" in page                 # deadline derived, not written
    for tag in ("<!doctype", "<html", "<body"):  # safe to publish as an artifact
        assert tag not in page.lower()


def test_report_refuses_a_quarter_it_has_not_ingested(conn, tmp_path):
    _filed(conn, "1", Q2)
    _pos(conn, "1", Q2, "AAA", 100, 20_000.0)
    with pytest.raises(SystemExit):
        report.build(conn, "2025-06-30", tmp_path)
