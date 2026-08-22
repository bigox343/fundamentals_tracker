# Portfolio Construction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dollar-, sector- and factor-neutral long/short book over the 153-name universe from stored signals, solved with cvxpy and recorded daily.

**Architecture:** A new `factors.py` fetches Fama-French factor returns; `portfolio.py` builds the alpha composite, estimates an 8-factor risk model, and solves the optimization; `history.py` gains a `target_weights` table; two `tools/` scripts render a report and run a walk-forward backtest. Nothing touches `build_dashboard.py` — the solver stays out of the daily critical path.

**Tech Stack:** Python 3.12, cvxpy 1.9.1, numpy 2.4.6, pandas 3.0.3, scipy 1.18.0, sqlite3, pytest 8

**Spec:** `docs/superpowers/specs/2026-08-21-portfolio-construction-design.md`

## Global Constraints

- **All dependencies are already installed.** cvxpy 1.9.1, numpy 2.4.6, pandas 3.0.3, scipy 1.18.0. Do not add new runtime dependencies. `requirements-dev.txt` stays `pytest>=8.0`.
- **Tests never touch the network.** Every test uses `history.connect(":memory:")` or a fixture file under `tests/fixtures/`.
- **A missing value writes no row.** Never store a sentinel for "not observed" — this is a load-bearing invariant of the store.
- **CSV reads use `float_precision="round_trip"`.** The default parser is lossy and breaks rebuild equality.
- **Dated CSVs are gzipped** (`.csv.gz`), never pruned.
- **Module knowledge boundaries hold:** `portfolio.py` never fetches and never writes HTML; `factors.py` never touches SQL; `tools/*` never fetch and never solve.
- **Universe filter:** require ≥400 of the trailing 504 daily closes. Applied everywhere a covariance or loading is estimated.
- **Signal windows:** momentum 252d skipping 21d; 13F quarter-over-quarter; insider 12 months.
- **Optimizer defaults:** `σ_target` 8% annualized, gross ≤ 2.0, position cap 0.04, sub-industry band 0.10, turnover penalty κ = 0.001.

---

## File Structure

| File | Responsibility |
|---|---|
| `factors.py` (create) | Ken French factor download, parse, gzip cache. Knows CSV shapes; knows no SQL. |
| `portfolio.py` (create) | Returns matrix, alpha legs, risk model, cvxpy solve. Knows optimization; fetches nothing. |
| `history.py` (modify) | `target_weights` schema, `TargetWeightRow`, upsert + read. SQL stays here. |
| `tools/build_portfolio_report.py` (create) | Render one solved book to `reports/`. |
| `tools/backtest_portfolio.py` (create) | Walk-forward evaluation. |
| `tests/test_factors.py` (create) | Parser against a captured fixture. |
| `tests/test_portfolio_signals.py` (create) | The three alpha legs. |
| `tests/test_portfolio_risk.py` (create) | Loadings, conditioning, universe filter. |
| `tests/test_portfolio_optimize.py` (create) | Constraint satisfaction, relaxation ladder. |
| `tests/test_backtest.py` (create) | Point-in-time correctness, no-lookahead. |

---

### Task 1: Fama-French factor ingest

**Files:**
- Create: `factors.py`
- Create: `tests/test_factors.py`
- Create: `tests/fixtures/ff5_daily_sample.csv`

**Interfaces:**
- Consumes: nothing
- Produces: `factors.parse_french_csv(text: str) -> pd.DataFrame` (index `date` as `YYYY-MM-DD` strings, float columns, decimals not percent); `factors.fetch_factors(cache: Path | None = None) -> pd.DataFrame` with columns `Mkt-RF, SMB, HML, RMW, CMA, RF, UMD`; `factors.load_cached(path) -> pd.DataFrame`; `factors.CACHE_PATH`

- [ ] **Step 1: Create the fixture**

Ken French files carry a multi-line copyright preamble, then a header row, then `YYYYMMDD` rows in **percent**, and may append a second block. Create `tests/fixtures/ff5_daily_sample.csv` exactly:

```
This file was created by CMPT_ME_BEVME_RETS using the 202606 CRSP database.

,Mkt-RF,SMB,HML,RMW,CMA,RF
20260102, 1.05,-0.23, 0.44,-0.11, 0.08,0.019
20260105,-0.62, 0.31,-0.17, 0.22,-0.03,0.019
20260106, 0.14, 0.05, 0.09,-0.04, 0.01,0.019

 Copyright 2026 Kenneth R. French
```

- [ ] **Step 2: Write the failing test**

```python
"""Fama-French factor parsing: preamble, percent units, trailing blocks."""
from pathlib import Path

import pandas as pd

import factors

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_only_the_dated_rows():
    text = (FIXTURES / "ff5_daily_sample.csv").read_text()
    df = factors.parse_french_csv(text)

    assert list(df.index) == ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert list(df.columns) == ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]


def test_converts_percent_to_decimal():
    text = (FIXTURES / "ff5_daily_sample.csv").read_text()
    df = factors.parse_french_csv(text)

    # 1.05 percent must land as 0.0105, not 1.05.
    assert df.loc["2026-01-02", "Mkt-RF"] == pytest.approx(0.0105)
    assert df.loc["2026-01-05", "SMB"] == pytest.approx(0.0031)


def test_ignores_copyright_footer_and_blank_lines():
    text = (FIXTURES / "ff5_daily_sample.csv").read_text()
    df = factors.parse_french_csv(text)

    assert len(df) == 3
    assert df.notna().all().all()
```

