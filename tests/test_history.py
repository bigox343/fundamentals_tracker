import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import extract
import history


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    history.ensure_schema(c)
    yield c
    c.close()


def _tables(c):
    rows = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _count(c):
    return c.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]


# --------------------------------------------------------------------------- #
# schema and contract                                                          #
# --------------------------------------------------------------------------- #

def test_ensure_schema_creates_tables(conn):
    assert {"metrics", "companies", "runs"} <= _tables(conn)


def test_ensure_schema_is_idempotent(conn):
    history.ensure_schema(conn)  # second call must not raise
    assert {"metrics", "companies", "runs"} <= _tables(conn)


def test_metric_row_field_order():
    row = history.MetricRow("NVDA", "2026-08-15", "snapshot", "roe", "", 114.3)
    assert row.ticker == "NVDA"
    assert row.as_of == "2026-08-15"
    assert row.period_type == "snapshot"
    assert row.metric == "roe"
    assert row.ref_period == ""
    assert row.value == 114.3


@pytest.mark.parametrize(
    "value,expected",
    [(1.0, True), (0, True), (-2.5, True), (float("nan"), False),
     (float("inf"), False), (None, False), ("abc", False)],
)
def test_is_finite(value, expected):
    assert history.is_finite(value) is expected


# --------------------------------------------------------------------------- #
# upsert                                                                       #
# --------------------------------------------------------------------------- #

def test_upsert_writes_rows(conn):
    rows = [
        history.MetricRow("NVDA", "2026-08-15", "snapshot", "roe", "", 114.3),
        history.MetricRow("AVGO", "2026-08-15", "snapshot", "roe", "", 37.3),
    ]
    assert history.upsert_rows(conn, rows) == 2
    assert _count(conn) == 2


def test_upsert_is_idempotent(conn):
    row = history.MetricRow("NVDA", "2026-08-15", "snapshot", "roe", "", 114.3)
    history.upsert_rows(conn, [row])
    history.upsert_rows(conn, [row])
    assert _count(conn) == 1


def test_upsert_updates_value_in_place(conn):
    key = ("NVDA", "2026-08-15", "snapshot", "roe", "")
    history.upsert_rows(conn, [history.MetricRow(*key, 114.3)])
    history.upsert_rows(conn, [history.MetricRow(*key, 120.0)])
    assert _count(conn) == 1
    value = conn.execute("SELECT value FROM metrics").fetchone()[0]
    assert value == 120.0


def test_annual_and_quarter_coexist_on_same_date(conn):
    """NVDA's fiscal year and its Q4 both end 2026-01-31 with different values.

    COST is a second live instance: its FY2026 and its Q4 both end 2026-08-31.
    Without period_type in the primary key one silently overwrites the other.
    """
    rows = [
        history.MetricRow("NVDA", "2026-01-31", "annual", "revenue", "", 130_000.0),
        history.MetricRow("NVDA", "2026-01-31", "quarter", "revenue", "", 39_000.0),
    ]
    history.upsert_rows(conn, rows)
    assert _count(conn) == 2


def test_two_ref_periods_coexist_on_same_as_of(conn):
    """0y and +1y consensus observed the same day are different series."""
    rows = [
        history.MetricRow("NVDA", "2026-08-15", "estimate", "epsEst",
                          "2027-01-31", 8.95773),
        history.MetricRow("NVDA", "2026-08-15", "estimate", "epsEst",
                          "2028-01-31", 12.79836),
    ]
    history.upsert_rows(conn, rows)
    assert _count(conn) == 2


def test_upsert_drops_non_finite_values(conn):
    rows = [
        history.MetricRow("NVDA", "2026-08-15", "snapshot", "roe", "", float("nan")),
        history.MetricRow("NVDA", "2026-08-15", "snapshot", "ps", "", float("inf")),
        history.MetricRow("NVDA", "2026-08-15", "snapshot", "fcf", "", 46_335.0),
    ]
    assert history.upsert_rows(conn, rows) == 1
    assert _count(conn) == 1


def test_upsert_of_empty_iterable_is_noop(conn):
    assert history.upsert_rows(conn, []) == 0
    assert _count(conn) == 0


# --------------------------------------------------------------------------- #
# snapshot and company ingest                                                  #
# --------------------------------------------------------------------------- #

