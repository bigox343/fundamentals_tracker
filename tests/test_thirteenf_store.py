"""13F store behaviour: change detection, exits, and split adjustment."""
import pytest

import history
from history import FilingRow, ThirteenFRow

Q2, Q1 = "2026-06-30", "2026-03-31"


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    history.ensure_schema(c)
    return c


def _pos(cik, quarter, ticker, shares, value, cusip=None):
    return ThirteenFRow(cik, f"Fund{cik}", quarter, cusip or f"CU{ticker}0010",
                        ticker, ticker, "COM", shares, value)


def _filed(conn, cik, quarter, book=1_000_000.0, status="ok"):
    history.upsert_filings(conn, [FilingRow(
        cik, f"Fund{cik}", "Tiger", quarter, f"acc-{cik}-{quarter}",
        "2026-08-14", 10, 1, book, status)])


def _close(conn, ticker, as_of, value):
    history.upsert_rows(conn, [history.MetricRow(
        ticker, as_of, "daily", "close", "", value)])


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def test_new_add_trim_and_hold(conn):
    for cik, prev, cur in [("1", None, 100), ("2", 100, 200),
                           ("3", 200, 100), ("4", 100, 100)]:
        _filed(conn, cik, Q2)
        _filed(conn, cik, Q1)
        if prev is not None:
            history.upsert_thirteenf(conn, [_pos(cik, Q1, "AAA", prev, prev)])
        history.upsert_thirteenf(conn, [_pos(cik, Q2, "AAA", cur, cur)])

    got = history.thirteenf_changes(conn, Q2, Q1).set_index("cik")["action"]
    assert got["1"] == "NEW"
    assert got["2"] == "ADD"
    assert got["3"] == "TRIM"
    assert got["4"] == "HOLD"


def test_small_drift_is_hold_not_a_decision(conn):
    """A 0.4% move in share count is not a position change worth badging."""
    _filed(conn, "1", Q2); _filed(conn, "1", Q1)
    history.upsert_thirteenf(conn, [_pos("1", Q1, "AAA", 100_000, 100)])
    history.upsert_thirteenf(conn, [_pos("1", Q2, "AAA", 100_400, 100)])
    assert history.thirteenf_changes(conn, Q2, Q1).iloc[0]["action"] == "HOLD"


# --------------------------------------------------------------------------
# The exit-versus-gap distinction
# --------------------------------------------------------------------------

def test_a_manager_that_filed_and_dropped_the_name_shows_an_exit(conn):
    _filed(conn, "1", Q1)
    _filed(conn, "1", Q2)                      # filed, but holds nothing now
    history.upsert_thirteenf(conn, [_pos("1", Q1, "AAA", 500, 500)])

    changes = history.thirteenf_changes(conn, Q2, Q1)
    assert list(changes["action"]) == ["EXIT"]
    assert changes.iloc[0]["shares"] == 0


def test_a_manager_with_no_filing_shows_nothing_rather_than_a_false_exit(conn):
    """The single most important guard in this dataset.

    An exit is encoded as an absent row, so a fetch that never happened is
    indistinguishable from a liquidation unless the filing is recorded
    separately. Saying nothing is correct; inventing an exit is not.
    """
    _filed(conn, "1", Q1)                      # Q2 deliberately not recorded
    history.upsert_thirteenf(conn, [_pos("1", Q1, "AAA", 500, 500)])

    assert len(history.thirteenf_changes(conn, Q2, Q1)) == 0


def test_a_failed_filing_does_not_count_as_filed(conn):
    _filed(conn, "1", Q1)
    _filed(conn, "1", Q2, status="failed")
    history.upsert_thirteenf(conn, [_pos("1", Q1, "AAA", 500, 500)])

    assert len(history.thirteenf_changes(conn, Q2, Q1)) == 0


# --------------------------------------------------------------------------
# Splits
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ratio,expected", [
    (1.0, 1.0), (1.055, 1.0), (0.95, 1.0),      # dividend-adjustment drift
    (10.0, 10.0), (25.06, 25.0), (4.0, 4.0),    # real splits
    (0.25, 0.25),                               # a reverse split
    (1.33, 1.0), (3.7, 1.0),                    # unexplained: leave alone
])
def test_snap_split(ratio, expected):
    assert history._snap_split(ratio) == expected


def test_a_split_does_not_read_as_the_manager_adding(conn):
    """Booking split 25:1 between Q1 and Q2 2026.

    The filings report 2,062 then 51,550 shares for an untouched position.
    Differenced raw, that is +2,400%.
    """
    _filed(conn, "1", Q1); _filed(conn, "1", Q2)
    history.upsert_thirteenf(conn, [_pos("1", Q1, "BKNG", 2_062, 2_062 * 5000.0)])
    history.upsert_thirteenf(conn, [_pos("1", Q2, "BKNG", 51_550, 51_550 * 200.0)])
    _close(conn, "BKNG", Q1, 200.0)     # back-adjusted, so post-split basis
    _close(conn, "BKNG", Q2, 200.0)

    row = history.thirteenf_changes(conn, Q2, Q1).iloc[0]
    assert row["prev_shares"] == pytest.approx(51_550)
    assert row["delta_shares"] == pytest.approx(0)
    assert row["action"] == "HOLD"


