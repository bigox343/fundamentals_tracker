import pytest

import history
import portfolio


def _thirteenf_store(tmp_path):
    """A store with one 13F quarter, filed 45 days after the period end."""
    conn = history.connect(tmp_path / "h.db")
    history.ensure_schema(conn)
    conn.execute(
        "INSERT INTO thirteenf_filings (cik, fund, cohort, quarter, accession,"
        " filed_date, n_positions, n_universe, book_value, status, ingested_at)"
        " VALUES ('1','F','c','2025-06-30','a','2025-08-14',1,1,1.0,'ok','x')")
    conn.commit()
    return conn


def test_a_13f_quarter_is_invisible_until_it_has_been_filed(tmp_path):
    # The quarter ends 2025-06-30 but the filing lands 2025-08-14. A backtest
    # rebalancing on 2025-07-31 must not see it; one rebalancing in September
    # must. Selecting on the period end gives the strategy six weeks of
    # information nobody had, on one of three equally weighted legs -- which
    # inflates the reported information ratio rather than raising an error.
    conn = _thirteenf_store(tmp_path)
    assert history.thirteenf_quarters(conn, known_by="2025-07-31") == []
    assert history.thirteenf_quarters(conn, known_by="2025-08-14") == \
        ["2025-06-30"]
    assert history.thirteenf_quarters(conn, known_by="2025-09-30") == \
        ["2025-06-30"]


def test_thirteenf_quarters_without_a_date_still_returns_everything(tmp_path):
    # Live callers pass nothing: a quarter only reaches the store after it was
    # filed, so there is nothing to gate and gating would be a silent change.
    conn = _thirteenf_store(tmp_path)
    assert history.thirteenf_quarters(conn) == ["2025-06-30"]


def _two_quarter_store(tmp_path):
    """Two filed quarters plus positions, so thirteenf_signal has a delta."""
    conn = history.connect(tmp_path / "sig.db")
    history.ensure_schema(conn)
    for q, filed in (("2025-03-31", "2025-05-15"), ("2025-06-30", "2025-08-14")):
        conn.execute(
            "INSERT INTO thirteenf_filings (cik, fund, cohort, quarter,"
            " accession, filed_date, n_positions, n_universe, book_value,"
            " status, ingested_at) VALUES ('1','F','c',?,?,?,1,1,1.0,'ok','x')",
            (q, "acc" + q, filed))
    for q, shares in (("2025-03-31", 100.0), ("2025-06-30", 200.0)):
        conn.execute(
            "INSERT INTO thirteenf (cik, fund, quarter, cusip, ticker,"
            " shares, value, ingested_at) VALUES"
            " ('1','F',?,'cusip','AAPL',?,1.0,'x')", (q, shares))
    conn.commit()
    return conn


def test_the_13f_signal_itself_refuses_a_quarter_that_was_not_yet_filed(tmp_path):
    # Gating inside history.thirteenf_quarters is only half the fix: the
    # backtest leg has to actually pass its as_of through. Rebalancing on
    # 2025-07-31, the June quarter exists in the store but was not filed until
    # 2025-08-14, so the signal must be empty rather than reporting a doubled
    # position nobody could have seen.
    conn = _two_quarter_store(tmp_path)
    early = portfolio.thirteenf_signal(conn, "2025-07-31")
    assert early.empty, f"six weeks of lookahead: {early.to_dict()}"
    later = portfolio.thirteenf_signal(conn, "2025-09-30")
    assert not later.empty, "once filed, the same quarter must be usable"
    assert later.get("AAPL") == pytest.approx(1.0), "100 -> 200 shares"