@pytest.fixture
def snapshot_df():
    """Two rows shaped exactly like data/fundamentals_YYYYMMDD.csv."""
    return pd.DataFrame([
        {"ticker": "NVDA", "name": "NVIDIA Corporation", "price": 225.16,
         "marketCap": 5.453600260096e12, "forwardPE": 17.59288,
         "trailingPE": 34.480858, "evEbitda": 32.677, "ps": 21.513979,
         "fcfYield": 0.8496382355531197, "revGrowth": 85.2, "epsGrowth": 214.5,
         "grossMargin": 74.145, "opMargin": 65.596, "netMargin": 62.966,
         "roe": 114.288, "netDebtEbitda": -0.2438343463332097,
         "fcf": 46335873024.0, "cash": 53171998720.0,
         "sector": "TMT (Tech · Media · Telecom)", "subindustry": "Semiconductors",
         "spark": "182.6;187.7;192.6", "ret1m": 5.9576487821691115,
         "ret6m": 23.31634804557765, "retYtd": 19.372316899133658},
        {"ticker": "AVGO", "name": "Broadcom Inc.", "price": 392.99,
         "marketCap": 1.869681393664e12, "forwardPE": 20.121695,
         "trailingPE": 65.38934, "evEbitda": 45.503, "ps": 24.775478,
         "fcfYield": 1.455448461979523, "revGrowth": 47.9, "epsGrowth": 85.4,
         "grossMargin": 76.284, "opMargin": 48.988, "netMargin": 38.848,
         "roe": 37.280998, "netDebtEbitda": 1.0759196582890276,
         "fcf": 27212249088.0, "cash": 19627999232.0,
         "sector": "TMT (Tech · Media · Telecom)", "subindustry": "Semiconductors",
         "spark": "324;332.8;324.3", "ret1m": -0.32718082299781903,
         "ret6m": 21.30197017444788, "retYtd": 13.468061737570824},
    ])


def test_numeric_metrics_has_nineteen_names():
    assert len(history.NUMERIC_METRICS) == 19
    assert len(set(history.NUMERIC_METRICS)) == 19


def test_numeric_metrics_excludes_non_numeric_columns():
    for name in ("ticker", "name", "sector", "subindustry", "spark"):
        assert name not in history.NUMERIC_METRICS


def test_snapshot_rows_covers_every_metric(snapshot_df):
    rows = history.snapshot_rows(snapshot_df, "2026-08-15")
    assert len(rows) == 2 * 19
    assert {r.ticker for r in rows} == {"NVDA", "AVGO"}
    assert {r.period_type for r in rows} == {"snapshot"}
    assert {r.ref_period for r in rows} == {""}
    assert {r.as_of for r in rows} == {"2026-08-15"}


def test_snapshot_rows_preserves_negative_values(snapshot_df):
    """Negative net debt is legitimate and must survive."""
    rows = history.snapshot_rows(snapshot_df, "2026-08-15")
    nd = [r for r in rows if r.ticker == "NVDA" and r.metric == "netDebtEbitda"]
    assert len(nd) == 1
    assert nd[0].value < 0


def test_snapshot_rows_skips_missing_values(snapshot_df):
    snapshot_df.loc[0, "forwardPE"] = float("nan")
    rows = history.snapshot_rows(snapshot_df, "2026-08-15")
    assert not [r for r in rows
                if r.ticker == "NVDA" and r.metric == "forwardPE"]
    assert len(rows) == 2 * 19 - 1


def test_ingest_snapshot_persists(conn, snapshot_df):
    assert history.ingest_snapshot(conn, snapshot_df, "2026-08-15") == 38
    assert _count(conn) == 38


def test_upsert_companies_writes_attributes(conn, snapshot_df):
    assert history.upsert_companies(conn, snapshot_df) == 2
    row = conn.execute(
        "SELECT name, sector, subindustry FROM companies WHERE ticker='NVDA'"
    ).fetchone()
    assert row == ("NVIDIA Corporation", "TMT (Tech · Media · Telecom)",
                   "Semiconductors")


def test_upsert_companies_updates_on_resector(conn, snapshot_df):
    history.upsert_companies(conn, snapshot_df)
    snapshot_df.loc[0, "subindustry"] = "Semicap Equipment"
    history.upsert_companies(conn, snapshot_df)
    count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    assert count == 2
    sub = conn.execute(
        "SELECT subindustry FROM companies WHERE ticker='NVDA'"
    ).fetchone()[0]
    assert sub == "Semicap Equipment"