def test_a_dividend_payers_drift_is_not_mistaken_for_a_split(conn):
    """IBM's implied/close ratio runs to 1.055 two years back. Not a split."""
    _filed(conn, "1", Q1); _filed(conn, "1", Q2)
    history.upsert_thirteenf(conn, [_pos("1", Q1, "IBM", 1_000, 1_000 * 211.0)])
    history.upsert_thirteenf(conn, [_pos("1", Q2, "IBM", 2_000, 2_000 * 200.0)])
    _close(conn, "IBM", Q1, 200.0)
    _close(conn, "IBM", Q2, 200.0)

    row = history.thirteenf_changes(conn, Q2, Q1).iloc[0]
    assert row["prev_shares"] == pytest.approx(1_000)   # untouched
    assert row["action"] == "ADD"


# --------------------------------------------------------------------------
# Aggregation and bookkeeping
# --------------------------------------------------------------------------

def test_share_classes_are_summed_at_the_query_boundary(conn):
    """Stored separately -- Alphabet A and C are different CUSIPs -- but the
    reader wants one Alphabet line."""
    _filed(conn, "1", Q2)
    history.upsert_thirteenf(conn, [
        _pos("1", Q2, "GOOGL", 100, 1000, cusip="02079K305"),
        _pos("1", Q2, "GOOGL", 50, 500, cusip="02079K107")])

    changes = history.thirteenf_changes(conn, Q2, None)
    assert len(changes) == 1
    assert changes.iloc[0]["shares"] == 150


def test_pct_of_book_uses_the_whole_filing_not_the_universe_slice(conn):
    """The column that separates conviction from market-making flow."""
    _filed(conn, "1", Q2, book=1_000_000.0)
    history.upsert_thirteenf(conn, [_pos("1", Q2, "AAA", 10, 50_000.0)])
    row = history.thirteenf_changes(conn, Q2, None).iloc[0]
    assert row["pct_of_book"] == pytest.approx(0.05)


def test_replace_quarter_clears_a_restated_filing(conn):
    """An amendment restates the whole table; upserting would leave orphans."""
    _filed(conn, "1", Q2)
    history.upsert_thirteenf(conn, [_pos("1", Q2, "AAA", 10, 10),
                                    _pos("1", Q2, "BBB", 20, 20)])
    history.replace_quarter(conn, "1", Q2)
    history.upsert_thirteenf(conn, [_pos("1", Q2, "AAA", 10, 10)])

    tickers = set(history.thirteenf_changes(conn, Q2, None)["ticker"])
    assert tickers == {"AAA"}


def test_ingested_filings_reports_accessions_for_skipping(conn):
    _filed(conn, "1", Q2)
    assert history.ingested_filings(conn) == {("1", Q2): f"acc-1-{Q2}"}


def test_a_failed_filing_is_not_reported_as_ingested(conn):
    _filed(conn, "1", Q2, status="failed")
    assert history.ingested_filings(conn) == {}


# --------------------------------------------------------------------------
# Reaching a quiet steady state
# --------------------------------------------------------------------------

def test_a_filing_that_does_not_exist_is_recorded_so_it_stops_being_missing(conn):
    """Viking Global filed nothing for Q1 2026, Pershing Square nothing for Q2.

    Counted as missing, they would force a sweep of EDGAR on every run forever,
    because the absent filing never arrives.
    """
    history.upsert_filings(conn, [FilingRow(
        "1", "Fund1", "Tiger", Q2, "", "", 0, 0, 0.0, "no-filing")])

    assert history.ingested_filings(conn) == {}       # not a success
    assert ("1", Q2) in history.seen_filings(conn)    # but it is settled


def test_seen_filings_covers_every_outcome(conn):
    _filed(conn, "1", Q2)
    history.upsert_filings(conn, [
        FilingRow("2", "F2", "Tiger", Q2, "", "", 0, 0, 0.0, "no-filing"),
        FilingRow("3", "F3", "Tiger", Q2, "a", "", 0, 0, 0.0, "failed")])
    assert history.seen_filings(conn) == {("1", Q2), ("2", Q2), ("3", Q2)}


def test_last_sweep_reports_when_edgar_was_asked(conn):
    assert history.last_sweep(conn) is None
    _filed(conn, "1", Q2)
    assert history.last_sweep(conn) is not None
