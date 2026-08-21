"""SQLite store for the fundamentals history.

Knows SQL and nothing else: no yfinance, no HTML. Callers hand it MetricRow
tuples and it persists them idempotently. Because the raw dated CSVs in data/
remain the record of truth, this database is always reconstructable.
"""
from __future__ import annotations

import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, NamedTuple

import pandas as pd

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "history.db"


class MetricRow(NamedTuple):
    """One observation. The unit every writer produces and the store consumes."""

    ticker: str
    as_of: str        # ISO date the value is as-of
    period_type: str  # 'snapshot'|'daily'|'estimate'|'quarter'|'annual'
    metric: str
    ref_period: str   # absolute fiscal period for estimates; '' otherwise
    value: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
  ticker      TEXT NOT NULL,
  as_of       TEXT NOT NULL,
  period_type TEXT NOT NULL,
  metric      TEXT NOT NULL,
  ref_period  TEXT NOT NULL,
  value       REAL NOT NULL,
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, as_of, period_type, metric, ref_period)
);

CREATE TABLE IF NOT EXISTS companies (
  ticker      TEXT PRIMARY KEY,
  name        TEXT,
  sector      TEXT,
  subindustry TEXT,
  updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  started_at    TEXT,
  finished_at   TEXT,
  status        TEXT,
  tickers_ok    INTEGER,
  tickers_failed INTEGER
);

-- Ownership does not fit MetricRow: a row is keyed by *who* holds the stock, not
-- by a metric name, and carries four numbers at once. Forcing it through
-- ref_period would overload a column the spec defines as a fiscal period.
--
-- as_of is the holder's own "Date Reported", not the run date: yfinance returns
-- mixed report dates within a single frame (NVDA on 2026-08-16 carried both
-- 2026-03-31 and 2026-06-30 rows), so a per-frame stamp would misdate half of
-- them. kind separates the 13F institution list from the fund list, which
-- overlap by holder name.
CREATE TABLE IF NOT EXISTS holdings (
  ticker      TEXT NOT NULL,
  as_of       TEXT NOT NULL,   -- the holder's report date
  kind        TEXT NOT NULL,   -- 'institution' | 'fund'
  holder      TEXT NOT NULL,
  shares      REAL,
  value       REAL,
  pct_held    REAL,
  pct_change  REAL,            -- quarter over quarter, as reported
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, as_of, kind, holder)
);

-- One filing line. Shares+transaction+date is not unique on its own (a director
-- can file two identical gifts on one day), so the insider is in the key too.
CREATE TABLE IF NOT EXISTS insider_txns (
  ticker      TEXT NOT NULL,
  as_of       TEXT NOT NULL,
  insider     TEXT NOT NULL,
  position    TEXT,
  txn_type    TEXT NOT NULL,   -- not `transaction`: reserved word in SQLite
  shares      REAL,
  value       REAL,
  ownership   TEXT,
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, as_of, insider, txn_type, shares)
);

-- 13F institutional holdings, filed per *manager* rather than per issuer. This
-- is a different dataset from `holdings` above despite the similar shape:
-- `holdings` is Yahoo's top-10 list per ticker (BlackRock, Vanguard, State
-- Street -- no hedge fund is ever large enough to appear), while this table is
-- parsed from the manager's own SEC filing.
--
-- Grain is the CUSIP, never a pre-aggregated ticker: Alphabet's Class A and
-- Class C are separate CUSIPs and a fund can hold either or both. Quarter-over-
-- quarter deltas are computed at query time from adjacent quarters and are
-- deliberately not stored -- derived data does not belong in the store.
CREATE TABLE IF NOT EXISTS thirteenf (
  cik         TEXT NOT NULL,
  fund        TEXT NOT NULL,
  quarter     TEXT NOT NULL,   -- the filing's report date, e.g. '2026-06-30'
  cusip       TEXT NOT NULL,
  ticker      TEXT NOT NULL,   -- resolved via data/cusip_map.csv
  issuer      TEXT,
  title_class TEXT,
  shares      REAL,
  value       REAL,            -- US dollars (filings have reported dollars since 2023)
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (cik, quarter, cusip)
);