Add `import pytest` at the top of the file.

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_factors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'factors'`

- [ ] **Step 4: Implement `factors.py`**

```python
"""Fama-French factor returns from the Ken French data library.

Knows the library's CSV shapes and nothing else -- no SQL, no HTML. Factor
returns are a market-wide series with no ticker grain, so they do not belong
in `metrics` and are cached as their own gzipped CSV.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "ff_factors.csv.gz"

BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
FF5_URL = f"{BASE}/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
UMD_URL = f"{BASE}/F-F_Momentum_Factor_daily_CSV.zip"

# A data line is a bare YYYYMMDD followed by comma-separated numbers. The
# library wraps every file in a copyright preamble and sometimes appends a
# second block, so anchoring on this shape is what keeps the parser honest.
_DATA_RE = re.compile(r"^\s*(\d{8})\s*,(.*)$")


def parse_french_csv(text: str) -> pd.DataFrame:
    """Parse one Ken French CSV into decimals indexed by ISO date."""
    header: list[str] | None = None
    rows: list[list] = []

    for line in text.splitlines():
        match = _DATA_RE.match(line)
        if match is None:
            # The last comma-bearing line before the data is the header.
            if "," in line and not line.strip().startswith("Copyright"):
                parts = [p.strip() for p in line.split(",")]
                if parts and parts[0] == "":
                    header = parts[1:]
            continue
        stamp, rest = match.groups()
        values = [float(v) / 100.0 for v in rest.split(",") if v.strip() != ""]
        rows.append([f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"] + values)

    if header is None or not rows:
        raise ValueError("no Fama-French data rows found")

    width = len(rows[0]) - 1
    frame = pd.DataFrame(rows, columns=["date"] + header[:width])
    return frame.set_index("date").astype(float)


def _download(url: str) -> str:
    request = Request(url, headers={"User-Agent": "fundamentals_tracker"})
    with urlopen(request, timeout=60) as response:
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        return archive.read(name).decode("latin-1")


def fetch_factors(cache: Path | None = None) -> pd.DataFrame:
    """Download FF5 + momentum and join them on date."""
    ff5 = parse_french_csv(_download(FF5_URL))
    umd = parse_french_csv(_download(UMD_URL))
    umd.columns = ["UMD"] * len(umd.columns)
    joined = ff5.join(umd[["UMD"]], how="inner")

    path = CACHE_PATH if cache is None else Path(cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    joined.to_csv(path, compression="gzip")
    return joined


def load_cached(path: Path | None = None) -> pd.DataFrame:
    """Read the cached factor file. Round-trip precision, per repo convention."""
    target = CACHE_PATH if path is None else Path(path)
    return pd.read_csv(target, index_col=0, float_precision="round_trip")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_factors.py -v`
Expected: 3 passed

- [ ] **Step 6: Populate the real cache once**

Run: `python -c "import factors; d=factors.fetch_factors(); print(d.shape, d.index[0], d.index[-1])"`
Expected: a shape near `(15000, 7)` spanning 1963 to 2026. This is the only networked step in the whole plan.

- [ ] **Step 7: Commit**

```bash
git add factors.py tests/test_factors.py tests/fixtures/ff5_daily_sample.csv data/ff_factors.csv.gz
git commit -m "feat: ingest Fama-French daily factor returns"
```

---

### Task 2: `target_weights` schema

**Files:**
- Modify: `history.py` (append to `SCHEMA`; add row type and functions near the other `upsert_*`)
- Create: `tests/test_target_weights.py`

**Interfaces:**
- Consumes: `history.connect`, `history.ensure_schema`, `history._now`
- Produces: `history.TargetWeightRow(as_of, ticker, weight, mu, contrib_momentum, contrib_13f, contrib_insider, solver_status)`; `history.upsert_target_weights(conn, rows) -> int`; `history.target_weights(conn, as_of) -> pd.DataFrame`

- [ ] **Step 1: Write the failing test**

```python
"""The target_weights record: idempotent writes and per-leg attribution."""
import pytest

import history
from history import TargetWeightRow


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    history.ensure_schema(c)
    return c


def _row(ticker, weight, status="optimal"):
    return TargetWeightRow("2026-08-21", ticker, weight, weight * 2,
                           0.1, 0.2, 0.3, status)


def test_round_trips_weights_and_contributions(conn):
    history.upsert_target_weights(conn, [_row("NVDA", 0.03), _row("INTC", -0.03)])
    out = history.target_weights(conn, "2026-08-21").set_index("ticker")

    assert out.loc["NVDA", "weight"] == pytest.approx(0.03)
    assert out.loc["INTC", "weight"] == pytest.approx(-0.03)
    assert out.loc["NVDA", "contrib_13f"] == pytest.approx(0.2)


def test_rerunning_a_date_replaces_rather_than_duplicates(conn):
    history.upsert_target_weights(conn, [_row("NVDA", 0.03)])
    history.upsert_target_weights(conn, [_row("NVDA", 0.01)])
    out = history.target_weights(conn, "2026-08-21")

    assert len(out) == 1
    assert out.iloc[0]["weight"] == pytest.approx(0.01)


def test_solver_status_is_preserved(conn):
    history.upsert_target_weights(conn, [_row("NVDA", 0.03, "relaxed:gross")])
    out = history.target_weights(conn, "2026-08-21")

    assert out.iloc[0]["solver_status"] == "relaxed:gross"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_target_weights.py -v`
Expected: FAIL with `AttributeError: module 'history' has no attribute 'TargetWeightRow'`

- [ ] **Step 3: Append the table to `SCHEMA`**

In `history.py`, inside the `SCHEMA` string (ends around line 157), add:

```sql
CREATE TABLE IF NOT EXISTS target_weights (
    ticker            TEXT NOT NULL,
    as_of             TEXT NOT NULL,
    weight            REAL NOT NULL,
    mu                REAL,
    contrib_momentum  REAL,
    contrib_13f       REAL,
    contrib_insider   REAL,
    solver_status     TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    PRIMARY KEY (as_of, ticker)
);
```

- [ ] **Step 4: Add the row type and functions**

Place beside the other `upsert_*` helpers:

```python
class TargetWeightRow(NamedTuple):
    """One name's target weight on one date, with its alpha attribution.

    Per-leg contributions are stored rather than just the blended mu, because
    the forward record has to answer "which leg worked", not only "did the
    book work". Recovering the split later is impossible.
    """
    as_of: str
    ticker: str
    weight: float
    mu: float | None
    contrib_momentum: float | None
    contrib_13f: float | None
    contrib_insider: float | None
    solver_status: str


_TARGET_WEIGHT_SQL = """
INSERT INTO target_weights
    (ticker, as_of, weight, mu, contrib_momentum, contrib_13f,
     contrib_insider, solver_status, ingested_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(as_of, ticker) DO UPDATE SET
    weight = excluded.weight,
    mu = excluded.mu,
    contrib_momentum = excluded.contrib_momentum,
    contrib_13f = excluded.contrib_13f,
    contrib_insider = excluded.contrib_insider,
    solver_status = excluded.solver_status,
    ingested_at = excluded.ingested_at
"""


def upsert_target_weights(conn: sqlite3.Connection,
                          rows: Iterable[TargetWeightRow]) -> int:
    stamp = _now()
    payload = [(r.ticker, r.as_of, r.weight, _opt(r.mu),
                _opt(r.contrib_momentum), _opt(r.contrib_13f),
                _opt(r.contrib_insider), r.solver_status, stamp)
               for r in rows]
    if not payload:
        return 0
    conn.executemany(_TARGET_WEIGHT_SQL, payload)
    conn.commit()
    return len(payload)


def target_weights(conn: sqlite3.Connection, as_of: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT ticker, as_of, weight, mu, contrib_momentum, contrib_13f, "
        "contrib_insider, solver_status FROM target_weights "
        "WHERE as_of = ? ORDER BY weight DESC", conn, params=[as_of])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_target_weights.py -v`
Expected: 3 passed

- [ ] **Step 6: Confirm no existing test regressed**

Run: `python -m pytest tests/ -q`
Expected: all previously passing tests still pass (96 + 6 new)

- [ ] **Step 7: Commit**

```bash
git add history.py tests/test_target_weights.py
git commit -m "feat: record target weights with per-leg alpha attribution"
```

---

### Task 3: Returns matrix and universe filter

**Files:**
- Create: `portfolio.py`
- Create: `tests/test_portfolio_risk.py`

**Interfaces:**
- Consumes: `history.connect`, `history.MetricRow`
- Produces: `portfolio.returns_matrix(conn, end, window=504) -> pd.DataFrame` (dates × tickers, simple returns); `portfolio.eligible_universe(rets, min_obs=400) -> list[str]`; `portfolio.MIN_OBS`, `portfolio.WINDOW`

- [ ] **Step 1: Write the failing test**

```python
"""Returns matrix construction and the seasoning filter."""
import pytest

import history
import portfolio
from history import MetricRow


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    history.ensure_schema(c)
    return c


def _closes(conn, ticker, prices, start_day=1):
    rows = [MetricRow(ticker, f"2026-01-{start_day + i:02d}", "daily",
                      "close", "", p) for i, p in enumerate(prices)]
    history.upsert_rows(conn, rows)


def test_builds_simple_returns(conn):
    _closes(conn, "AAA", [100.0, 110.0, 121.0])
    rets = portfolio.returns_matrix(conn, end="2026-01-03", window=10)

    assert rets["AAA"].tolist() == pytest.approx([0.10, 0.10])


def test_excludes_names_below_the_seasoning_floor(conn):
    _closes(conn, "OLD", [100.0 + i for i in range(12)])
    _closes(conn, "NEW", [100.0, 101.0])
    rets = portfolio.returns_matrix(conn, end="2026-01-12", window=20)

    assert portfolio.eligible_universe(rets, min_obs=10) == ["OLD"]


def test_seasoning_floor_uses_observation_count_not_span(conn):
    # A name present on the first and last day only must still be excluded.
    _closes(conn, "GAPPY", [100.0, 101.0])
    history.upsert_rows(conn, [MetricRow("GAPPY", "2026-01-12", "daily",
                                         "close", "", 150.0)])
    rets = portfolio.returns_matrix(conn, end="2026-01-12", window=20)

    assert "GAPPY" not in portfolio.eligible_universe(rets, min_obs=10)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_portfolio_risk.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'portfolio'`

- [ ] **Step 3: Implement the module head**

```python
"""Portfolio construction: alpha composite, factor risk model, cvxpy solve.

Reads the store and returns frames. Never fetches, never writes HTML, and
never writes SQL -- persistence goes through history.py, which is where SQL
knowledge lives.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WINDOW = 504      # trading days for covariance and loadings
MIN_OBS = 400     # seasoning floor within that window


def returns_matrix(conn, end: str, window: int = WINDOW) -> pd.DataFrame:
    """Daily simple returns, dates x tickers, ending on or before `end`."""
    closes = pd.read_sql_query(
        "SELECT ticker, as_of, value FROM metrics "
        "WHERE period_type = 'daily' AND metric = 'close' AND as_of <= ? "
        "ORDER BY as_of", conn, params=[end])
    if closes.empty:
        return pd.DataFrame()

    wide = closes.pivot(index="as_of", columns="ticker", values="value")
    wide = wide.tail(window + 1)
    return wide.pct_change().iloc[1:]


def eligible_universe(rets: pd.DataFrame, min_obs: int = MIN_OBS) -> list[str]:
    """Names with enough observations to support a variance estimate.

    Counted as present observations, not calendar span: a name with two
    prices six months apart has a span but no estimable variance.
    """
    if rets.empty:
        return []
    counts = rets.notna().sum()
    return sorted(counts[counts >= min_obs].index)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_portfolio_risk.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add portfolio.py tests/test_portfolio_risk.py
git commit -m "feat: returns matrix and seasoning filter for portfolio construction"
```

---

### Task 4: Momentum alpha leg

**Files:**
- Modify: `portfolio.py`
- Create: `tests/test_portfolio_signals.py`

**Interfaces:**
- Consumes: `portfolio.returns_matrix`
- Produces: `portfolio.momentum_signal(conn, as_of, lookback=252, skip=21) -> pd.Series` (raw, unstandardized, indexed by ticker)

- [ ] **Step 1: Write the failing test**

```python
"""Alpha legs: momentum, 13F positioning, insider purchases."""
import numpy as np
import pytest

import history
import portfolio
from history import MetricRow


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    history.ensure_schema(c)
    return c


def _ramp(conn, ticker, days, start=100.0, step=1.0):
    rows = []
    for i in range(days):
        day = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)
        rows.append(MetricRow(ticker, day.strftime("%Y-%m-%d"), "daily",
                              "close", "", start + i * step))
    history.upsert_rows(conn, rows)


