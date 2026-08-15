# Fundamentals History Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the fundamentals tracker a time dimension by capturing every metric and every forward analyst estimate daily into a queryable SQLite store, with plain CSVs remaining the durable record.

**Architecture:** Two new modules split by what each may know. `extract.py` turns yfinance frames into `MetricRow` tuples and resolves relative estimate horizons (`0y`, `+1q`) to absolute fiscal periods; it never touches SQL. `history.py` persists `MetricRow` tuples idempotently and answers queries; it never touches yfinance. `build_dashboard.py` orchestrates both and is otherwise unchanged. Raw daily CSVs stay the record of truth so the database is always rebuildable.

**Tech Stack:** Python 3.12, pandas 3.0.3, stdlib `sqlite3` (SQLite 3.50.2), yfinance 1.4.1, pytest (dev only).

**Spec:** `docs/superpowers/specs/2026-08-15-fundamentals-history-design.md`

> **Status: implemented 2026-08-15, with Task 4 superseded.** Read
> `docs/superpowers/specs/2026-08-15-probe-findings.md` before touching fiscal
> resolution. Task 4's `next_period_end` resolves horizons from `as_of`, which is
> wrong by a full quarter for any company between quarter close and earnings
> (verified against NVDA, AVGO and WMT) and cannot see a stale statement frame.
> Task 4's own `test_resolve_ref_periods_for_nvda` asserts the correct
> `0q == "2026-07-31"` and so fails against the implementation printed beneath it.
> What shipped instead is in `extract.py`: resolution counts forward from the last
> *reported* period, takes the fiscal year from `info.lastFiscalYearEnd`, snaps
> labels to month-end, and prefixes them `FY`/`FQ` so an annual and a quarterly
> estimate ending the same day cannot overwrite each other. Tasks 1-3 and 5-12
> shipped as written.

## Global Constraints

- Python interpreter is `/Users/owen/opt/anaconda3/envs/py312/bin/python3`. Every command below uses it explicitly; there is no active venv.
- No new **runtime** dependencies. `sqlite3` is stdlib. pytest is a dev-only dependency.
- SQLite is 3.50.2, so `INSERT ... ON CONFLICT DO UPDATE` and window functions are available.
- pandas is **3.0.x**, not 2.x. Do not rely on removed 1.x/2.x behaviours (`append`, positional `fillna`, silent downcasting).
- Universe is 148 tickers, defined by `UNIVERSE` in `build_dashboard.py:43`.
- `fetch_all()` sleeps `time.sleep(0.3)` per ticker (`build_dashboard.py:261`). Keep that politeness delay in any new per-ticker fetch loop.
- **macOS has no `flock(1)`.** Single-instance locking must use Python `fcntl.flock`, never the shell utility.
- Missing values write **no row**. Never write NULL, zero, or a carried-forward value.
- An unresolvable `ref_period` means the row is **skipped**, never guessed.
- All dates stored as ISO `YYYY-MM-DD` strings.

## Deviations from the spec (deliberate, made while planning)

1. **§6.1** specifies `estimates_YYYYMMDD.csv` columns as `ticker, horizon, ref_period, field, value`. This plan writes the six `MetricRow` fields instead (`ticker, as_of, period_type, metric, ref_period, value`). Once a horizon is resolved to an absolute `ref_period`, the relative label is redundant, and matching `MetricRow` exactly makes `--rebuild-history` a straight read-and-upsert with no re-derivation.
2. **§6.4** lists `ingest_estimates(conn, rows, as_of)`. Estimate rows are already `MetricRow` tuples carrying their own `as_of`, so they go through `upsert_rows` directly and no wrapper is written. `ingest_prices` is kept because it converts a DataFrame.
3. **§6.4** places `MetricRow` implicitly between the modules; this plan defines it in `history.py` and has `extract.py` import it. Importing a NamedTuple is not "knowing SQL" — the behavioural boundary the spec asks for is intact.
4. **`growth_estimates`** is not ingested. Its `LTG` row has no fiscal period, and the remaining rows duplicate `growth` already captured from `earnings_estimate`/`revenue_estimate`. Noted as a possible follow-up.
5. **§8** lists the attribution invariants (price doubles / EPS flat, and the log identity summing to total return) as tests for this sub-project. They are deferred to sub-project 2, because the function they would test — the decomposition itself — is not written here. Writing assertions against code that does not exist yet would produce either a stub or a test that tests nothing. The requirement is restated at the end of this plan so it travels with the work.

## File Structure

| File | Responsibility |
|---|---|
| `history.py` (new) | `MetricRow`, schema, connect, upsert, snapshot/price ingest, queries, rebuild, run bookkeeping |
| `extract.py` (new) | fiscal-period arithmetic, estimate-frame parsing, estimate fetch, raw estimates CSV I/O |
| `build_dashboard.py` (modify) | orchestration in `main()`, single-instance lock, `--rebuild-history` |
| `run_daily.sh` (rename of `run_weekly.sh`) | driver: no pruning, daily cadence |
| `tests/test_history.py` (new) | store behaviour against in-memory SQLite |
| `tests/test_extract.py` (new) | frame parsing and fiscal arithmetic against fixtures |
| `tests/fixtures/*.json` (new) | real yfinance frames captured 2026-08-15 |
| `~/Library/LaunchAgents/com.owen.fundamentals-tracker.plist` (modify) | weekly → weekdays |

---

### Task 1: Test harness and store schema

**Files:**
- Create: `history.py`
- Create: `tests/test_history.py`
- Create: `requirements-dev.txt`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `history.MetricRow` (NamedTuple: `ticker: str`, `as_of: str`, `period_type: str`, `metric: str`, `ref_period: str`, `value: float`), `history.is_finite(v) -> bool`, `history.connect(path=DB_PATH) -> sqlite3.Connection`, `history.ensure_schema(conn) -> None`, `history.DB_PATH: Path`.

- [ ] **Step 1: Install pytest**

pytest is not present in the `py312` environment. Install it as a dev-only dependency:

```bash
/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pip install pytest
```

Record it:

```bash
printf 'pytest>=8.0\n' > requirements-dev.txt
```

- [ ] **Step 2: Ignore the database and lock file**

Append to `.gitignore` (it currently holds `__pycache__/`, `*.pyc`, `*.db`, `*.db-wal`, `*.db-shm`):

```
.run.lock
.pytest_cache/
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_history.py`:

```python
import sqlite3

import pytest

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
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'history'`

- [ ] **Step 5: Write the minimal implementation**

Create `history.py`:

```python
"""SQLite store for the fundamentals history.

Knows SQL and nothing else: no yfinance, no HTML. Callers hand it MetricRow
tuples and it persists them idempotently. Because the raw dated CSVs in data/
remain the record of truth, this database is always reconstructable.
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import NamedTuple

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
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: PASS (10 tests — `test_is_finite` is parametrized seven ways)

- [ ] **Step 7: Commit**

```bash
git add history.py tests/test_history.py requirements-dev.txt .gitignore
git commit -m "feat: add history store schema and MetricRow contract"
```

---

### Task 2: Idempotent upsert and primary-key collisions

**Files:**
- Modify: `history.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `history.MetricRow`, `history.is_finite`, `history.ensure_schema`.
- Produces: `history.upsert_rows(conn, rows: Iterable[MetricRow]) -> int` returning the number of rows written after filtering.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
def _count(c):
    return c.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: FAIL with `AttributeError: module 'history' has no attribute 'upsert_rows'`

- [ ] **Step 3: Write the minimal implementation**

Add to `history.py` (imports first: add `from datetime import datetime, timezone` and `from typing import Iterable, NamedTuple`):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Commit**

```bash
git add history.py tests/test_history.py
git commit -m "feat: add idempotent metric upsert with collision-safe primary key"
```

---

### Task 3: Snapshot and company ingest

**Files:**
- Modify: `history.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `history.MetricRow`, `history.upsert_rows`.
- Produces: `history.NUMERIC_METRICS: tuple[str, ...]` (19 names), `history.snapshot_rows(df, as_of) -> list[MetricRow]`, `history.ingest_snapshot(conn, df, as_of) -> int`, `history.upsert_companies(conn, df) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py` (add `import pandas as pd` at the top of the file):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: FAIL with `AttributeError: module 'history' has no attribute 'NUMERIC_METRICS'`

- [ ] **Step 3: Write the minimal implementation**

Add to `history.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: PASS (25 tests)

- [ ] **Step 5: Commit**

```bash
git add history.py tests/test_history.py
git commit -m "feat: ingest wide snapshots into long metric rows"
```

---

### Task 4: Fiscal period resolution

**Files:**
- Create: `extract.py`
- Create: `tests/test_extract.py`

**Interfaces:**
- Consumes: `history.MetricRow`, `history.is_finite`.
- Produces: `extract.add_months(d: date, n: int) -> date`, `extract.next_period_end(anchor: date, as_of: date, step_months: int) -> date`, `extract.resolve_ref_periods(as_of: date, annual_ends: Sequence[date], quarter_ends: Sequence[date]) -> dict[str, str]` keyed by `'0q'`, `'+1q'`, `'0y'`, `'+1y'` with unresolvable horizons **absent** from the dict.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extract.py`:

```python
from datetime import date

import pytest

import extract


# NVDA's fiscal calendar, verified 2026-08-15 against yfinance 1.4.1.
NVDA_ANNUAL = [date(2022, 1, 31), date(2023, 1, 31), date(2024, 1, 31),
               date(2025, 1, 31), date(2026, 1, 31)]
NVDA_QUARTER = [date(2025, 4, 30), date(2025, 7, 31), date(2025, 10, 31),
                date(2026, 1, 31), date(2026, 4, 30)]


def test_add_months_preserves_month_end():
    """Fiscal quarters sit on month ends; 30 April + 3 months is 31 July."""
    assert extract.add_months(date(2026, 4, 30), 3) == date(2026, 7, 31)
    assert extract.add_months(date(2026, 1, 31), 3) == date(2026, 4, 30)


def test_add_months_handles_mid_month_dates():
    assert extract.add_months(date(2026, 1, 15), 3) == date(2026, 4, 15)


def test_add_months_handles_leap_year_month_end():
    assert extract.add_months(date(2027, 2, 28), 12) == date(2028, 2, 29)


def test_add_months_goes_backwards():
    assert extract.add_months(date(2026, 1, 31), -3) == date(2025, 10, 31)


def test_next_period_end_walks_forward():
    got = extract.next_period_end(date(2026, 1, 31), date(2026, 8, 15), 12)
    assert got == date(2027, 1, 31)


def test_next_period_end_is_strictly_after_as_of():
    got = extract.next_period_end(date(2026, 1, 31), date(2027, 1, 31), 12)
    assert got == date(2028, 1, 31)


def test_next_period_end_walks_back_when_anchor_is_far_ahead():
    got = extract.next_period_end(date(2030, 1, 31), date(2026, 8, 15), 12)
    assert got == date(2027, 1, 31)


def test_resolve_ref_periods_for_nvda():
    got = extract.resolve_ref_periods(date(2026, 8, 15), NVDA_ANNUAL, NVDA_QUARTER)
    assert got["0y"] == "2027-01-31"
    assert got["+1y"] == "2028-01-31"
    assert got["0q"] == "2026-07-31"
    assert got["+1q"] == "2026-10-31"


def test_resolve_ref_periods_across_year_rollover():
    """The day after FY-end, 0y must advance a full year, not report a revision."""
    before = extract.resolve_ref_periods(date(2027, 1, 30), NVDA_ANNUAL, NVDA_QUARTER)
    after = extract.resolve_ref_periods(date(2027, 2, 1), NVDA_ANNUAL, NVDA_QUARTER)
    assert before["0y"] == "2027-01-31"
    assert after["0y"] == "2028-01-31"


def test_resolve_ref_periods_for_calendar_fiscal_year():
    annual = [date(2025, 12, 31), date(2026, 12, 31)]
    quarter = [date(2026, 3, 31), date(2026, 6, 30)]
    got = extract.resolve_ref_periods(date(2026, 8, 15), annual, quarter)
    assert got["0y"] == "2027-12-31"
    assert got["0q"] == "2026-09-30"