-- Load-bearing, for the same reason `runs` exists -- and the stakes are higher.
-- A complete exit is encoded as the ABSENCE of a thirteenf row, and an exit is
-- the most interesting signal the dataset carries. A fund liquidating its entire
-- NVDA stake and an EDGAR timeout are the same absence. Without a separate
-- record that the filing was successfully ingested, every network hiccup renders
-- as a fabricated exit.
CREATE TABLE IF NOT EXISTS thirteenf_filings (
  cik         TEXT NOT NULL,
  fund        TEXT NOT NULL,
  cohort      TEXT,            -- 'Tiger' | 'Multi-strat' | 'Concentrated'
  quarter     TEXT NOT NULL,
  accession   TEXT,            -- re-ingest when a 13F-HR/A arrives under a new one
  filed_date  TEXT,
  n_positions INTEGER,         -- lines in the filing after the equity filter
  n_universe  INTEGER,         -- of those, lines resolving into the universe
  book_value  REAL,            -- total filing value, INCLUDING off-universe
                               -- positions. The store keeps only universe rows,
                               -- so a position's share of the manager's book is
                               -- only computable if the whole-book total rides
                               -- along here. That ratio is what separates a
                               -- conviction position from market-making flow.
  status      TEXT NOT NULL,   -- 'ok' | 'failed' | 'no-filing'
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (cik, quarter)
);

CREATE INDEX IF NOT EXISTS idx_metric_series ON metrics (ticker, metric, as_of);
CREATE INDEX IF NOT EXISTS idx_metric_xsec   ON metrics (metric, as_of);
CREATE INDEX IF NOT EXISTS idx_holdings      ON holdings (ticker, kind, as_of);
CREATE INDEX IF NOT EXISTS idx_insiders      ON insider_txns (ticker, as_of);
CREATE INDEX IF NOT EXISTS idx_13f_ticker    ON thirteenf (ticker, quarter);
CREATE INDEX IF NOT EXISTS idx_13f_fund      ON thirteenf (cik, quarter);
"""


def is_finite(value) -> bool:
    """True only for real, finite numbers. Guards every write."""
    if isinstance(value, bool) or value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_UPSERT_SQL = """
INSERT INTO metrics
    (ticker, as_of, period_type, metric, ref_period, value, ingested_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker, as_of, period_type, metric, ref_period)