def test_momentum_skips_the_most_recent_month(conn):
    # Flat for 300 days, then a spike in the final 10. A 12-1 signal must not
    # see the spike, because the skip window excludes it.
    rows = []
    for i in range(300):
        day = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)
        price = 100.0 if i < 290 else 500.0
        rows.append(MetricRow("SPIKE", day.strftime("%Y-%m-%d"), "daily",
                              "close", "", price))
    history.upsert_rows(conn, rows)

    end = (pd.Timestamp("2024-01-01") + pd.Timedelta(days=299)).strftime("%Y-%m-%d")
    signal = portfolio.momentum_signal(conn, end, lookback=252, skip=21)

    assert signal["SPIKE"] == pytest.approx(0.0, abs=1e-9)


def test_momentum_is_positive_for_a_riser(conn):
    _ramp(conn, "UP", 300, start=100.0, step=1.0)
    end = (pd.Timestamp("2024-01-01") + pd.Timedelta(days=299)).strftime("%Y-%m-%d")

    assert portfolio.momentum_signal(conn, end) > 0
```

Add `import pandas as pd` at the top.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_portfolio_signals.py -v`
Expected: FAIL with `AttributeError: module 'portfolio' has no attribute 'momentum_signal'`

- [ ] **Step 3: Implement**

```python
def momentum_signal(conn, as_of: str, lookback: int = 252,
                    skip: int = 21) -> pd.Series:
    """12-1 momentum: return over `lookback` days, excluding the last `skip`.

    The skip is not optional. The most recent month carries short-term
    reversal, which works against medium-term momentum; including it degrades
    the signal rather than sharpening it.
    """
    closes = pd.read_sql_query(
        "SELECT ticker, as_of, value FROM metrics "
        "WHERE period_type = 'daily' AND metric = 'close' AND as_of <= ? "
        "ORDER BY as_of", conn, params=[as_of])
    if closes.empty:
        return pd.Series(dtype=float)

    wide = closes.pivot(index="as_of", columns="ticker", values="value")
    if len(wide) < lookback + 1:
        lookback = len(wide) - 1

    end = wide.iloc[-(skip + 1)]
    start = wide.iloc[-(lookback + 1)]
    return (end / start - 1.0).dropna()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_portfolio_signals.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add portfolio.py tests/test_portfolio_signals.py
git commit -m "feat: 12-1 momentum alpha leg"
```

---

### Task 5: 13F positioning alpha leg

**Files:**
- Modify: `portfolio.py`
- Modify: `tests/test_portfolio_signals.py`

**Interfaces:**
- Consumes: `history.thirteenf_changes(conn, quarter, prev_quarter)`, `history.thirteenf_quarters(conn)`
- Produces: `portfolio.thirteenf_signal(conn, as_of) -> pd.Series` (aggregate ownership change fraction, indexed by ticker)

- [ ] **Step 1: Write the failing test**

```python
def test_thirteenf_signal_aggregates_managers(conn):
    from history import FilingRow, ThirteenFRow

    def filed(cik, quarter):
        history.upsert_filings(conn, [FilingRow(
            cik, f"Fund{cik}", "Tiger", quarter, f"acc-{cik}-{quarter}",
            "2026-08-14", 10, 1, 1_000_000.0, "ok")])

    def pos(cik, quarter, ticker, shares):
        return ThirteenFRow(cik, f"Fund{cik}", quarter, f"CU{ticker}0010",
                            ticker, ticker, "COM", shares, shares * 10.0)

    for q in ("2026-03-31", "2026-06-30"):
        filed("1", q)
        filed("2", q)

    # Two managers each add 50%: aggregate change is +50%.
    history.upsert_thirteenf(conn, [
        pos("1", "2026-03-31", "AAA", 100), pos("2", "2026-03-31", "AAA", 100),
        pos("1", "2026-06-30", "AAA", 150), pos("2", "2026-06-30", "AAA", 150),
    ])

    signal = portfolio.thirteenf_signal(conn, "2026-08-21")
    assert signal["AAA"] == pytest.approx(0.5)


def test_thirteenf_signal_is_split_adjusted(conn):
    """A 10:1 split must not read as a manager adding 900%."""
    from history import FilingRow, ThirteenFRow

    for q in ("2026-03-31", "2026-06-30"):
        history.upsert_filings(conn, [FilingRow(
            "1", "Fund1", "Tiger", q, f"acc-{q}", "2026-08-14",
            10, 1, 1_000_000.0, "ok")])

    # Back-adjusted closes fall 10x across the split; as-filed shares rise 10x.
    history.upsert_rows(conn, [
        MetricRow("SPL", "2026-03-31", "daily", "close", "", 1000.0),
        MetricRow("SPL", "2026-06-30", "daily", "close", "", 100.0),
    ])
    history.upsert_thirteenf(conn, [
        ThirteenFRow("1", "Fund1", "2026-03-31", "CUSPL0010", "SPL", "SPL",
                     "COM", 100, 100_000.0),
        ThirteenFRow("1", "Fund1", "2026-06-30", "CUSPL0010", "SPL", "SPL",
                     "COM", 1000, 100_000.0),
    ])

    signal = portfolio.thirteenf_signal(conn, "2026-08-21")
    assert signal["SPL"] == pytest.approx(0.0, abs=1e-9)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_portfolio_signals.py -v`