def test_resolve_ref_periods_omits_unresolvable_horizons():
    """A ticker with no statements yet yields no estimate rows, never a guess."""
    assert extract.resolve_ref_periods(date(2026, 8, 15), [], []) == {}
    annual_only = extract.resolve_ref_periods(date(2026, 8, 15), NVDA_ANNUAL, [])
    assert set(annual_only) == {"0y", "+1y"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_extract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'extract'`

- [ ] **Step 3: Write the minimal implementation**

Create `extract.py`:

```python
"""yfinance frames -> MetricRow observations.

Knows yfinance shapes and fiscal calendars. Knows no SQL and no HTML, so every
parsing function here is testable against captured fixtures with no network.
The network-touching helpers are confined to the bottom of the module.

MetricRow is imported from history because it is the shared contract between
producer and store; importing a NamedTuple is not knowledge of SQL.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Sequence

from history import MetricRow, is_finite


def _is_month_end(d: date) -> bool:
    return d.day == monthrange(d.year, d.month)[1]


def add_months(d: date, n: int) -> date:
    """Shift by n months, preserving month-end.

    Fiscal periods sit on month ends and those ends have different lengths:
    30 April plus one quarter is 31 July, not 30 July. Getting this wrong
    misdates every backfilled estimate by a day.
    """
    total = (d.year * 12 + d.month - 1) + n
    year, month = divmod(total, 12)
    month += 1
    last = monthrange(year, month)[1]
    day = last if _is_month_end(d) else min(d.day, last)
    return date(year, month, day)


def next_period_end(anchor: date, as_of: date, step_months: int) -> date:
    """The first period end strictly after as_of, stepping from a known end."""
    cur = anchor
    while cur <= as_of:
        cur = add_months(cur, step_months)
    while add_months(cur, -step_months) > as_of:
        cur = add_months(cur, -step_months)
    return cur


def resolve_ref_periods(
    as_of: date,
    annual_ends: Sequence[date],
    quarter_ends: Sequence[date],
) -> dict[str, str]:
    """Map relative estimate horizons to absolute fiscal period end dates.

    Horizon labels are relative: '0y' means a different fiscal year after
    rollover. Storing by label alone renders that rollover as an enormous fake
    revision, so every estimate row carries the resolved absolute period.

    Horizons that cannot be resolved are omitted. Callers must skip them
    rather than substitute a guess: a wrong reference period corrupts
    attribution invisibly, while a missing point is merely missing.
    """
    out: dict[str, str] = {}
    if annual_ends:
        year0 = next_period_end(max(annual_ends), as_of, 12)
        out["0y"] = year0.isoformat()
        out["+1y"] = add_months(year0, 12).isoformat()
    if quarter_ends:
        q0 = next_period_end(max(quarter_ends), as_of, 3)
        out["0q"] = q0.isoformat()
        out["+1q"] = add_months(q0, 3).isoformat()
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_extract.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add extract.py tests/test_extract.py
git commit -m "feat: resolve relative estimate horizons to absolute fiscal periods"
```

---

### Task 5: EPS trend extraction (the 90-day revision history)

**Files:**
- Modify: `extract.py`
- Modify: `tests/test_extract.py`
- Create: `tests/fixtures/nvda_eps_trend.json`

**Interfaces:**
- Consumes: `extract.resolve_ref_periods`, `history.MetricRow`, `history.is_finite`.
- Produces: `extract.TREND_OFFSETS: dict[str, int]`, `extract.eps_trend_rows(ticker: str, frame: pd.DataFrame, as_of: date, ref_periods: dict[str, str]) -> list[MetricRow]`.

This is the highest-value task in the plan: `eps_trend` is the only forward-looking field with any history, and it yields a real ~90-day revision series on the first run.

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/nvda_eps_trend.json` — real values captured from yfinance 1.4.1 on 2026-08-15:

```json
{
  "0q":  {"current": 2.08380,  "7daysAgo": 2.08051,  "30daysAgo": 2.07846,  "60daysAgo": 2.07667,  "90daysAgo": 1.96260},
  "+1q": {"current": 2.35237,  "7daysAgo": 2.34527,  "30daysAgo": 2.33963,  "60daysAgo": 2.33883,  "90daysAgo": 2.19094},
  "0y":  {"current": 8.95773,  "7daysAgo": 8.96139,  "30daysAgo": 8.94722,  "60daysAgo": 8.92355,  "90daysAgo": 8.38125},
  "+1y": {"current": 12.79836, "7daysAgo": 12.81462, "30daysAgo": 12.73899, "60daysAgo": 12.64277, "90daysAgo": 11.42982}
}
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_extract.py` (add `import json`, `import pandas as pd`, `from pathlib import Path` at the top):

```python
FIXTURES = Path(__file__).parent / "fixtures"


def _frame(name):
    data = json.loads((FIXTURES / name).read_text())
    return pd.DataFrame.from_dict(data, orient="index")


@pytest.fixture
def eps_trend():
    return _frame("nvda_eps_trend.json")


@pytest.fixture
def nvda_refs():
    return extract.resolve_ref_periods(date(2026, 8, 15), NVDA_ANNUAL, NVDA_QUARTER)


def test_trend_offsets_are_calendar_days():
    assert extract.TREND_OFFSETS == {
        "current": 0, "7daysAgo": 7, "30daysAgo": 30,
        "60daysAgo": 60, "90daysAgo": 90,
    }


def test_eps_trend_rows_span_four_horizons_and_five_dates(eps_trend, nvda_refs):
    rows = extract.eps_trend_rows("NVDA", eps_trend, date(2026, 8, 15), nvda_refs)
    assert len(rows) == 20
    assert {r.metric for r in rows} == {"epsEst"}
    assert {r.period_type for r in rows} == {"estimate"}
    assert len({r.ref_period for r in rows}) == 4


def test_eps_trend_rows_are_dated_backwards(eps_trend, nvda_refs):
    rows = extract.eps_trend_rows("NVDA", eps_trend, date(2026, 8, 15), nvda_refs)
    fy = sorted(r for r in rows if r.ref_period == "2027-01-31")
    assert [r.as_of for r in fy] == [
        "2026-05-17", "2026-06-16", "2026-07-16", "2026-08-08", "2026-08-15",
    ]


def test_eps_trend_rows_capture_the_revision(eps_trend, nvda_refs):
    """NVDA FY consensus moved 8.38 -> 8.96 over 90 days: a +6.9% revision."""
    rows = extract.eps_trend_rows("NVDA", eps_trend, date(2026, 8, 15), nvda_refs)
    fy = {r.as_of: r.value for r in rows if r.ref_period == "2027-01-31"}
    assert fy["2026-05-17"] == pytest.approx(8.38125)
    assert fy["2026-08-15"] == pytest.approx(8.95773)


def test_eps_trend_rows_skip_unresolvable_horizons(eps_trend):
    """With no annual statements, the two yearly horizons produce nothing."""
    refs = extract.resolve_ref_periods(date(2026, 8, 15), [], NVDA_QUARTER)
    rows = extract.eps_trend_rows("NVDA", eps_trend, date(2026, 8, 15), refs)
    assert len(rows) == 10
    assert {r.ref_period for r in rows} == {"2026-07-31", "2026-10-31"}


def test_eps_trend_rows_skip_missing_values(eps_trend, nvda_refs):
    eps_trend.loc["0y", "30daysAgo"] = float("nan")
    rows = extract.eps_trend_rows("NVDA", eps_trend, date(2026, 8, 15), nvda_refs)
    assert len(rows) == 19
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_extract.py -v`
Expected: FAIL with `AttributeError: module 'extract' has no attribute 'TREND_OFFSETS'`

- [ ] **Step 4: Write the minimal implementation**

Add to `extract.py` (add `from datetime import date, timedelta` to the imports):

```python
# eps_trend column -> how far before as_of the observation was taken.
#
# Yahoo labels these in calendar days, so they are treated as calendar days
# here. Kept as a table rather than parsed from the column name so that a
# single edit corrects every backfilled date if that assumption is ever
# disproved. See the open item in the spec.
TREND_OFFSETS: dict[str, int] = {
    "current": 0,
    "7daysAgo": 7,
    "30daysAgo": 30,
    "60daysAgo": 60,
    "90daysAgo": 90,
}


def eps_trend_rows(
    ticker: str,
    frame,
    as_of: date,
    ref_periods: dict[str, str],
) -> list[MetricRow]:
    """Expand an eps_trend frame into dated consensus observations.

    This is the only forward-looking field carrying history, so it seeds the
    revision series with roughly 90 days of real observations on first run.
    """
    rows: list[MetricRow] = []
    for horizon, series in frame.iterrows():
        ref = ref_periods.get(str(horizon))
        if ref is None:
            continue
        for column, offset in TREND_OFFSETS.items():
            if column not in series.index:
                continue
            value = series[column]
            if not is_finite(value):
                continue
            observed = (as_of - timedelta(days=offset)).isoformat()
            rows.append(
                MetricRow(ticker, observed, "estimate", "epsEst", ref, float(value))
            )
    return rows
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_extract.py -v`
Expected: PASS (17 tests)

- [ ] **Step 6: Commit**

```bash
git add extract.py tests/test_extract.py tests/fixtures/nvda_eps_trend.json
git commit -m "feat: extract 90-day EPS revision history from eps_trend"
```

---

### Task 6: Remaining estimate frames

**Files:**
- Modify: `extract.py`
- Modify: `tests/test_extract.py`
- Create: `tests/fixtures/nvda_earnings_estimate.json`
- Create: `tests/fixtures/nvda_revenue_estimate.json`
- Create: `tests/fixtures/nvda_eps_revisions.json`

**Interfaces:**
- Consumes: `extract.resolve_ref_periods`, `history.MetricRow`, `history.is_finite`.
- Produces: `extract.FRAME_FIELDS: dict[str, dict[str, str]]`, `extract.frame_rows(ticker: str, frame_name: str, frame: pd.DataFrame, as_of: date, ref_periods: dict[str, str]) -> list[MetricRow]`.

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/nvda_earnings_estimate.json`:

```json
{
  "0q":  {"avg": 2.08380,  "low": 2.03128, "high": 2.20000,  "yearAgoEps": 1.05000, "numberOfAnalysts": 40, "growth": 0.9846},
  "+1q": {"avg": 2.35237,  "low": 2.13000, "high": 2.59000,  "yearAgoEps": 1.30000, "numberOfAnalysts": 40, "growth": 0.8095},
  "0y":  {"avg": 8.95773,  "low": 8.20000, "high": 9.64866,  "yearAgoEps": 4.77000, "numberOfAnalysts": 48, "growth": 0.8779},
  "+1y": {"avg": 12.79836, "low": 9.65000, "high": 16.19516, "yearAgoEps": 8.95773, "numberOfAnalysts": 48, "growth": 0.4288}
}
```

`tests/fixtures/nvda_revenue_estimate.json`:

```json
{
  "0q":  {"avg": 91956584330,  "low": 90302000000,  "high": 96655000000,  "numberOfAnalysts": 43, "yearAgoRevenue": 46743000000,  "growth": 0.9673},
  "+1q": {"avg": 103378458750, "low": 91996000000,  "high": 112149000000, "numberOfAnalysts": 41, "yearAgoRevenue": 57006000000,  "growth": 0.8135},
  "0y":  {"avg": 393928480900, "low": 358367000000, "high": 418025459420, "numberOfAnalysts": 53, "yearAgoRevenue": 215938000000, "growth": 0.8243},
  "+1y": {"avg": 562143085840, "low": 416398000000, "high": 710010000000, "numberOfAnalysts": 55, "yearAgoRevenue": 393928480900, "growth": 0.4270}
}
```

`tests/fixtures/nvda_eps_revisions.json`:

```json
{
  "0q":  {"upLast7days": 1, "upLast30days": 5, "downLast30days": 0, "downLast7Days": 0},
  "+1q": {"upLast7days": 1, "upLast30days": 5, "downLast30days": 0, "downLast7Days": 0},
  "0y":  {"upLast7days": 2, "upLast30days": 4, "downLast30days": 0, "downLast7Days": 0},
  "+1y": {"upLast7days": 2, "upLast30days": 4, "downLast30days": 0, "downLast7Days": 0}
}
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_extract.py`:

```python
def test_frame_fields_covers_three_frames():
    assert set(extract.FRAME_FIELDS) == {
        "earnings_estimate", "revenue_estimate", "eps_revisions",
    }


def test_frame_fields_uses_yahoos_inconsistent_casing():
    """yfinance ships upLast7days but downLast7Days. A silent typo drops data."""
    revisions = extract.FRAME_FIELDS["eps_revisions"]
    assert "upLast7days" in revisions
    assert "downLast7Days" in revisions
    assert "downLast30days" in revisions


def test_frame_rows_from_earnings_estimate(nvda_refs):
    frame = _frame("nvda_earnings_estimate.json")
    rows = extract.frame_rows("NVDA", "earnings_estimate", frame,
                              date(2026, 8, 15), nvda_refs)
    assert {r.as_of for r in rows} == {"2026-08-15"}
    assert {r.period_type for r in rows} == {"estimate"}
    by_metric = {(r.metric, r.ref_period): r.value for r in rows}
    assert by_metric[("epsEstAvg", "2027-01-31")] == pytest.approx(8.95773)
    assert by_metric[("epsEstLow", "2027-01-31")] == pytest.approx(8.20000)
    assert by_metric[("epsEstAnalysts", "2027-01-31")] == 48


def test_frame_rows_from_revenue_estimate(nvda_refs):
    frame = _frame("nvda_revenue_estimate.json")
    rows = extract.frame_rows("NVDA", "revenue_estimate", frame,
                              date(2026, 8, 15), nvda_refs)
    by_metric = {(r.metric, r.ref_period): r.value for r in rows}
    assert by_metric[("revEstAvg", "2027-01-31")] == pytest.approx(393928480900)


def test_frame_rows_from_eps_revisions(nvda_refs):
    frame = _frame("nvda_eps_revisions.json")
    rows = extract.frame_rows("NVDA", "eps_revisions", frame,
                              date(2026, 8, 15), nvda_refs)
    by_metric = {(r.metric, r.ref_period): r.value for r in rows}
    assert by_metric[("epsRevUp30", "2027-01-31")] == 4
    assert by_metric[("epsRevDown30", "2027-01-31")] == 0


def test_frame_rows_ignores_unknown_frame(nvda_refs):
    frame = _frame("nvda_eps_revisions.json")
    rows = extract.frame_rows("NVDA", "growth_estimates", frame,
                              date(2026, 8, 15), nvda_refs)
    assert rows == []


def test_frame_rows_skip_unresolvable_horizons():
    frame = _frame("nvda_earnings_estimate.json")
    refs = extract.resolve_ref_periods(date(2026, 8, 15), NVDA_ANNUAL, [])
    rows = extract.frame_rows("NVDA", "earnings_estimate", frame,
                              date(2026, 8, 15), refs)
    assert {r.ref_period for r in rows} == {"2027-01-31", "2028-01-31"}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_extract.py -v`
Expected: FAIL with `AttributeError: module 'extract' has no attribute 'FRAME_FIELDS'`

- [ ] **Step 4: Write the minimal implementation**

Add to `extract.py`:

```python
# frame name -> {yfinance column: stored metric name}.
#
# Yahoo's casing is inconsistent within a single frame (upLast7days but
# downLast7Days). These names are copied verbatim from the live API; a
# plausible-looking correction silently drops the column.
#
# growth_estimates is deliberately absent: its LTG row has no fiscal period,
# and its remaining rows duplicate the growth figures already captured here.
FRAME_FIELDS: dict[str, dict[str, str]] = {
    "earnings_estimate": {
        "avg": "epsEstAvg",
        "low": "epsEstLow",
        "high": "epsEstHigh",
        "numberOfAnalysts": "epsEstAnalysts",
        "growth": "epsEstGrowth",
    },
    "revenue_estimate": {
        "avg": "revEstAvg",
        "low": "revEstLow",
        "high": "revEstHigh",
        "numberOfAnalysts": "revEstAnalysts",
        "growth": "revEstGrowth",
    },
    "eps_revisions": {
        "upLast7days": "epsRevUp7",
        "upLast30days": "epsRevUp30",
        "downLast7Days": "epsRevDown7",
        "downLast30days": "epsRevDown30",
    },
}


def frame_rows(
    ticker: str,
    frame_name: str,
    frame,
    as_of: date,
    ref_periods: dict[str, str],
) -> list[MetricRow]:
    """Expand a point-in-time estimate frame into observations dated as_of.

    Unlike eps_trend these frames carry no history, so every row is stamped
    with today's date and the series accrues only from capture forward.
    """
    fields = FRAME_FIELDS.get(frame_name)
    if not fields:
        return []
    stamp = as_of.isoformat()
    rows: list[MetricRow] = []
    for horizon, series in frame.iterrows():
        ref = ref_periods.get(str(horizon))
        if ref is None:
            continue
        for column, metric in fields.items():
            if column not in series.index:
                continue
            value = series[column]
            if not is_finite(value):
                continue
            rows.append(
                MetricRow(ticker, stamp, "estimate", metric, ref, float(value))
            )
    return rows
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_extract.py -v`
Expected: PASS (24 tests)

- [ ] **Step 6: Commit**

```bash
git add extract.py tests/test_extract.py tests/fixtures/
git commit -m "feat: extract dispersion, analyst counts and revision breadth"
```

---

### Task 7: Estimate fetch and the raw CSV record

**Files:**
- Modify: `extract.py`
- Modify: `tests/test_extract.py`

**Interfaces:**
- Consumes: everything from Tasks 4-6.
- Produces: `extract.ESTIMATES_COLUMNS: tuple[str, ...]`, `extract.write_estimates_csv(rows: list[MetricRow], path: Path) -> int`, `extract.read_estimates_csv(path: Path) -> list[MetricRow]`, `extract.fetch_ticker_estimates(ticker: str, as_of: date) -> list[MetricRow]` (network), `extract.collect_estimates(tickers: Sequence[str], as_of: date) -> tuple[list[MetricRow], list[str]]` (network, returns rows and the tickers that failed).

The CSV is what makes the database disposable. Estimates cannot be refetched, so they must land in plain text on the same run that captures them.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extract.py`:

```python
def test_estimates_columns_match_metric_row_fields():
    from history import MetricRow
    assert extract.ESTIMATES_COLUMNS == MetricRow._fields


def test_write_then_read_estimates_csv_roundtrips(tmp_path, nvda_refs):
    frame = _frame("nvda_eps_trend.json")
    rows = extract.eps_trend_rows("NVDA", frame, date(2026, 8, 15), nvda_refs)
    path = tmp_path / "estimates_20260815.csv"

    assert extract.write_estimates_csv(rows, path) == len(rows)
    back = extract.read_estimates_csv(path)

    assert back == rows


def test_estimates_csv_has_a_header(tmp_path, nvda_refs):
    frame = _frame("nvda_eps_trend.json")
    rows = extract.eps_trend_rows("NVDA", frame, date(2026, 8, 15), nvda_refs)
    path = tmp_path / "estimates_20260815.csv"
    extract.write_estimates_csv(rows, path)

    header = path.read_text().splitlines()[0]
    assert header == ",".join(extract.ESTIMATES_COLUMNS)


def test_write_estimates_csv_of_empty_rows_still_writes_header(tmp_path):
    path = tmp_path / "estimates_20260815.csv"
    assert extract.write_estimates_csv([], path) == 0
    assert path.read_text().strip() == ",".join(extract.ESTIMATES_COLUMNS)


def test_read_estimates_csv_preserves_ref_period_as_text(tmp_path, nvda_refs):
    """ref_period must stay a string; a date parsed to NaN loses the key."""
    frame = _frame("nvda_eps_trend.json")
    rows = extract.eps_trend_rows("NVDA", frame, date(2026, 8, 15), nvda_refs)
    path = tmp_path / "estimates_20260815.csv"
    extract.write_estimates_csv(rows, path)

    back = extract.read_estimates_csv(path)
    assert all(isinstance(r.ref_period, str) for r in back)
    assert all(isinstance(r.as_of, str) for r in back)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_extract.py -v`
Expected: FAIL with `AttributeError: module 'extract' has no attribute 'ESTIMATES_COLUMNS'`

- [ ] **Step 3: Write the minimal implementation**

Add to `extract.py` (add `import time`, `from pathlib import Path`, `import pandas as pd`, `import yfinance as yf` to the imports):

```python
ESTIMATES_COLUMNS: tuple[str, ...] = MetricRow._fields

# Matches the politeness delay fetch_all() already uses.
FETCH_SLEEP = 0.3


def write_estimates_csv(rows: list[MetricRow], path: Path) -> int:
    """Write the durable record of a day's estimates.

    Estimates are the one dataset that cannot be refetched, so they are
    written as plain text on the same run that captures them. This keeps
    history.db derived and therefore disposable.
    """
    frame = pd.DataFrame(list(rows), columns=list(ESTIMATES_COLUMNS))
    frame.to_csv(path, index=False)
    return len(rows)


def read_estimates_csv(path: Path) -> list[MetricRow]:
    frame = pd.read_csv(
        path,
        dtype={"ticker": str, "as_of": str, "period_type": str,
               "metric": str, "ref_period": str},
        keep_default_na=False,
    )
    return [
        MetricRow(r["ticker"], r["as_of"], r["period_type"],
                  r["metric"], r["ref_period"], float(r["value"]))
        for r in frame.to_dict("records")
    ]


def _statement_ends(frame) -> list[date]:
    if frame is None or getattr(frame, "empty", True):
        return []
    return [d.date() for d in pd.to_datetime(list(frame.columns))]


def fetch_ticker_estimates(ticker: str, as_of: date) -> list[MetricRow]:
    """Pull one company's estimate frames and expand them into rows.

    Raises on a failed fetch so the caller can count the ticker as failed;
    a partial day is acceptable but a silently empty one is not.
    """
    handle = yf.Ticker(ticker)
    ref_periods = resolve_ref_periods(
        as_of,
        _statement_ends(handle.income_stmt),
        _statement_ends(handle.quarterly_income_stmt),
    )
    if not ref_periods:
        return []

    rows = eps_trend_rows(ticker, handle.eps_trend, as_of, ref_periods)
    for name in FRAME_FIELDS:
        frame = getattr(handle, name, None)
        if frame is None or getattr(frame, "empty", True):
            continue
        rows.extend(frame_rows(ticker, name, frame, as_of, ref_periods))
    return rows


def collect_estimates(
    tickers: Sequence[str], as_of: date
) -> tuple[list[MetricRow], list[str]]:
    """Fetch estimates for the universe, tolerating per-ticker failures."""
    rows: list[MetricRow] = []
    failed: list[str] = []
    for i, ticker in enumerate(tickers, 1):
        try:
            rows.extend(fetch_ticker_estimates(ticker, as_of))
        except Exception as exc:  # noqa: BLE001 - one bad name must not end the run
            failed.append(ticker)
            print(f"  [{i:>3}/{len(tickers)}] {ticker:<6} estimates FAILED: {exc}",
                  flush=True)
        else:
            print(f"  [{i:>3}/{len(tickers)}] {ticker:<6} estimates", flush=True)
        time.sleep(FETCH_SLEEP)
    return rows, failed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_extract.py -v`
Expected: PASS (29 tests)

- [ ] **Step 5: Smoke-test the network path against one real ticker**

```bash
/Users/owen/opt/anaconda3/envs/py312/bin/python3 -c "
from datetime import date
import extract
rows = extract.fetch_ticker_estimates('NVDA', date.today())
print(len(rows), 'rows')
print(sorted({r.metric for r in rows}))
print(sorted({r.ref_period for r in rows}))
"
```

Expected: roughly 20 `epsEst` rows plus ~52 point-in-time rows, four distinct `ref_period` values, and no exception.

- [ ] **Step 6: Verify fiscal resolution on a non-calendar, non-US name**

The spec flags that only NVDA was probed. Pick a tracked name with an off-calendar fiscal year and confirm the resolved periods look sane:

```bash
/Users/owen/opt/anaconda3/envs/py312/bin/python3 -c "
from datetime import date
import extract, yfinance as yf
for t in ['AVGO', 'COST', 'NKE']:
    h = yf.Ticker(t)
    refs = extract.resolve_ref_periods(
        date.today(),
        extract._statement_ends(h.income_stmt),
        extract._statement_ends(h.quarterly_income_stmt))
    print(t, refs)
" 2>&1 | grep -v Warning
```

Expected: every resolved date is in the future, `+1y` is twelve months after `0y`, and `0q` precedes `+1q` by one quarter. If any ticker returns `{}`, note it — that ticker will simply contribute no estimate rows, which is the designed behaviour.

- [ ] **Step 7: Commit**

```bash
git add extract.py tests/test_extract.py
git commit -m "feat: fetch estimates and write the durable daily CSV record"
```

---

### Task 8: Price ingest

**Files:**
- Modify: `history.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `history.MetricRow`, `history.upsert_rows`.
- Produces: `history.price_rows(closes: pd.DataFrame) -> list[MetricRow]`, `history.ingest_prices(conn, closes: pd.DataFrame) -> int`. `closes` is indexed by date with one column per ticker, matching what `yf.download(...)["Close"]` returns.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
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
    closes.loc["2026-08-14", "AVGO"] = float("nan")
    rows = history.price_rows(closes)
    assert len(rows) == 5


def test_ingest_prices_is_idempotent(conn, closes):
    assert history.ingest_prices(conn, closes) == 6
    assert history.ingest_prices(conn, closes) == 6
    assert _count(conn) == 6


def test_price_rows_of_empty_frame(conn):
    assert history.price_rows(pd.DataFrame()) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: FAIL with `AttributeError: module 'history' has no attribute 'price_rows'`

- [ ] **Step 3: Write the minimal implementation**

Add to `history.py`:

```python
def price_rows(closes) -> list[MetricRow]:
    """Convert a date-indexed close-price frame into daily observations.

    Accepts what yf.download(...)["Close"] returns: rows are dates, columns
    are tickers. Gaps (holidays, halted names) produce no row.
    """
    if closes is None or closes.empty:
        return []
    rows: list[MetricRow] = []
    for stamp, series in closes.iterrows():
        as_of = stamp.date().isoformat()
        for ticker, value in series.items():
            if not is_finite(value):
                continue
            rows.append(
                MetricRow(str(ticker), as_of, "daily", "close", "", float(value))
            )
    return rows


def ingest_prices(conn: sqlite3.Connection, closes) -> int:
    return upsert_rows(conn, price_rows(closes))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: PASS (29 tests)

- [ ] **Step 5: Commit**

```bash
git add history.py tests/test_history.py
git commit -m "feat: ingest daily close prices"
```

---

### Task 9: Read API

**Files:**
- Modify: `history.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: everything in `history.py` so far.
- Produces: `history.series(conn, ticker, metric, period_types=("snapshot",)) -> pd.DataFrame` with columns `as_of, ref_period, value`; `history.latest_and_prior(conn, metric, window_days) -> pd.DataFrame` with columns `ticker, latest_as_of, latest, prior_as_of, prior, delta`; `history.coverage(conn) -> pd.DataFrame` with columns `period_type, metric, tickers, observations, first_as_of, last_as_of`.

`latest_and_prior` ships here as tested read API with no caller. Sub-project 3 adds the delta badges that consume it; settling the query surface in one pass is cheaper than revisiting the store later.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: FAIL with `AttributeError: module 'history' has no attribute 'series'`

- [ ] **Step 3: Write the minimal implementation**

Add to `history.py` (add `import pandas as pd` to the imports):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: PASS (35 tests)

- [ ] **Step 5: Commit**

```bash
git add history.py tests/test_history.py
git commit -m "feat: add series, latest_and_prior and coverage queries"
```

---

### Task 10: Rebuild from the raw CSVs

**Files:**
- Modify: `history.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `history.ingest_snapshot`, `history.upsert_companies`, `history.upsert_rows`, `extract.read_estimates_csv`.
- Produces: `history.as_of_from_filename(path) -> str | None`, `history.rebuild(conn, data_dir: Path) -> dict[str, int]` returning counts keyed `snapshots`, `estimates`, `rows`.

This task is what makes the "database is disposable" claim true rather than aspirational.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py` (add `from pathlib import Path` and `import extract` at the top):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: FAIL with `AttributeError: module 'history' has no attribute 'as_of_from_filename'`

- [ ] **Step 3: Write the minimal implementation**

Add to `history.py` (add `import re` to the imports):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/ -v`
Expected: PASS — 40 in `test_history.py`, 29 in `test_extract.py`, 69 total

- [ ] **Step 5: Commit**

```bash
git add history.py tests/test_history.py
git commit -m "feat: rebuild the store from raw CSVs"
```

---

### Task 11: Run bookkeeping, single-instance lock, and wiring

**Files:**
- Modify: `history.py`
- Modify: `build_dashboard.py:1183-1226` (`main()`), plus a new helper above it
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `history.start_run(conn) -> str`, `history.finish_run(conn, run_id, status, tickers_ok, tickers_failed) -> None`, and in `build_dashboard.py`: `single_instance()` context manager, `record_history(df, closes, est_rows, as_of, failed)`.

- [ ] **Step 1: Write the failing tests for run bookkeeping**

Append to `tests/test_history.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: FAIL with `AttributeError: module 'history' has no attribute 'start_run'`

- [ ] **Step 3: Implement run bookkeeping**

Add to `history.py` (add `import uuid` to the imports):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/test_history.py -v`
Expected: PASS (43 tests)

- [ ] **Step 5: Add the single-instance lock to `build_dashboard.py`**

macOS ships no `flock(1)`, so this must be done in Python. Add to the imports at `build_dashboard.py:19-32`:

```python
import fcntl
import os
from contextlib import contextmanager
from datetime import date
```

Add above `def main():` at `build_dashboard.py:1183`:

```python
LOCK_PATH = ROOT / ".run.lock"


@contextmanager
def single_instance():
    """Refuse to run twice at once.

    A daily LaunchAgent can fire while a slow run is still going. macOS has no
    flock(1), so the lock is taken here with fcntl; the OS releases it when the
    process exits, which means no stale lock file to clean up.
    """
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print("Another run is in progress; exiting.")
        sys.exit(0)
    try:
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
```

- [ ] **Step 6: Add the history recording helper**

Add below `single_instance()` in `build_dashboard.py`:

```python
def record_history(df, closes, est_rows, as_of, failed):
    """Persist one day's observations. Never fatal to the dashboard build."""
    conn = history.connect()
    try:
        history.ensure_schema(conn)
        run_id = history.start_run(conn)
        written = history.ingest_snapshot(conn, df, as_of)
        history.upsert_companies(conn, df)
        written += history.ingest_prices(conn, closes)
        written += history.upsert_rows(conn, est_rows)
        history.finish_run(
            conn, run_id, "ok",
            tickers_ok=len(df) - len(failed), tickers_failed=len(failed),
        )
        print(f"History: {written} rows written for {as_of}")
    finally:
        conn.close()
```

- [ ] **Step 7: Wire into `main()`**

Add the flag beside `--no-fetch` at `build_dashboard.py:1185`:

```python
    ap.add_argument("--rebuild-history", action="store_true",
                    help="drop history.db and rebuild it from the raw CSVs")
```

Immediately after `args = ap.parse_args()` (`build_dashboard.py:1187`):

```python
    if args.rebuild_history:
        conn = history.connect()
        try:
            report = history.rebuild(conn, DATA_DIR)
        finally:
            conn.close()
        print(f"Rebuilt history: {report['rows']} rows from "
              f"{report['snapshots']} snapshots and "
              f"{report['estimates']} estimate files")
        return
```

After the `df.to_csv(...)` line at `build_dashboard.py:1212`:

```python
        as_of = date.today()
        print("Fetching analyst estimates...")
        est_rows, failed = extract.collect_estimates(df["ticker"].tolist(), as_of)
        extract.write_estimates_csv(est_rows, DATA_DIR / f"estimates_{stamp}.csv")

        print("Fetching 5y closes for the history store...")
        closes = fetch_closes(df["ticker"].tolist(), period=STORE_PERIOD)

        record_history(df, closes, est_rows, as_of.isoformat(), failed)
```

Add the module imports beside the existing `import pandas as pd` at `build_dashboard.py:31`:

```python
import extract
import history
```

Finally wrap the body of `main()` in the lock. Change `def main():` so that everything after `args = ap.parse_args()` runs inside `with single_instance():`.

- [ ] **Step 8: Verify `fetch_closes` returns what `price_rows` expects**

`fetch_closes` is defined at `build_dashboard.py:268` and currently pulls `HIST_PERIOD = "1y"`. The store wants five years. Confirm the shape and widen the window:

```bash
/Users/owen/opt/anaconda3/envs/py312/bin/python3 -c "
import build_dashboard as b
c = b.fetch_closes(['NVDA','AVGO'])
print(type(c), c.shape)
print(c.columns.tolist())
print(c.index[:2], c.index.dtype)
" 2>&1 | grep -v Warning
```

Expected: a DataFrame indexed by date with one column per ticker. If the index is not a DatetimeIndex or the columns are a MultiIndex, adapt `price_rows` accordingly and add a test covering the real shape before proceeding.

Then add a constant beside `HIST_PERIOD` at `build_dashboard.py:129`:

```python
STORE_PERIOD = "5y"    # how much daily history the store keeps
```

and give `fetch_closes` a `period=HIST_PERIOD` keyword so the dashboard keeps its 1-year pull while `record_history` requests `STORE_PERIOD`.

- [ ] **Step 9: Run the full suite and a live end-to-end run**

```bash
/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/ -v
/Users/owen/opt/anaconda3/envs/py312/bin/python3 build_dashboard.py
```

Expected: all tests pass; the run prints a history line; `data/estimates_<today>.csv` and `data/history.db` both exist. Then confirm the store is disposable:

```bash
/Users/owen/opt/anaconda3/envs/py312/bin/python3 -c "
import history
conn = history.connect()
print(history.coverage(conn).to_string())
"
mv data/history.db /tmp/history.db.bak
/Users/owen/opt/anaconda3/envs/py312/bin/python3 build_dashboard.py --rebuild-history
```

Expected: coverage lists `snapshot`, `daily` and `estimate` rows; the rebuild reproduces them from the CSVs alone.

- [ ] **Step 10: Commit**

```bash
git add history.py build_dashboard.py tests/test_history.py
git commit -m "feat: wire daily capture into the dashboard build"
```

---

### Task 12: Daily driver and LaunchAgent

**Files:**
- Rename: `run_weekly.sh` → `run_daily.sh`
- Modify: `run_daily.sh`
- Modify: `~/Library/LaunchAgents/com.owen.fundamentals-tracker.plist`

**Interfaces:**
- Consumes: `build_dashboard.py` with the lock and history wiring from Task 11.
- Produces: nothing importable.

- [ ] **Step 1: Rename the driver**

```bash
git mv run_weekly.sh run_daily.sh
```

- [ ] **Step 2: Remove the pruning and update the comments**

In `run_daily.sh`, delete these two lines:

```bash
# prune raw CSVs older than a year; the dashboard only needs the newest
find "$ROOT/data" -name 'fundamentals_*.csv' -type f -mtime +365 -delete 2>/dev/null
```

The snapshot CSVs are now the record of truth. Deleting them would destroy the estimate history, which cannot be refetched.

Update the header comment to match:

```bash
#!/bin/bash
# Daily driver for the fundamentals tracker, invoked by the LaunchAgent
# com.owen.fundamentals-tracker. Safe to run by hand too:  ./run_daily.sh
#
# Raw CSVs in data/ are never pruned: they are the durable record from which
# history.db is rebuilt, and the estimate files cannot be refetched.
```

No locking goes here — `build_dashboard.py` takes an `fcntl` lock itself, because macOS has no `flock(1)`.

- [ ] **Step 3: Verify the driver still runs**

```bash
chmod +x run_daily.sh
./run_daily.sh
echo "EXIT=$?"
tail -5 logs/weekly.log
```

Expected: `EXIT=0` and a fresh `run OK:` line.

- [ ] **Step 4: Point the LaunchAgent at the renamed script and run it on weekdays**

The plist currently holds `StartCalendarInterval = {Minute 0, Weekday 6, Hour 8}` — Saturdays at 08:00 — and `ProgramArguments = [/Users/owen/Desktop/fundamentals_tracker/run_weekly.sh]`. Both must change.

Weekdays only, because a weekend run captures no new closes and only duplicates the prior estimate values:

```bash
PLIST=~/Library/LaunchAgents/com.owen.fundamentals-tracker.plist

/usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 /Users/owen/Desktop/fundamentals_tracker/run_daily.sh" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :StartCalendarInterval" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval array" "$PLIST"
for d in 1 2 3 4 5; do
  /usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:$((d-1)) dict" "$PLIST"
  /usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:$((d-1)):Weekday integer $d" "$PLIST"
  /usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:$((d-1)):Hour integer 18" "$PLIST"
  /usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:$((d-1)):Minute integer 30" "$PLIST"
done
plutil -lint "$PLIST"
```

Hour 18:30 local rather than the previous 08:00: the run then captures the same day's close and the estimates as they stood after the session, instead of yesterday's. Adjust if the machine is reliably asleep then — `launchd` will run a missed job at next wake, but the `as_of` stamp will be the wake date.

- [ ] **Step 5: Reload the agent and verify**

```bash
launchctl unload ~/Library/LaunchAgents/com.owen.fundamentals-tracker.plist
launchctl load ~/Library/LaunchAgents/com.owen.fundamentals-tracker.plist
launchctl list | grep fundamentals
/usr/libexec/PlistBuddy -c "Print" ~/Library/LaunchAgents/com.owen.fundamentals-tracker.plist
```

Expected: the agent is listed, `ProgramArguments` points at `run_daily.sh`, and five weekday entries appear.

- [ ] **Step 6: Commit**

```bash
git add run_daily.sh
git commit -m "chore: run daily on weekdays and stop pruning raw CSVs"
```

---

## Verification

After Task 12, confirm the whole thing end to end:

```bash
/Users/owen/opt/anaconda3/envs/py312/bin/python3 -m pytest tests/ -v
./run_daily.sh && echo "EXIT=$?"
/Users/owen/opt/anaconda3/envs/py312/bin/python3 -c "
import history
conn = history.connect()
print(history.coverage(conn).to_string())
print()
print(history.latest_and_prior(conn, 'ps', window_days=7).head().to_string())
"
```

Expected: every test passes; `coverage` shows `estimate` rows spanning roughly 90 days (from `eps_trend`), `daily` rows spanning five years, and `snapshot` rows for today only; `latest_and_prior` returns null priors until the second daily run.

## Notes for the next sub-project

Sub-project 2 (return attribution) consumes `history.series(conn, ticker, "epsEst", period_types=("estimate",))` together with `metric="close"` rows, computes `ln(P₁/P₀) = ln(fPE₁/fPE₀) + ln(fEPS₁/fEPS₀)` **within a constant `ref_period`**, and must never span a rollover. The invariant tests named in spec §8 belong there, once there is a function to test.