DO UPDATE SET value = excluded.value, ingested_at = excluded.ingested_at
"""


def upsert_rows(conn: sqlite3.Connection, rows: Iterable[MetricRow]) -> int:
    """Write rows, replacing any existing observation with the same key.

    Non-finite values are dropped rather than stored: a missing row means
    "never observed", which must stay distinct from a stored zero.
    """
    stamp = _now()
    payload = [
        (r.ticker, r.as_of, r.period_type, r.metric, r.ref_period,
         float(r.value), stamp)
        for r in rows
        if is_finite(r.value)
    ]
    if not payload:
        return 0
    conn.executemany(_UPSERT_SQL, payload)
    conn.commit()
    return len(payload)


# The numeric columns of data/fundamentals_YYYYMMDD.csv. The remaining five
# columns are company attributes (ticker, name, sector, subindustry) or a
# rendered artifact (spark, which daily close rows now supersede).
NUMERIC_METRICS: tuple[str, ...] = (
    "price", "marketCap", "forwardPE", "trailingPE", "evEbitda", "ps",
    "fcfYield", "revGrowth", "epsGrowth", "grossMargin", "opMargin",
    "netMargin", "roe", "netDebtEbitda", "fcf", "cash",
    "ret1m", "ret6m", "retYtd",
)


def snapshot_rows(df, as_of: str) -> list[MetricRow]:
    """Melt one wide snapshot DataFrame into MetricRow observations."""
    rows: list[MetricRow] = []
    for record in df.to_dict("records"):
        ticker = record.get("ticker")
        if not ticker:
            continue
        for metric in NUMERIC_METRICS:
            value = record.get(metric)
            if not is_finite(value):
                continue
            rows.append(
                MetricRow(str(ticker), as_of, "snapshot", metric, "", float(value))
            )
    return rows


def ingest_snapshot(conn: sqlite3.Connection, df, as_of: str) -> int:
    return upsert_rows(conn, snapshot_rows(df, as_of))


_COMPANY_SQL = """
INSERT INTO companies (ticker, name, sector, subindustry, updated_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(ticker) DO UPDATE SET
    name = excluded.name,
    sector = excluded.sector,
    subindustry = excluded.subindustry,
    updated_at = excluded.updated_at
"""


def upsert_companies(conn: sqlite3.Connection, df) -> int:
    stamp = _now()
    payload = [
        (str(r["ticker"]), r.get("name"), r.get("sector"),
         r.get("subindustry"), stamp)
        for r in df.to_dict("records")
        if r.get("ticker")
    ]
    if not payload:
        return 0
    conn.executemany(_COMPANY_SQL, payload)
    conn.commit()
    return len(payload)


def price_rows(closes) -> list[MetricRow]:
    """Convert a date-indexed close-price frame into daily observations.

    Accepts what yf.download(...)["Close"] returns: rows are dates, columns
    are tickers. Gaps (holidays, halted names) produce no row.
    """
    if closes is None or closes.empty:
        return []
    rows: list[MetricRow] = []
    for stamp, series in closes.iterrows():
        as_of = pd.Timestamp(stamp).date().isoformat()
        for ticker, value in series.items():
            if not is_finite(value):
                continue
            rows.append(
                MetricRow(str(ticker), as_of, "daily", "close", "", float(value))
            )
    return rows


def ingest_prices(conn: sqlite3.Connection, closes) -> int:
    return upsert_rows(conn, price_rows(closes))


def series(
    conn: sqlite3.Connection,
    ticker: str,
    metric: str,
    period_types: tuple[str, ...] = ("snapshot",),
) -> pd.DataFrame:
    """One company's history for one metric, oldest first."""
    placeholders = ",".join("?" for _ in period_types)
    return pd.read_sql_query(
        f"""
        SELECT as_of, ref_period, value
        FROM metrics
        WHERE ticker = ? AND metric = ? AND period_type IN ({placeholders})
        ORDER BY as_of, ref_period
        """,
        conn,
        params=[ticker, metric, *period_types],
    )


_LATEST_PRIOR_SQL = """
WITH snap AS (
    SELECT ticker, as_of, value
    FROM metrics
    WHERE metric = ? AND period_type = 'snapshot'
),
cur AS (
    SELECT s.ticker, s.as_of, s.value
    FROM snap s
    JOIN (SELECT ticker, MAX(as_of) AS as_of FROM snap GROUP BY ticker) l
      ON s.ticker = l.ticker AND s.as_of = l.as_of
),
prev AS (
    SELECT s.ticker, MAX(s.as_of) AS as_of
    FROM snap s
    JOIN cur c ON s.ticker = c.ticker
    WHERE s.as_of <= date(c.as_of, ?)
    GROUP BY s.ticker
)
SELECT c.ticker,
       c.as_of  AS latest_as_of,
       c.value  AS latest,
       p.as_of  AS prior_as_of,
       pv.value AS prior,
       c.value - pv.value AS delta
FROM cur c
LEFT JOIN prev p  ON p.ticker = c.ticker
LEFT JOIN snap pv ON pv.ticker = p.ticker AND pv.as_of = p.as_of
ORDER BY c.ticker
"""


def latest_and_prior(
    conn: sqlite3.Connection, metric: str, window_days: int
) -> pd.DataFrame:
    """Latest snapshot value against the newest one at least window_days old.

    Restricted to period_type='snapshot' on purpose: comparing an observed
    value against a reconstructed quarterly point would produce a real-looking
    number with no meaning. With a single observation the prior is null and no
    delta exists, which is the correct answer rather than a missing feature.
    """
    return pd.read_sql_query(
        _LATEST_PRIOR_SQL, conn, params=[metric, f"-{int(window_days)} days"]
    )


def coverage(conn: sqlite3.Connection) -> pd.DataFrame:
    """What the store actually holds. For diagnosing gaps."""
    return pd.read_sql_query(
        """
        SELECT period_type,
               metric,
               COUNT(DISTINCT ticker) AS tickers,
               COUNT(*)               AS observations,
               MIN(as_of)             AS first_as_of,
               MAX(as_of)             AS last_as_of
        FROM metrics
        GROUP BY period_type, metric
        ORDER BY period_type, metric
        """,
        conn,
    )


_STAMP_RE = re.compile(r"_(\d{4})(\d{2})(\d{2})\.csv(?:\.gz)?$")


def as_of_from_filename(path) -> str | None:
    """'data/fundamentals_20260815.csv[.gz]' -> '2026-08-15'."""
    match = _STAMP_RE.search(str(path))
    if not match:
        return None
    return "-".join(match.groups())


def _dated(data_dir: Path, kind: str) -> list[Path]:
    """Every dated file of one kind, compressed or plain, one per date.

    Files written before the gzip switch remain readable; where both forms of a
    date exist the compressed one wins, so a half-finished migration cannot
    ingest the same day twice.
    """
    found: dict[str, Path] = {}
    for path in sorted(data_dir.glob(f"{kind}_*.csv")):
        found[path.name[:-4]] = path
    for path in sorted(data_dir.glob(f"{kind}_*.csv.gz")):
        found[path.name[:-7]] = path
    return [found[k] for k in sorted(found)]


def rebuild(conn: sqlite3.Connection, data_dir) -> dict[str, int]:
    """Drop the derived tables and reconstruct them from the raw CSVs.

    The raw dated CSVs are the record of truth, so this is always safe: a
    corrupted or deleted database costs a rebuild, not data. Estimates are
    included precisely because they cannot be refetched.

    Daily closes are the exception and are **not** restored: no CSV holds them,
    because unlike estimates they can be refetched in full at any time. A
    rebuilt store is therefore complete in the perishable data and empty of
    prices until the next normal run repopulates them. `report['prices']`
    reports the shortfall so a caller cannot mistake the gap for data loss.
    """
    import extract  # local import: history must not depend on extract at load

    data_dir = Path(data_dir)
    ensure_schema(conn)
    conn.execute("DELETE FROM metrics")
    conn.execute("DELETE FROM companies")
    conn.commit()

    report = {"snapshots": 0, "estimates": 0, "rows": 0}

    for path in sorted(_dated(data_dir, "fundamentals")):
        as_of = as_of_from_filename(path)
        if as_of is None:
            continue
        # round_trip for the same reason read_estimates_csv needs it: the
        # default parser is inexact, and a snapshot is a point-in-time
        # observation that cannot be refetched either.
        frame = pd.read_csv(path, float_precision="round_trip")
        report["rows"] += ingest_snapshot(conn, frame, as_of)
        upsert_companies(conn, frame)
        report["snapshots"] += 1

    for path in sorted(_dated(data_dir, "estimates")):
        if as_of_from_filename(path) is None:
            continue
        report["rows"] += upsert_rows(conn, extract.read_estimates_csv(path))
        report["estimates"] += 1

    report["prices"] = conn.execute(
        "SELECT COUNT(*) FROM metrics WHERE period_type = 'daily'"
    ).fetchone()[0]
    return report


class HoldingRow(NamedTuple):
    """One holder's position in one company, as of that holder's report date."""

    ticker: str
    as_of: str
    kind: str          # 'institution' | 'fund'
    holder: str
    shares: float | None
    value: float | None
    pct_held: float | None
    pct_change: float | None


class InsiderRow(NamedTuple):
    """One insider filing line."""

    ticker: str
    as_of: str
    insider: str
    position: str
    transaction: str
    shares: float | None
    value: float | None
    ownership: str


class ThirteenFRow(NamedTuple):
    """One equity position from one manager's 13F information table."""

    cik: str
    fund: str
    quarter: str
    cusip: str
    ticker: str | None
    issuer: str
    title_class: str
    shares: float | None
    value: float | None


class FilingRow(NamedTuple):
    """Proof that a (manager, quarter) filing was looked at.

    Without this, an absent position cannot be read as a sold-out position.
    """

    cik: str
    fund: str
    cohort: str
    quarter: str
    accession: str
    filed_date: str
    n_positions: int
    n_universe: int
    book_value: float
    status: str


HOLDING_COLUMNS: tuple[str, ...] = HoldingRow._fields
THIRTEENF_COLUMNS: tuple[str, ...] = ThirteenFRow._fields
INSIDER_COLUMNS: tuple[str, ...] = InsiderRow._fields

_HOLDINGS_SQL = """
INSERT INTO holdings
    (ticker, as_of, kind, holder, shares, value, pct_held, pct_change, ingested_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker, as_of, kind, holder) DO UPDATE SET
    shares = excluded.shares, value = excluded.value,
    pct_held = excluded.pct_held, pct_change = excluded.pct_change,
    ingested_at = excluded.ingested_at
"""

_INSIDER_SQL = """
INSERT INTO insider_txns
    (ticker, as_of, insider, position, txn_type, shares, value, ownership,
     ingested_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker, as_of, insider, txn_type, shares) DO UPDATE SET
    position = excluded.position, value = excluded.value,
    ownership = excluded.ownership, ingested_at = excluded.ingested_at
"""


def _opt(value):
    """Keep a real number, drop anything else to NULL.

    Unlike `metrics`, these tables allow NULL: a holder row is meaningful even
    when one of its four numbers is missing, so dropping the whole row would
    lose the position itself.
    """
    return float(value) if is_finite(value) else None


def upsert_holdings(conn: sqlite3.Connection, rows: Iterable[HoldingRow]) -> int:
    stamp = _now()
    payload = [
        (r.ticker, r.as_of, r.kind, r.holder, _opt(r.shares), _opt(r.value),
         _opt(r.pct_held), _opt(r.pct_change), stamp)
        for r in rows
        if r.ticker and r.as_of and r.kind and r.holder
    ]
    if not payload:
        return 0
    conn.executemany(_HOLDINGS_SQL, payload)
    conn.commit()
    return len(payload)


def upsert_insiders(conn: sqlite3.Connection, rows: Iterable[InsiderRow]) -> int:
    stamp = _now()
    payload = [
        (r.ticker, r.as_of, r.insider, r.position, r.transaction,
         _opt(r.shares), _opt(r.value), r.ownership, stamp)
        for r in rows
        if r.ticker and r.as_of and r.insider and r.transaction
    ]
    if not payload:
        return 0
    conn.executemany(_INSIDER_SQL, payload)
    conn.commit()
    return len(payload)


def holdings(conn: sqlite3.Connection, ticker: str, kind: str | None = None):
    """A company's holders, largest position first."""
    sql = ("SELECT ticker, as_of, kind, holder, shares, value, pct_held, pct_change "
           "FROM holdings WHERE ticker = ?")
    params = [ticker]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return pd.read_sql_query(sql + " ORDER BY pct_held DESC", conn, params=params)


def insiders(conn: sqlite3.Connection, ticker: str, limit: int = 25):
    """A company's insider filings, most recent first."""
    return pd.read_sql_query(
        'SELECT as_of, insider, position, txn_type AS "transaction", '
        "shares, value, ownership "
        "FROM insider_txns WHERE ticker = ? ORDER BY as_of DESC LIMIT ?",
        conn, params=[ticker, int(limit)],
    )


_THIRTEENF_SQL = """
INSERT INTO thirteenf
    (cik, fund, quarter, cusip, ticker, issuer, title_class, shares, value,
     ingested_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(cik, quarter, cusip) DO UPDATE SET
    ticker = excluded.ticker, issuer = excluded.issuer,
    title_class = excluded.title_class, shares = excluded.shares,
    value = excluded.value, ingested_at = excluded.ingested_at
"""

_FILING_SQL = """
INSERT INTO thirteenf_filings
    (cik, fund, cohort, quarter, accession, filed_date, n_positions, n_universe,
     book_value, status, ingested_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(cik, quarter) DO UPDATE SET
    cohort = excluded.cohort,
    accession = excluded.accession, filed_date = excluded.filed_date,
    n_positions = excluded.n_positions, n_universe = excluded.n_universe,
    book_value = excluded.book_value, status = excluded.status,
    ingested_at = excluded.ingested_at
"""


def upsert_thirteenf(conn: sqlite3.Connection, rows: Iterable[ThirteenFRow]) -> int:
    stamp = _now()
    payload = [
        (r.cik, r.fund, r.quarter, r.cusip, r.ticker, r.issuer, r.title_class,
         _opt(r.shares), _opt(r.value), stamp)
        for r in rows
        if r.cik and r.quarter and r.cusip and r.ticker
    ]
    if not payload:
        return 0
    conn.executemany(_THIRTEENF_SQL, payload)
    conn.commit()
    return len(payload)


def upsert_filings(conn: sqlite3.Connection, rows: Iterable[FilingRow]) -> int:
    stamp = _now()
    payload = [
        (r.cik, r.fund, r.cohort, r.quarter, r.accession, r.filed_date,
         int(r.n_positions or 0), int(r.n_universe or 0), _opt(r.book_value),
         r.status, stamp)
        for r in rows
        if r.cik and r.quarter and r.status
    ]
    if not payload:
        return 0
    conn.executemany(_FILING_SQL, payload)
    conn.commit()
    return len(payload)


def replace_quarter(conn: sqlite3.Connection, cik: str, quarter: str) -> None:
    """Clear one manager-quarter before re-ingesting it.

    An amendment restates the whole information table rather than supplementing
    it, so upserting a 13F-HR/A over the original would leave behind any position
    the amendment dropped -- which reads as a position the manager still holds.
    """
    conn.execute("DELETE FROM thirteenf WHERE cik = ? AND quarter = ?",
                 (cik, quarter))
    conn.commit()


def ingested_filings(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """(cik, quarter) -> accession, for every filing already stored."""
    return {
        (cik, q): acc
        for cik, q, acc in conn.execute(
            "SELECT cik, quarter, COALESCE(accession, '') FROM thirteenf_filings "
            "WHERE status = 'ok'")
    }


def thirteenf_quarters(conn: sqlite3.Connection) -> list[str]:
    """Quarters with at least one ingested filing, newest first."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT quarter FROM thirteenf_filings WHERE status = 'ok' "
        "ORDER BY quarter DESC")]


def _book_values(conn: sqlite3.Connection, quarter: str) -> dict[str, float]:
    return {cik: bv for cik, bv in conn.execute(
        "SELECT cik, book_value FROM thirteenf_filings "
        "WHERE quarter = ? AND status = 'ok' AND book_value > 0", (quarter,))}


def filed_ciks(conn: sqlite3.Connection, quarter: str) -> set[str]:
    """Managers whose filing for this quarter was successfully ingested.

    The gate on reading an absent position as an exit rather than a gap.
    """
    return {r[0] for r in conn.execute(
        "SELECT cik FROM thirteenf_filings WHERE quarter = ? AND status = 'ok'",
        (quarter,))}


# Clean multiples a share count can change by without the position changing.
_SPLIT_RATIOS = (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50)


def _snap_split(ratio: float) -> float:
    """Round a share-count ratio to a stock split, or to 1 if it is not one.

    Daily closes in this store are back-adjusted for splits AND dividends, while
    13F share counts are as-filed. So the implied-price-to-close ratio drifts a
    few percent for any dividend payer -- IBM runs 1.055 two years back -- and
    that drift must NOT be treated as a share-count adjustment. Only a clean
    multiple is: Netflix 10:1, ServiceNow 5:1, Booking 25:1, CrowdStrike 4:1.

    Without this, a 25:1 split renders as a manager adding 2,400% to a position
    it did not touch.
    """
    if not is_finite(ratio) or ratio <= 0:
        return 1.0
    if 0.90 <= ratio <= 1.10:
        return 1.0
    for candidate in _SPLIT_RATIOS:
        for factor in (float(candidate), 1.0 / candidate):
            if abs(ratio / factor - 1.0) <= 0.05:
                return factor
    # A ratio that is neither ~1 nor a clean split is unexplained; leaving the
    # counts alone keeps a wrong guess out of the numbers.
    return 1.0


def _quarter_price_factor(conn: sqlite3.Connection, quarter: str) -> dict[str, float]:
    """Per ticker, filed price divided by the store's adjusted close."""
    filed = pd.read_sql_query(
        "SELECT ticker, SUM(value) AS v, SUM(shares) AS s FROM thirteenf "
        "WHERE quarter = ? GROUP BY ticker", conn, params=[quarter])
    closes = pd.read_sql_query(
        "SELECT ticker, value FROM metrics WHERE metric = 'close' AND as_of = "
        "(SELECT MAX(as_of) FROM metrics WHERE metric = 'close' AND as_of <= ?)",
        conn, params=[quarter]).set_index("ticker")["value"].to_dict()
    out: dict[str, float] = {}
    for row in filed.itertuples():
        close = closes.get(row.ticker)
        if close and close > 0 and row.s and row.s > 0:
            out[row.ticker] = (row.v / row.s) / close
    return out


def split_ratios(conn: sqlite3.Connection, quarter: str,
                 prev_quarter: str) -> dict[str, float]:
    """Per ticker, what last quarter's share count must be multiplied by.

    Derived from the two quarters' own filings rather than a corporate-actions
    feed: if the filed-price-to-close ratio jumps by a clean multiple between
    them, a split happened in between and the older count is on the old basis.
    """
    cur = _quarter_price_factor(conn, quarter)
    prev = _quarter_price_factor(conn, prev_quarter)
    out: dict[str, float] = {}
    for ticker, before in prev.items():
        after = cur.get(ticker)
        if not after:
            continue
        ratio = _snap_split(before / after)
        if ratio != 1.0:
            out[ticker] = ratio
    return out


def thirteenf_changes(conn: sqlite3.Connection, quarter: str,
                      prev_quarter: str | None = None,
                      ticker: str | None = None,
                      cik: str | None = None):
    """Positions for `quarter` with quarter-over-quarter deltas.

    Returns one row per (manager, ticker), with `action` in NEW/ADD/TRIM/HOLD/
    EXIT. Share classes of one issuer are summed here, at the query boundary,
    so the store keeps them separable while the reader sees one Alphabet line.

    An EXIT row -- present last quarter, gone this quarter -- is emitted only
    for managers that actually filed for `quarter`. For a manager whose filing
    is missing the position is simply unknown, and saying nothing is correct
    where inventing an exit is not.
    """
    def _load(q):
        sql = ("SELECT cik, fund, ticker, SUM(shares) AS shares, "
               "SUM(value) AS value FROM thirteenf WHERE quarter = ?")
        params: list = [q]
        if ticker:
            sql += " AND ticker = ?"
            params.append(ticker)
        if cik:
            sql += " AND cik = ?"
            params.append(cik)
        return pd.read_sql_query(sql + " GROUP BY cik, fund, ticker", conn,
                                 params=params)

    cur = _load(quarter)
    if prev_quarter is None:
        cur["prev_shares"] = float("nan")
        cur["prev_value"] = float("nan")
        prev = cur.iloc[0:0]
    else:
        prev = _load(prev_quarter)
        # Put last quarter's counts on this quarter's share basis before any
        # subtraction happens.
        ratios = split_ratios(conn, quarter, prev_quarter)
        if ratios and len(prev):
            prev = prev.copy()
            prev["shares"] = prev.apply(
                lambda r: r["shares"] * ratios.get(r["ticker"], 1.0), axis=1)

    if prev_quarter is not None:
        merged = cur.merge(
            prev[["cik", "ticker", "shares", "value"]].rename(
                columns={"shares": "prev_shares", "value": "prev_value"}),
            on=["cik", "ticker"], how="outer")
        # An outer join resurrects last quarter's identity columns for exits.
        ident = (pd.concat([cur[["cik", "fund"]], prev[["cik", "fund"]]])
                   .drop_duplicates("cik").set_index("cik")["fund"])
        merged["fund"] = merged["cik"].map(ident)
        filed = filed_ciks(conn, quarter)
        gone = merged["shares"].isna()
        # Drop the would-be exits belonging to managers that did not file.
        merged = merged[~gone | merged["cik"].isin(filed)].copy()
        merged["shares"] = merged["shares"].fillna(0.0)
        merged["value"] = merged["value"].fillna(0.0)
    else:
        merged = cur

    merged["delta_shares"] = merged["shares"] - merged["prev_shares"]
    prior = merged["prev_shares"].where(merged["prev_shares"] > 0)
    merged["delta_pct"] = merged["delta_shares"] / prior

    books = _book_values(conn, quarter)
    merged["pct_of_book"] = merged.apply(
        lambda r: (r["value"] / books[r["cik"]]) if books.get(r["cik"]) else float("nan"),
        axis=1)
    merged["action"] = [_action(s, p) for s, p in
                        zip(merged["shares"], merged["prev_shares"])]
    return merged.sort_values("value", ascending=False).reset_index(drop=True)


def _action(shares, prev) -> str:
    """Label one position's quarter-over-quarter move.

    The 2% deadband keeps routine drift out of the ADD/TRIM badges; a manager
    whose share count moved 0.4% did not make a decision worth flagging.
    """
    has_prev = is_finite(prev) and prev > 0
    if shares <= 0:
        return "EXIT" if has_prev else ""
    if not has_prev:
        return "NEW"
    change = (shares - prev) / prev
    if change > 0.02:
        return "ADD"
    if change < -0.02:
        return "TRIM"
    return "HOLD"


def start_run(conn: sqlite3.Connection) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, 'running')",
        (run_id, _now()),
    )
    conn.commit()
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    tickers_ok: int,
    tickers_failed: int,
) -> None:
    """Close out a run so gaps stay diagnosable.

    Without this, a failed run, a market holiday and a delisting all look
    identical in the data: an absent row.
    """
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, tickers_ok = ?, "
        "tickers_failed = ? WHERE run_id = ?",
        (_now(), status, int(tickers_ok), int(tickers_failed), run_id),
    )
    conn.commit()