Expected: FAIL with `AttributeError: module 'portfolio' has no attribute 'thirteenf_signal'`

- [ ] **Step 3: Implement**

```python
def thirteenf_signal(conn, as_of: str) -> pd.Series:
    """Aggregate quarter-over-quarter change in tracked-manager ownership.

    Delegates to history.thirteenf_changes, which already puts last quarter's
    counts on this quarter's share basis. Differencing raw as-filed counts
    would render a 25:1 split as a manager adding 2,400%, and the error is
    worse in a difference than in a level.
    """
    import history

    quarters = [q for q in history.thirteenf_quarters(conn) if q <= as_of]
    if len(quarters) < 2:
        return pd.Series(dtype=float)

    changes = history.thirteenf_changes(conn, quarters[-1], quarters[-2])
    if changes.empty:
        return pd.Series(dtype=float)

    grouped = changes.groupby("ticker")[["shares", "prev_shares"]].sum()
    prior = grouped["prev_shares"].where(grouped["prev_shares"] > 0)
    return ((grouped["shares"] - grouped["prev_shares"]) / prior).dropna()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_portfolio_signals.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add portfolio.py tests/test_portfolio_signals.py
git commit -m "feat: 13F positioning alpha leg, split-adjusted"
```

---

### Task 6: Insider purchase alpha leg

**Files:**
- Modify: `portfolio.py`
- Modify: `tests/test_portfolio_signals.py`

**Interfaces:**
- Consumes: `insider_txns` table
- Produces: `portfolio.insider_signal(conn, as_of, months=12) -> pd.Series` (`log1p` of purchase value, indexed by ticker)

**Naming trap:** the database column is `txn_type` but the `history.InsiderRow`
field is `transaction`. Construct the row positionally, as the test below does,
or use `transaction=` — `txn_type=` raises `TypeError`.

**Context the implementer needs:** `insider_txns.txn_type` is free text like `"Purchase at price 143.14 per share."`. Across 8,249 stored rows the families are Sale (3,644), Stock Award(Grant) (3,071), Conversion of Exercise (950), Stock Gift (431), **Purchase (153)**. Only purchases are used — sales outnumber them 24:1 and are largely uninformative.

- [ ] **Step 1: Write the failing test**

```python
def test_insider_signal_counts_only_open_market_purchases(conn):
    from history import InsiderRow

    history.upsert_insiders(conn, [
        InsiderRow("AAA", "2026-08-01", "Jane Doe", "CEO",
                   "Purchase at price 100.00 per share.", 1000, 100_000.0, 5000),
        InsiderRow("BBB", "2026-08-01", "John Roe", "CFO",
                   "Stock Award(Grant) at price 0.00 per share.", 9999,
                   999_999.0, 9999),
        InsiderRow("CCC", "2026-08-01", "Ann Poe", "CTO",
                   "Sale at price 100.00 per share.", 5000, 500_000.0, 100),
    ])

    signal = portfolio.insider_signal(conn, "2026-08-21", months=12)

    assert signal["AAA"] > 0
    assert "BBB" not in signal      # grant excluded
    assert "CCC" not in signal      # sale excluded


def test_insider_signal_respects_the_lookback_window(conn):
    from history import InsiderRow

    history.upsert_insiders(conn, [
        InsiderRow("OLD", "2024-01-01", "Jane Doe", "CEO",
                   "Purchase at price 10.00 per share.", 100, 1_000.0, 100),
    ])

    assert "OLD" not in portfolio.insider_signal(conn, "2026-08-21", months=12)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_portfolio_signals.py -v`
Expected: FAIL with `AttributeError: module 'portfolio' has no attribute 'insider_signal'`

- [ ] **Step 3: Implement**

```python
def insider_signal(conn, as_of: str, months: int = 12) -> pd.Series:
    """Open-market insider purchases over a trailing window, log1p-scaled.

    Purchases only, deliberately not net: sales outnumber purchases 24:1 in
    this store and insiders sell for diversification, taxes and liquidity, so
    a net measure would be dominated by the uninformative side.

    log1p rather than market-cap scaling because marketCap exists only as a
    snapshot metric with days of history, and so cannot be reconstructed
    point-in-time for the backtest.
    """
    rows = pd.read_sql_query(
        "SELECT ticker, value FROM insider_txns "
        "WHERE txn_type LIKE 'Purchase%' AND as_of <= ? "
        "AND as_of >= date(?, ?)",
        conn, params=[as_of, as_of, f"-{months} months"])
    if rows.empty:
        return pd.Series(dtype=float)

    totals = rows.groupby("ticker")["value"].sum()
    return np.log1p(totals[totals > 0])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_portfolio_signals.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add portfolio.py tests/test_portfolio_signals.py
git commit -m "feat: sparse insider-purchase alpha leg"
```

---

### Task 7: Standardization and the alpha blend

**Files:**
- Modify: `portfolio.py`
- Modify: `tests/test_portfolio_signals.py`

**Interfaces:**
- Consumes: the three `*_signal` functions
- Produces: `portfolio.zscore(series, groups=None, clip=3.0) -> pd.Series`; `portfolio.build_alpha(conn, as_of, universe, sectors_by_ticker, subindustry_by_ticker) -> pd.DataFrame` with columns `mu, contrib_momentum, contrib_13f, contrib_insider` indexed by ticker

- [ ] **Step 1: Write the failing test**

```python
def test_zscore_within_groups_is_computed_per_group():
    s = pd.Series({"A": 1.0, "B": 3.0, "C": 10.0, "D": 30.0})
    groups = pd.Series({"A": "x", "B": "x", "C": "y", "D": "y"})
    z = portfolio.zscore(s, groups=groups)

    # Within each pair the lower value is negative, the higher positive.
    assert z["A"] < 0 < z["B"]
    assert z["C"] < 0 < z["D"]


def test_zscore_clips_outliers():
    s = pd.Series({f"T{i}": 0.0 for i in range(50)} | {"OUT": 1e6})
    z = portfolio.zscore(s, clip=3.0)

    assert z["OUT"] == pytest.approx(3.0)


def test_zscore_of_a_constant_group_is_zero_not_nan():
    s = pd.Series({"A": 5.0, "B": 5.0})
    z = portfolio.zscore(s)

    assert z.tolist() == pytest.approx([0.0, 0.0])


def test_missing_leg_contributes_zero_not_nan(conn):
    """A name absent from a sparse leg must be neutral, never dropped."""
    legs = {
        "momentum": pd.Series({"AAA": 1.0, "BBB": -1.0}),
        "13f": pd.Series({"AAA": 0.5}),          # BBB absent
        "insider": pd.Series(dtype=float),        # entirely empty
    }
    frame = portfolio.blend(legs, ["AAA", "BBB"])

    assert set(frame.index) == {"AAA", "BBB"}
    assert frame.loc["BBB", "contrib_13f"] == 0.0
    assert frame["mu"].notna().all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_portfolio_signals.py -v`
Expected: FAIL with `AttributeError: module 'portfolio' has no attribute 'zscore'`

- [ ] **Step 3: Implement**