# --------------------------------------------------------------------------- #
# prices                                                                       #
# --------------------------------------------------------------------------- #

@pytest.fixture
def closes():
    idx = pd.to_datetime(["2026-08-13", "2026-08-14", "2026-08-15"])
    return pd.DataFrame(
        {"NVDA": [224.0, 224.1, 225.16], "AVGO": [418.2, 427.8, 392.99]},
        index=idx,
    )


def test_price_rows_are_daily_closes(closes):
    rows = history.price_rows(closes)
    assert len(rows) == 6
    assert {r.period_type for r in rows} == {"daily"}
    assert {r.metric for r in rows} == {"close"}
    assert {r.ref_period for r in rows} == {""}
    assert {r.as_of for r in rows} == {"2026-08-13", "2026-08-14", "2026-08-15"}


def test_price_rows_skip_gaps(closes):
    closes.loc[pd.Timestamp("2026-08-14"), "AVGO"] = float("nan")
    rows = history.price_rows(closes)
    assert len(rows) == 5


def test_ingest_prices_is_idempotent(conn, closes):
    assert history.ingest_prices(conn, closes) == 6
    assert history.ingest_prices(conn, closes) == 6
    assert _count(conn) == 6


def test_price_rows_of_empty_frame():
    assert history.price_rows(pd.DataFrame()) == []


# --------------------------------------------------------------------------- #
# read API                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def week_of_snapshots(conn):
    rows = []
    for day, value in [("2026-08-08", 100.0), ("2026-08-12", 104.0),
                       ("2026-08-15", 110.0)]:
        rows.append(history.MetricRow("NVDA", day, "snapshot", "ps", "", value))
        rows.append(history.MetricRow("AVGO", day, "snapshot", "ps", "", 50.0))
    history.upsert_rows(conn, rows)
    return conn


def test_series_returns_sorted_observations(week_of_snapshots):
    got = history.series(week_of_snapshots, "NVDA", "ps")
    assert list(got["as_of"]) == ["2026-08-08", "2026-08-12", "2026-08-15"]
    assert list(got["value"]) == [100.0, 104.0, 110.0]


def test_series_filters_by_period_type(conn):
    history.upsert_rows(conn, [
        history.MetricRow("NVDA", "2026-08-15", "snapshot", "ps", "", 21.5),
        history.MetricRow("NVDA", "2026-08-15", "annual", "ps", "", 18.0),
    ])
    snap = history.series(conn, "NVDA", "ps", period_types=("snapshot",))
    both = history.series(conn, "NVDA", "ps", period_types=("snapshot", "annual"))
    assert len(snap) == 1
    assert len(both) == 2


def test_latest_and_prior_computes_delta(week_of_snapshots):
    got = history.latest_and_prior(week_of_snapshots, "ps", window_days=7)
    nvda = got.set_index("ticker").loc["NVDA"]
    assert nvda["latest_as_of"] == "2026-08-15"
    assert nvda["latest"] == 110.0
    assert nvda["prior_as_of"] == "2026-08-08"
    assert nvda["prior"] == 100.0
    assert nvda["delta"] == pytest.approx(10.0)


def test_latest_and_prior_has_no_prior_on_first_observation(conn):
    """One snapshot means no delta at all, never a delta against a backfill."""
    history.upsert_rows(conn, [
        history.MetricRow("NVDA", "2026-08-15", "snapshot", "ps", "", 21.5),
    ])
    got = history.latest_and_prior(conn, "ps", window_days=7)
    assert len(got) == 1
    assert pd.isna(got.iloc[0]["prior"])
    assert pd.isna(got.iloc[0]["delta"])


def test_latest_and_prior_ignores_non_snapshot_rows(conn):
    history.upsert_rows(conn, [
        history.MetricRow("NVDA", "2026-08-15", "snapshot", "ps", "", 21.5),
        history.MetricRow("NVDA", "2026-01-31", "annual", "ps", "", 12.0),
    ])
    got = history.latest_and_prior(conn, "ps", window_days=7)
    assert pd.isna(got.iloc[0]["prior"])


def test_coverage_summarises_the_store(week_of_snapshots):
    got = history.coverage(week_of_snapshots)
    row = got[(got["period_type"] == "snapshot") & (got["metric"] == "ps")].iloc[0]
    assert row["tickers"] == 2
    assert row["observations"] == 6
    assert row["first_as_of"] == "2026-08-08"
    assert row["last_as_of"] == "2026-08-15"


