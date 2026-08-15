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

CREATE INDEX IF NOT EXISTS idx_metric_series ON metrics (ticker, metric, as_of);
CREATE INDEX IF NOT EXISTS idx_metric_xsec   ON metrics (metric, as_of);
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


_STAMP_RE = re.compile(r"_(\d{4})(\d{2})(\d{2})\.csv$")


def as_of_from_filename(path) -> str | None:
    """'data/fundamentals_20260815.csv' -> '2026-08-15'."""
    match = _STAMP_RE.search(str(path))
    if not match:
        return None
    return "-".join(match.groups())


def rebuild(conn: sqlite3.Connection, data_dir) -> dict[str, int]:
    """Drop the derived tables and reconstruct them from the raw CSVs.

    The raw dated CSVs are the record of truth, so this is always safe: a
    corrupted or deleted database costs a rebuild, not data. Estimates are
    included precisely because they cannot be refetched.
    """
    import extract  # local import: history must not depend on extract at load

    data_dir = Path(data_dir)
    ensure_schema(conn)
    conn.execute("DELETE FROM metrics")
    conn.execute("DELETE FROM companies")
    conn.commit()

    report = {"snapshots": 0, "estimates": 0, "rows": 0}

    for path in sorted(data_dir.glob("fundamentals_*.csv")):
        as_of = as_of_from_filename(path)
        if as_of is None:
            continue
        frame = pd.read_csv(path)
        report["rows"] += ingest_snapshot(conn, frame, as_of)
        upsert_companies(conn, frame)
        report["snapshots"] += 1

    for path in sorted(data_dir.glob("estimates_*.csv")):
        if as_of_from_filename(path) is None:
            continue
        report["rows"] += upsert_rows(conn, extract.read_estimates_csv(path))
        report["estimates"] += 1

    return report


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