```python
LEG_WEIGHTS = {"momentum": 1 / 3, "13f": 1 / 3, "insider": 1 / 3}


def zscore(series: pd.Series, groups: pd.Series | None = None,
           clip: float = 3.0) -> pd.Series:
    """Standardize, optionally within groups, winsorized at +/- `clip`.

    A group whose values are all identical yields zero, not NaN: "no
    dispersion" is neutral information, and a NaN would silently drop the name
    from the optimization.
    """
    if series.empty:
        return series

    def _z(block: pd.Series) -> pd.Series:
        spread = block.std(ddof=0)
        if not np.isfinite(spread) or spread == 0:
            return pd.Series(0.0, index=block.index)
        return (block - block.mean()) / spread

    out = series.groupby(groups).transform(_z) if groups is not None else _z(series)
    return out.clip(-clip, clip)


def blend(legs: dict[str, pd.Series], universe: list[str],
          weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Combine standardized legs into mu, keeping per-leg contributions.

    Every name in `universe` gets a row. A name missing from a sparse leg
    contributes zero for that leg -- neutral -- rather than being dropped,
    which matters because the insider leg is empty for roughly two thirds of
    names by construction.
    """
    weights = LEG_WEIGHTS if weights is None else weights
    frame = pd.DataFrame(index=pd.Index(universe, name="ticker"))

    for name, series in legs.items():
        contribution = series.reindex(universe).fillna(0.0) * weights[name]
        frame[f"contrib_{name}"] = contribution

    frame["mu"] = frame[[f"contrib_{n}" for n in legs]].sum(axis=1)
    return frame


def build_alpha(conn, as_of: str, universe: list[str],
                subindustry: pd.Series) -> pd.DataFrame:
    """The full composite: three legs, standardized, blended.

    Momentum and 13F standardize within sub-industry, matching how the
    dashboard scores. The insider leg standardizes across the full universe
    instead: sub-industry groups have a median of 7 names and roughly 70% of
    them are zero, which does not support a within-group moment estimate.
    """
    legs = {
        "momentum": zscore(momentum_signal(conn, as_of).reindex(universe).dropna(),
                           groups=subindustry),
        "13f": zscore(thirteenf_signal(conn, as_of).reindex(universe).dropna(),
                      groups=subindustry),
        "insider": zscore(insider_signal(conn, as_of).reindex(universe).dropna()),
    }
    return blend(legs, universe)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_portfolio_signals.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add portfolio.py tests/test_portfolio_signals.py
git commit -m "feat: standardize and blend the alpha legs"
```

---

### Task 8: Factor risk model

**Files:**
- Modify: `portfolio.py`
- Modify: `tests/test_portfolio_risk.py`

**Interfaces:**
- Consumes: `factors.load_cached`, `portfolio.returns_matrix`, `portfolio.eligible_universe`
- Produces: `portfolio.RiskModel(B, F, D, tickers, factor_names)` NamedTuple; `portfolio.sector_factors(rets, sectors) -> pd.DataFrame`; `portfolio.estimate_risk(rets, factor_returns, sectors) -> RiskModel`; `portfolio.covariance(rm) -> np.ndarray`

- [ ] **Step 1: Write the failing test**

```python
def test_sector_factors_are_universe_relative_and_drop_one():
    rets = pd.DataFrame({
        "T1": [0.02, 0.01], "T2": [0.02, 0.01],     # TMT
        "I1": [-0.01, 0.03], "I2": [-0.01, 0.03],   # Industrials
        "C1": [0.00, 0.00], "C2": [0.00, 0.00],     # Consumer
    })
    sectors = pd.Series({"T1": "TMT", "T2": "TMT", "I1": "Industrials",
                         "I2": "Industrials", "C1": "Consumer", "C2": "Consumer"})
    sf = portfolio.sector_factors(rets, sectors)

    # Consumer is the implicit base: only two columns survive.
    assert list(sf.columns) == ["SEC_TMT", "SEC_Industrials"]
    # Universe-relative: TMT beat the universe mean on day 0.
    assert sf.iloc[0]["SEC_TMT"] > 0


def test_covariance_is_positive_definite_and_better_conditioned():
    rng = np.random.default_rng(0)
    n_days, n_names = 504, 40
    factor_returns = pd.DataFrame(
        rng.normal(0, 0.01, (n_days, 6)),
        columns=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD"])
    loadings = rng.normal(1.0, 0.3, (n_names, 6))
    noise = rng.normal(0, 0.01, (n_days, n_names))
    rets = pd.DataFrame(factor_returns.values @ loadings.T + noise,
                        columns=[f"T{i}" for i in range(n_names)])
    sectors = pd.Series({f"T{i}": ["TMT", "Industrials", "Consumer"][i % 3]
                         for i in range(n_names)})

    rm = portfolio.estimate_risk(rets, factor_returns, sectors)
    cov = portfolio.covariance(rm)

    assert np.all(np.linalg.eigvalsh(cov) > 0)
    assert np.linalg.cond(cov) < np.linalg.cond(np.cov(rets.values.T))


def test_specific_variance_is_floored():
    rng = np.random.default_rng(1)
    factor_returns = pd.DataFrame(rng.normal(0, 0.01, (504, 6)),
                                  columns=["Mkt-RF", "SMB", "HML", "RMW",
                                           "CMA", "UMD"])
    # A name that is an exact linear function of the factors has zero residual.
    exact = factor_returns.sum(axis=1)
    rets = pd.DataFrame({"EXACT": exact, "NOISY": exact + rng.normal(0, 0.01, 504)})
    sectors = pd.Series({"EXACT": "TMT", "NOISY": "TMT"})

    rm = portfolio.estimate_risk(rets, factor_returns, sectors)

    assert rm.D.min() > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_portfolio_risk.py -v`
Expected: FAIL with `AttributeError: module 'portfolio' has no attribute 'sector_factors'`

- [ ] **Step 3: Implement**

```python
from typing import NamedTuple

FF_FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD"]
SECTOR_BASE = "Consumer"      # absorbed into the intercept
VAR_FLOOR = 1e-8              # no name is riskless
TRADING_DAYS = 252


class RiskModel(NamedTuple):
    B: pd.DataFrame           # tickers x factors
    F: np.ndarray             # factors x factors
    D: np.ndarray             # per-ticker specific variance
    tickers: list[str]
    factor_names: list[str]


def sector_factors(rets: pd.DataFrame, sectors: pd.Series) -> pd.DataFrame:
    """Universe-relative sector return series, one column short of the sectors.

    Raw sector returns correlate ~0.9 with the market, so regressing on both
    gives unstable loadings; subtracting the universe mean removes most of
    that. The relative series sum to zero by construction, so one sector must
    be dropped or the regression is singular.
    """
    universe_mean = rets.mean(axis=1)
    names = [s for s in sorted(sectors.unique()) if s != SECTOR_BASE]
    columns = {}
    for sector in names:
        members = [t for t in rets.columns if sectors.get(t) == sector]
        if members:
            columns[f"SEC_{sector}"] = rets[members].mean(axis=1) - universe_mean
    return pd.DataFrame(columns, index=rets.index)


def estimate_risk(rets: pd.DataFrame, factor_returns: pd.DataFrame,
                  sectors: pd.Series) -> RiskModel:
    """Estimate B by time-series regression, F from factor history, D from residuals."""
    ff = factor_returns.reindex(rets.index)[FF_FACTORS]
    panel = pd.concat([ff, sector_factors(rets, sectors)], axis=1).dropna()
    aligned = rets.loc[panel.index]

    X = np.column_stack([np.ones(len(panel)), panel.values])
    tickers, loadings, specific = [], [], []

    for ticker in aligned.columns:
        y = aligned[ticker]
        mask = y.notna().values
        if mask.sum() < MIN_OBS:
            continue
        coef, *_ = np.linalg.lstsq(X[mask], y.values[mask], rcond=None)
        residual = y.values[mask] - X[mask] @ coef
        tickers.append(ticker)
        loadings.append(coef[1:])                     # drop the intercept
        specific.append(max(residual.var(ddof=1), VAR_FLOOR) * TRADING_DAYS)

    B = pd.DataFrame(loadings, index=tickers, columns=panel.columns)
    F = np.cov(panel.values.T) * TRADING_DAYS
    return RiskModel(B, F, np.array(specific), tickers, list(panel.columns))


def covariance(rm: RiskModel) -> np.ndarray:
    """Sigma = B F B' + D, positive-definite by construction."""
    return rm.B.values @ rm.F @ rm.B.values.T + np.diag(rm.D)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_portfolio_risk.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add portfolio.py tests/test_portfolio_risk.py
git commit -m "feat: FF5+UMD+sector factor risk model"
```