# --------------------------------------------------------------------------- #
# rebuild                                                                      #
# --------------------------------------------------------------------------- #

def test_as_of_from_filename_parses_both_prefixes():
    assert history.as_of_from_filename(
        Path("data/fundamentals_20260815.csv")) == "2026-08-15"
    assert history.as_of_from_filename(
        Path("data/estimates_20260815.csv")) == "2026-08-15"


def test_as_of_from_filename_rejects_junk():
    assert history.as_of_from_filename(Path("data/notes.csv")) is None
    assert history.as_of_from_filename(Path("data/fundamentals_x.csv")) is None


def test_rebuild_reconstructs_from_raw_files(tmp_path, snapshot_df, conn):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    snapshot_df.to_csv(data_dir / "fundamentals_20260815.csv", index=False)

    est = [
        history.MetricRow("NVDA", "2026-08-15", "estimate", "epsEst",
                          "2027-01-31", 8.95773),
        history.MetricRow("NVDA", "2026-05-17", "estimate", "epsEst",
                          "2027-01-31", 8.38125),
    ]
    extract.write_estimates_csv(est, data_dir / "estimates_20260815.csv")

    report = history.rebuild(conn, data_dir)

    assert report["snapshots"] == 1
    assert report["estimates"] == 1
    assert report["rows"] == 38 + 2
    assert _count(conn) == 40


def test_rebuild_reports_that_prices_were_not_restored(tmp_path, snapshot_df,
                                                       conn, closes):
    """Closes live only in the store, so a rebuild must not claim to have them.

    Silently returning a store that is 93% smaller than the one it replaced
    looks identical to data loss.
    """
    history.ingest_prices(conn, closes)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    snapshot_df.to_csv(data_dir / "fundamentals_20260815.csv", index=False)

    report = history.rebuild(conn, data_dir)

    assert report["prices"] == 0


def test_rebuild_round_trips_float_values_exactly(tmp_path, snapshot_df, conn):
    """A rebuilt store must equal the one it replaced, to the last bit.

    pandas' default CSV float parser is fast rather than exact; without
    round_trip, 264 of 2761 real snapshot values came back altered in their
    last two bits.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    history.ingest_snapshot(conn, snapshot_df, "2026-08-15")
    before = conn.execute(
        "SELECT ticker, metric, value FROM metrics ORDER BY ticker, metric"
    ).fetchall()

    snapshot_df.to_csv(data_dir / "fundamentals_20260815.csv", index=False)
    history.rebuild(conn, data_dir)

    after = conn.execute(
        "SELECT ticker, metric, value FROM metrics ORDER BY ticker, metric"
    ).fetchall()
    assert after == before


def test_rebuild_is_idempotent(tmp_path, snapshot_df, conn):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    snapshot_df.to_csv(data_dir / "fundamentals_20260815.csv", index=False)

    history.rebuild(conn, data_dir)
    history.rebuild(conn, data_dir)

    assert _count(conn) == 38


def test_rebuild_clears_stale_rows(tmp_path, snapshot_df, conn):
    """Rows with no backing raw file must not survive a rebuild."""
    history.upsert_rows(conn, [
        history.MetricRow("GONE", "2020-01-01", "snapshot", "ps", "", 1.0),
    ])
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    snapshot_df.to_csv(data_dir / "fundamentals_20260815.csv", index=False)

    history.rebuild(conn, data_dir)

    stale = conn.execute(
        "SELECT COUNT(*) FROM metrics WHERE ticker='GONE'"
    ).fetchone()[0]
    assert stale == 0


# --------------------------------------------------------------------------- #
# run bookkeeping                                                              #
# --------------------------------------------------------------------------- #

def test_start_run_records_an_open_run(conn):
    run_id = history.start_run(conn)
    row = conn.execute(
        "SELECT status, finished_at FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row[0] == "running"
    assert row[1] is None


def test_finish_run_closes_it(conn):
    run_id = history.start_run(conn)
    history.finish_run(conn, run_id, "ok", tickers_ok=147, tickers_failed=1)
    row = conn.execute(
        "SELECT status, tickers_ok, tickers_failed, finished_at FROM runs "
        "WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row[0] == "ok"
    assert row[1] == 147
    assert row[2] == 1
    assert row[3] is not None


def test_run_ids_are_unique(conn):
    assert history.start_run(conn) != history.start_run(conn)