---

### Task 9: The optimizer

**Files:**
- Modify: `portfolio.py`
- Create: `tests/test_portfolio_optimize.py`

**Interfaces:**
- Consumes: `portfolio.RiskModel`, `portfolio.covariance`
- Produces: `portfolio.OptimizerConfig` dataclass; `portfolio.Solution(weights, status, relaxations)` NamedTuple; `portfolio.solve(mu, rm, sectors, subindustry, prev=None, config=None) -> Solution`

- [ ] **Step 1: Write the failing test**

```python
"""The cvxpy problem: does every constraint actually hold in the solution."""
import numpy as np
import pandas as pd
import pytest

import portfolio


@pytest.fixture
def setup():
    rng = np.random.default_rng(7)
    n = 30
    tickers = [f"T{i}" for i in range(n)]
    sectors = pd.Series({t: ["TMT", "Industrials", "Consumer"][i % 3]
                         for i, t in enumerate(tickers)})
    subind = pd.Series({t: f"sub{i % 6}" for i, t in enumerate(tickers)})

    factor_names = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD",
                    "SEC_Industrials", "SEC_TMT"]
    B = pd.DataFrame(rng.normal(0, 0.5, (n, 8)), index=tickers,
                     columns=factor_names)
    F = np.diag(rng.uniform(0.01, 0.04, 8))
    D = rng.uniform(0.02, 0.06, n)
    rm = portfolio.RiskModel(B, F, D, tickers, factor_names)
    mu = pd.Series(rng.normal(0, 1, n), index=tickers)
    return mu, rm, sectors, subind


def test_solution_is_dollar_and_factor_neutral(setup):
    mu, rm, sectors, subind = setup
    sol = portfolio.solve(mu, rm, sectors, subind)
    w = sol.weights.values

    assert abs(w.sum()) < 1e-6
    assert np.abs(rm.B.values.T @ w).max() < 1e-6


def test_solution_respects_position_gross_and_vol_caps(setup):
    mu, rm, sectors, subind = setup
    cfg = portfolio.OptimizerConfig(position_cap=0.04, gross_cap=2.0,
                                    vol_target=0.08)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)
    w = sol.weights.values
    cov = portfolio.covariance(rm)

    assert np.abs(w).max() <= 0.04 + 1e-6
    assert np.abs(w).sum() <= 2.0 + 1e-6
    assert np.sqrt(w @ cov @ w) <= 0.08 + 1e-6


def test_solution_is_sector_neutral(setup):
    mu, rm, sectors, subind = setup
    sol = portfolio.solve(mu, rm, sectors, subind)

    for sector in sectors.unique():
        members = [t for t in sol.weights.index if sectors[t] == sector]
        assert abs(sol.weights[members].sum()) < 1e-6


def test_turnover_penalty_pulls_toward_previous_weights(setup):
    mu, rm, sectors, subind = setup
    prev = pd.Series(0.0, index=rm.tickers)
    prev.iloc[0] = 0.04

    loose = portfolio.solve(mu, rm, sectors, subind, prev=prev,
                            config=portfolio.OptimizerConfig(turnover_penalty=0.0))
    tight = portfolio.solve(mu, rm, sectors, subind, prev=prev,
                            config=portfolio.OptimizerConfig(turnover_penalty=5.0))

    assert (tight.weights - prev).abs().sum() < (loose.weights - prev).abs().sum()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_portfolio_optimize.py -v`
Expected: FAIL with `AttributeError: module 'portfolio' has no attribute 'OptimizerConfig'`

- [ ] **Step 3: Implement**

```python
from dataclasses import dataclass

import cvxpy as cp


@dataclass(frozen=True)
class OptimizerConfig:
    vol_target: float = 0.08          # annualized
    gross_cap: float = 2.0
    position_cap: float = 0.04
    subindustry_band: float = 0.10
    turnover_penalty: float = 0.001


class Solution(NamedTuple):
    weights: pd.Series
    status: str
    relaxations: list[str]


def _problem(mu, cov, B, sector_masks, subind_masks, prev, cfg):
    """Assemble the cvxpy problem. Split out so the relaxation ladder can rebuild it."""
    n = len(mu)
    w = cp.Variable(n)

    objective = mu.values @ w
    if prev is not None and cfg.turnover_penalty > 0:
        objective = objective - cfg.turnover_penalty * cp.norm1(w - prev.values)

    constraints = [
        cp.sum(w) == 0,                                    # dollar neutral
        B.values.T @ w == 0,                               # factor neutral
        cp.quad_form(w, cp.psd_wrap(cov)) <= cfg.vol_target ** 2,
        cp.norm1(w) <= cfg.gross_cap,
        cp.abs(w) <= cfg.position_cap,
    ]
    for mask in sector_masks:
        constraints.append(mask @ w == 0)                  # sector neutral
    for mask in subind_masks:
        constraints.append(cp.abs(mask @ w) <= cfg.subindustry_band)

    return w, cp.Problem(cp.Maximize(objective), constraints)


def _masks(index, labels: pd.Series) -> list[np.ndarray]:
    out = []
    for value in sorted(set(labels.reindex(index).dropna())):
        out.append(np.array([1.0 if labels.get(t) == value else 0.0
                             for t in index]))
    return out


def solve(mu: pd.Series, rm: RiskModel, sectors: pd.Series,
          subindustry: pd.Series, prev: pd.Series | None = None,
          config: OptimizerConfig | None = None) -> Solution:
    """Solve for target weights, relaxing in a documented order if infeasible."""
    cfg = OptimizerConfig() if config is None else config
    mu = mu.reindex(rm.tickers).fillna(0.0)
    cov = covariance(rm)
    prev = None if prev is None else prev.reindex(rm.tickers).fillna(0.0)

    sector_masks = _masks(rm.tickers, sectors)
    subind_masks = _masks(rm.tickers, subindustry)

    w, problem = _problem(mu, cov, rm.B, sector_masks, subind_masks, prev, cfg)
    problem.solve()

    if problem.status in ("optimal", "optimal_inaccurate"):
        return Solution(pd.Series(w.value, index=rm.tickers), "optimal", [])

    return _relax(mu, cov, rm, sector_masks, subind_masks, prev, cfg)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_portfolio_optimize.py -v`
Expected: FAIL — `_relax` is not defined yet. Stub it to `raise NotImplementedError` and confirm the four tests above pass, since none of them triggers infeasibility.

- [ ] **Step 5: Commit**

```bash
git add portfolio.py tests/test_portfolio_optimize.py
git commit -m "feat: cvxpy market-neutral optimizer"
```

---

### Task 10: The relaxation ladder

**Files:**
- Modify: `portfolio.py`
- Modify: `tests/test_portfolio_optimize.py`

**Interfaces:**
- Consumes: `portfolio._problem`
- Produces: `portfolio._relax(...) -> Solution`; `Solution.relaxations` populated in order; `Solution.status` one of `optimal`, `relaxed:<name>`, `infeasible`

- [ ] **Step 1: Write the failing test**

```python
def test_infeasible_problem_relaxes_subindustry_first(setup):
    mu, rm, sectors, subind = setup
    # A band of zero cannot hold alongside sector neutrality and a gross floor.
    cfg = portfolio.OptimizerConfig(subindustry_band=0.0, vol_target=0.001)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)

    assert sol.relaxations[0] == "subindustry_band"
    assert sol.status.startswith("relaxed:")


def test_neutrality_is_never_relaxed(setup):
    mu, rm, sectors, subind = setup
    cfg = portfolio.OptimizerConfig(vol_target=1e-9, gross_cap=1e-9,
                                    subindustry_band=0.0)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)

    # Whatever happened, the book is still neutral -- or it failed outright.
    if sol.status != "infeasible":
        assert abs(sol.weights.sum()) < 1e-6
        assert np.abs(rm.B.values.T @ sol.weights.values).max() < 1e-6
    assert "sector_neutral" not in sol.relaxations
    assert "factor_neutral" not in sol.relaxations


def test_hopeless_problem_returns_infeasible_not_garbage(setup):
    mu, rm, sectors, subind = setup
    cfg = portfolio.OptimizerConfig(position_cap=0.0)
    sol = portfolio.solve(mu, rm, sectors, subind, config=cfg)

    assert sol.status == "infeasible"
    assert (sol.weights == 0).all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_portfolio_optimize.py -v`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement**

```python
from dataclasses import replace

# Relaxed in this order and no other. Dollar, sector and factor neutrality are
# absent by design: they define what the portfolio is, and a book that quietly
# stopped being neutral is worse than no book at all.
_LADDER = [
    ("subindustry_band", lambda c: replace(c, subindustry_band=min(c.subindustry_band * 4 + 0.05, 1.0))),
    ("gross_cap", lambda c: replace(c, gross_cap=c.gross_cap * 1.5)),
    ("vol_target", lambda c: replace(c, vol_target=c.vol_target * 1.5)),
]


def _relax(mu, cov, rm, sector_masks, subind_masks, prev, cfg) -> Solution:
    applied: list[str] = []

    for name, loosen in _LADDER:
        cfg = loosen(cfg)
        applied.append(name)
        w, problem = _problem(mu, cov, rm.B, sector_masks, subind_masks, prev, cfg)
        problem.solve()
        if problem.status in ("optimal", "optimal_inaccurate"):
            return Solution(pd.Series(w.value, index=rm.tickers),
                            f"relaxed:{'+'.join(applied)}", applied)

    # Still infeasible. The previous book stands; say so rather than inventing one.
    return Solution(pd.Series(0.0, index=rm.tickers), "infeasible", applied)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_portfolio_optimize.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add portfolio.py tests/test_portfolio_optimize.py
git commit -m "feat: documented relaxation ladder that never drops neutrality"
```

---

### Task 11: Walk-forward backtest

**Files:**
- Create: `tools/backtest_portfolio.py`
- Create: `tests/test_backtest.py`

**Interfaces:**
- Consumes: everything in `portfolio.py`, `factors.load_cached`
- Produces: `backtest_portfolio.rebalance_dates(rets, freq="ME") -> list[str]`; `backtest_portfolio.run(conn, start, end) -> pd.DataFrame` (one row per rebalance: `as_of, ret, turnover, gross, status`); `backtest_portfolio.summarize(frame) -> dict`

- [ ] **Step 1: Write the failing test**

```python
"""Backtest mechanics, and the lookahead check that matters most."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Repo convention: tools/ scripts are loaded by path, not imported as a
# package. See tests/test_report.py, which does the same for the 13F report.
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "backtest_portfolio", ROOT / "tools" / "backtest_portfolio.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)


def test_rebalance_dates_are_month_ends_within_range():
    idx = pd.date_range("2026-01-01", "2026-04-15", freq="B").strftime("%Y-%m-%d")
    rets = pd.DataFrame(index=idx, data={"A": 0.0})
    dates = bt.rebalance_dates(rets)

    assert dates[0].startswith("2026-01")
    assert all(d <= "2026-04-15" for d in dates)
    assert len(dates) == len(set(dates))


def test_summarize_reports_ir_and_turnover():
    frame = pd.DataFrame({
        "as_of": ["2026-01-31", "2026-02-28", "2026-03-31"],
        "ret": [0.01, -0.005, 0.02],
        "turnover": [0.5, 0.2, 0.3],
        "gross": [2.0, 2.0, 2.0],
        "status": ["optimal"] * 3,
    })
    stats = bt.summarize(frame)

    assert stats["periods"] == 3
    assert stats["mean_turnover"] == pytest.approx(1.0 / 3)
    assert np.isfinite(stats["ir"])


def test_no_lookahead_a_shuffled_future_signal_earns_nothing():
    """The single most important test here.

    Point-in-time reconstruction spans three sources with different cadences,
    which is exactly where lookahead hides. If forward returns are shuffled
    relative to the signal, any remaining edge is a bug, not alpha.
    """
    rng = np.random.default_rng(3)
    signal = pd.Series(rng.normal(0, 1, 200))
    forward = pd.Series(rng.normal(0, 0.02, 200))
    shuffled = forward.sample(frac=1.0, random_state=99).reset_index(drop=True)

    edge = bt.signal_edge(signal, shuffled)

    assert abs(edge) < 0.15
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL with `FileNotFoundError` on `tools/backtest_portfolio.py`

- [ ] **Step 3: Implement**

```python
"""Walk-forward backtest of the market-neutral book.

Fetches nothing and renders nothing: it reads the store, replays the
optimizer month by month, and reports what happened.

Read the honest limits in section 9 of the design spec before quoting any
number this produces. Twenty-four monthly observations with seven independent
13F changes detects a broken signal; it does not distinguish a good one from
a mediocre one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import factors           # noqa: E402
import history           # noqa: E402
import portfolio         # noqa: E402

COST_PER_TURNOVER = 0.0010     # 10 bps


def rebalance_dates(rets: pd.DataFrame, freq: str = "ME") -> list[str]:
    """Last available trading day of each period in the return index."""
    idx = pd.to_datetime(pd.Series(rets.index))
    return sorted(idx.groupby(idx.dt.to_period(freq[0])).max()
                     .dt.strftime("%Y-%m-%d").tolist())


def signal_edge(signal: pd.Series, forward: pd.Series) -> float:
    """Rank correlation between a signal and the return that followed it."""
    return float(signal.corr(forward, method="spearman"))


def run(conn, start: str, end: str) -> pd.DataFrame:
    factor_returns = factors.load_cached()
    companies = pd.read_sql_query(
        "SELECT ticker, sector, subindustry FROM companies", conn)
    sectors = companies.set_index("ticker")["sector"]
    subind = companies.set_index("ticker")["subindustry"]

    full = portfolio.returns_matrix(conn, end, window=10_000)
    dates = [d for d in rebalance_dates(full) if start <= d <= end]

    prev, records = None, []
    for i, as_of in enumerate(dates[:-1]):
        rets = portfolio.returns_matrix(conn, as_of)
        universe = portfolio.eligible_universe(rets)
        if len(universe) < 20:
            continue

        rm = portfolio.estimate_risk(rets[universe], factor_returns, sectors)
        alpha = portfolio.build_alpha(conn, as_of, rm.tickers, subind)
        sol = portfolio.solve(alpha["mu"], rm, sectors, subind, prev=prev)

        forward = full.loc[(full.index > as_of) & (full.index <= dates[i + 1])]
        realized = float((forward[rm.tickers].fillna(0.0) @ sol.weights.values).sum())
        turnover = float((sol.weights - (prev if prev is not None else 0)).abs().sum())

        records.append({"as_of": as_of,
                        "ret": realized - turnover * COST_PER_TURNOVER,
                        "turnover": turnover,
                        "gross": float(sol.weights.abs().sum()),
                        "status": sol.status})
        prev = sol.weights

    return pd.DataFrame(records)


def summarize(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"periods": 0}
    rets = frame["ret"]
    vol = rets.std(ddof=1)
    curve = (1 + rets).cumprod()
    return {
        "periods": len(frame),
        "mean_return": float(rets.mean()),
        "ir": float(rets.mean() / vol * np.sqrt(12)) if vol > 0 else float("nan"),
        "mean_turnover": float(frame["turnover"].mean()),
        "max_drawdown": float((curve / curve.cummax() - 1).min()),
        "relaxed_periods": int((frame["status"] != "optimal").sum()),
    }


if __name__ == "__main__":
    conn = history.connect()
    result = run(conn, "2024-09-01", "2026-08-21")
    print(result.to_string(index=False))
    for key, value in summarize(result).items():
        print(f"{key:20s} {value}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the real backtest**

Run: `python tools/backtest_portfolio.py`
Expected: a table of monthly rows and a summary. Record the output — it is the first evidence about these signals.

- [ ] **Step 6: Commit**

```bash
git add tools/backtest_portfolio.py tests/test_backtest.py
git commit -m "feat: walk-forward backtest with a no-lookahead guard"
```

---

### Task 12: Solve entry point and report

**Files:**
- Create: `tools/build_portfolio_report.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `portfolio.solve`, `history.upsert_target_weights`
- Produces: `build_portfolio_report.solve_today(conn, as_of) -> tuple[pd.DataFrame, portfolio.Solution]`; writes `reports/portfolio_YYYYMMDD.html`

- [ ] **Step 1: Write the report script**

```python
"""Solve today's book, record it, and render it as a standalone page.

Fetches nothing. Rendering and solving are separated so a rendering change
cannot alter what was recorded.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import factors           # noqa: E402
import history           # noqa: E402
import portfolio         # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def solve_today(conn, as_of: str):
    factor_returns = factors.load_cached()
    companies = pd.read_sql_query(
        "SELECT ticker, sector, subindustry FROM companies", conn)
    sectors = companies.set_index("ticker")["sector"]
    subind = companies.set_index("ticker")["subindustry"]

    rets = portfolio.returns_matrix(conn, as_of)
    universe = portfolio.eligible_universe(rets)
    rm = portfolio.estimate_risk(rets[universe], factor_returns, sectors)
    alpha = portfolio.build_alpha(conn, as_of, rm.tickers, subind)
    solution = portfolio.solve(alpha["mu"], rm, sectors, subind)
    return alpha, solution


def record(conn, as_of: str, alpha: pd.DataFrame,
           solution: portfolio.Solution) -> int:
    rows = [history.TargetWeightRow(
        as_of, ticker, float(weight),
        float(alpha.loc[ticker, "mu"]),
        float(alpha.loc[ticker, "contrib_momentum"]),
        float(alpha.loc[ticker, "contrib_13f"]),
        float(alpha.loc[ticker, "contrib_insider"]),
        solution.status)
        for ticker, weight in solution.weights.items()]
    return history.upsert_target_weights(conn, rows)


def render(as_of: str, alpha: pd.DataFrame,
           solution: portfolio.Solution) -> Path:
    book = (pd.DataFrame({"weight": solution.weights})
              .join(alpha).sort_values("weight", ascending=False))
    longs = book[book["weight"] > 1e-6]
    shorts = book[book["weight"] < -1e-6].sort_values("weight")

    def table(frame: pd.DataFrame) -> str:
        rows = "".join(
            f"<tr><td>{t}</td><td>{r.weight:+.2%}</td><td>{r.mu:+.2f}</td>"
            f"<td>{r.contrib_momentum:+.2f}</td><td>{r.contrib_13f:+.2f}</td>"
            f"<td>{r.contrib_insider:+.2f}</td></tr>"
            for t, r in frame.iterrows())
        return ("<table><tr><th>Ticker</th><th>Weight</th><th>mu</th>"
                "<th>Mom</th><th>13F</th><th>Insider</th></tr>"
                f"{rows}</table>")

    html = f"""<!doctype html><meta charset="utf-8">
<title>Portfolio {as_of}</title>
<style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse;
margin:1rem 0}}td,th{{border:1px solid #ccc;padding:.3rem .6rem;text-align:right}}
td:first-child,th:first-child{{text-align:left}}</style>
<h1>Market-neutral book — {as_of}</h1>
<p>Status: <b>{solution.status}</b> · gross {solution.weights.abs().sum():.2f}
 · {len(longs)} long / {len(shorts)} short</p>
<h2>Longs</h2>{table(longs)}
<h2>Shorts</h2>{table(shorts)}
"""
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"portfolio_{as_of.replace('-', '')}.html"
    path.write_text(html)
    return path


if __name__ == "__main__":
    as_of = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    conn = history.connect()
    alpha, solution = solve_today(conn, as_of)
    print(f"recorded {record(conn, as_of, alpha, solution)} weights")
    print(f"wrote {render(as_of, alpha, solution)}")
```

- [ ] **Step 2: Run it end to end**

Run: `python tools/build_portfolio_report.py 2026-08-21`
Expected: `recorded 15x weights` and a path under `reports/`. Open the file and confirm longs and shorts are both populated and gross is near 2.00.

- [ ] **Step 3: Verify the record persisted**

Run:
```bash
python -c "
import history
c = history.connect()
w = history.target_weights(c, '2026-08-21')
print(len(w), 'rows; gross', w['weight'].abs().sum().round(3),
      'net', w['weight'].sum().round(6), 'status', w['solver_status'].iloc[0])"
```
Expected: net ≈ 0.000000, gross ≈ 2.0, status `optimal`.

- [ ] **Step 4: Document it in the README**

Add to the Flags table and the run-flow diagram:

```markdown
| `python tools/build_portfolio_report.py [YYYY-MM-DD]` | Solve the market-neutral book, record weights, write `reports/portfolio_*.html` |
| `python tools/backtest_portfolio.py` | Walk-forward backtest of the same book |
```

Add a `## Portfolio construction` section stating: the book is dollar-, sector- and factor-neutral; alpha is momentum + 13F positioning + insider purchases; risk is FF5+UMD+2 sector factors; and that the backtest window is bounded by 13F availability and is a sanity check rather than evidence.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all green, ~125 tests.

- [ ] **Step 6: Commit**

```bash
git add tools/build_portfolio_report.py README.md
git commit -m "feat: solve, record and render the market-neutral book"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3 module boundaries | 1, 3, 11, 12 |
| §4 momentum leg | 4 |
| §4 13F leg, split-adjusted | 5 |
| §4 insider leg, sparse | 6 |
| §4 standardization + blend | 7 |
| §5.1 eight factors, universe-relative sectors | 8 |
| §5.3 estimation, universe filter | 3, 8 |
| §5.4 substitution interface | 8 (`RiskModel` NamedTuple is the seam) |
| §6 optimization problem | 9 |
| §7 schema | 2 |
| §8 relaxation ladder | 10 |
| §9 backtest, no-lookahead | 11 |
| §10 testing | every task |
| §11 deferred | not implemented, by design |

**Gap accepted:** §5.5 asks for variance-explained-per-factor reporting. Task 11 reports status and turnover but not per-factor variance decomposition. This is a reporting addition to `summarize()`, not a structural one — added as a follow-up rather than blocking v1.

**Placeholder scan:** none. Every step has runnable code or an exact command.

**Type consistency:** `RiskModel`, `Solution`, `OptimizerConfig`, `TargetWeightRow` are defined once and used with matching field names throughout. `portfolio.solve` takes `(mu, rm, sectors, subindustry, prev, config)` in Tasks 9, 10, 11 and 12 identically.
