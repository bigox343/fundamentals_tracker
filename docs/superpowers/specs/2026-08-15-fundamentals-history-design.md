# Fundamentals History: Daily Capture and Store

**Date:** 2026-08-15
**Status:** Approved design, ready for implementation planning
**Scope:** Sub-project 1 of 6 — daily capture and the history store

---

## 1. Context

`build_dashboard.py` fetches fundamentals and 1-year price history for 148 tickers
and renders a self-contained `dashboard.html`. It has run exactly once
(`logs/weekly.log`: `run start: 2026-08-15 13:17:04`, 66 seconds). Every metric is
scored against sector peers (`build_dashboard.py:585`) or sub-industry peers
(`:599`). Only the newest CSV is ever read, and `run_weekly.sh` deletes CSVs older
than 365 days.

The tracker therefore has no time dimension at all. The goal is to add one.

## 2. Goals

1. Capture a durable, queryable history of every tracked metric, daily.
2. Capture forward analyst estimates with their reference fiscal period, so that
   price returns can later be decomposed into multiple re-rating vs. estimate
   revision.
3. Preserve the existing dashboard's behaviour unchanged while doing so.

## 3. Non-goals (each is a later sub-project, with its own spec)

| # | Feature | Depends on |
|---|---|---|
| 2 | Return attribution: return = re-rating + revision | this spec |
| 3 | Week-over-week / month-over-month delta badges | this spec |
| 4 | Statement backfill: 5 years of filings | this spec |
| 5 | Inflection detection ("what meaningfully moved") | 3, 4 |
| 6 | Per-metric trend sparklines; per-company drill-down | 4 |

## 4. Why capture comes before backfill

The decomposition target is an identity in logs:

```
P = forwardPE × forwardEPS
ln(P₁/P₀) = ln(fPE₁/fPE₀) + ln(fEPS₁/fEPS₀)
     return  =  re-rating   +   revision
```

Data splits into two classes with opposite urgency:

- **Recoverable.** Statements (5 annual + 5 quarterly periods), daily closes
  (5 years), shares outstanding (234 points, 2021→2026). Equally available in six
  months. Not urgent.
- **Perishable.** Forward estimates, analyst dispersion, revision breadth. Only
  `eps_trend` carries any history (~90 days). Every observation not taken is lost
  permanently. Urgent.

Daily rather than weekly follows from the same asymmetry: estimates are revised
continuously and cluster around earnings and guidance events. Daily can always be
downsampled to weekly; weekly can never be upsampled to daily.

## 5. Data source findings (yfinance 1.4.1, verified 2026-08-15 against NVDA)

All estimate frames are indexed by relative horizon `0q`, `+1q`, `0y`, `+1y`.

| Field | Columns | History |
|---|---|---|
| `eps_trend` | current, 7daysAgo, 30daysAgo, 60daysAgo, 90daysAgo, currency | **~90 days, 5 points** |
| `earnings_estimate` | avg, low, high, yearAgoEps, numberOfAnalysts, growth, currency | current only |
| `revenue_estimate` | avg, low, high, numberOfAnalysts, yearAgoRevenue, growth, currency | current only |
| `eps_revisions` | upLast7days, upLast30days, downLast30days, downLast7Days | current only |
| `growth_estimates` | stockTrend, indexTrend (index includes `LTG`) | current only |
| `quarterly_income_stmt` | 5 periods (~15 months) | recoverable |
| `income_stmt` | 5 periods (5 years) | recoverable |
| `get_shares_full` | 234 points, 2021-02-02 → 2026-05-21 | recoverable |
| `history(period="5y")` | 1255 daily closes | recoverable |

`eps_trend` gives a real 90-day revision series on day one — NVDA's `0y` consensus
moved 8.38 → 8.96, **+6.9%**, over a window for which daily price already exists.
Attribution is therefore computable retrospectively before any new capture.

Backfill depth is **not uniform** and the dashboard must not imply otherwise:

| Metric group | Annual pts | Quarterly pts |
|---|---|---|
| Margins — gross / op / net | 5 | 5 (seasonal) |
| FCF, cash, net debt / EBITDA | 5 | 5 |
| ROE | 5 | 5 (needs annualizing) |
| Rev / EPS growth (YoY) | 4 | **1** |
| Multiples — P/E, EV/EBITDA, P/S | 5 | **2** (TTM-limited) |
| FCF yield | 5 | 2 |
| `forwardPE` | 0 | 0 |
| Price / returns | daily, 5 years | — |

## 6. Design

### 6.1 Storage: plain files are the record, SQLite is the index

Two raw daily artifacts, both append-only and never pruned:

- `data/fundamentals_YYYYMMDD.csv` — the existing wide snapshot, unchanged.
- `data/estimates_YYYYMMDD.csv` — **new**, long format
  (`ticker, horizon, ref_period, field, value`). Four horizons × several fields per
  ticker does not fit the wide one-row-per-ticker layout.

`data/history.db` is derived from these and is always reconstructable. This matters
specifically because estimates are perishable: if the DB were their only home, a
corrupted file would destroy the one dataset that cannot be refetched. Raw CSVs keep
`--rebuild-history` honest and keep the data greppable and backup-able.

Volume: ~2,800 snapshot rows/day → ~1M rows/year, ~30MB/year of CSVs. Neither is a
constraint.

### 6.2 Schema

```sql
CREATE TABLE metrics (
  ticker      TEXT NOT NULL,
  as_of       TEXT NOT NULL,   -- ISO date the value is as-of
  period_type TEXT NOT NULL,   -- see gloss below
  metric      TEXT NOT NULL,   -- 'grossMargin', 'close', 'epsEst', ...
  ref_period  TEXT NOT NULL,   -- absolute fiscal period for estimates; '' otherwise
  value       REAL NOT NULL,
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, as_of, period_type, metric, ref_period)
);

CREATE TABLE companies (
  ticker TEXT PRIMARY KEY, name TEXT, sector TEXT,
  subindustry TEXT, updated_at TEXT
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
  status TEXT, tickers_ok INTEGER, tickers_failed INTEGER
);

CREATE INDEX idx_metric_series ON metrics (ticker, metric, as_of);
CREATE INDEX idx_metric_xsec   ON metrics (metric, as_of);
```

`period_type` values, glossed explicitly because two of them are easy to confuse once
capture is daily:

| Value | Meaning | Ships in |
|---|---|---|
| `snapshot` | the wide fundamentals row as observed on `as_of` | this spec |
| `daily` | close price series (5y backfill + ongoing) | this spec |
| `estimate` | forward consensus for `ref_period`, as observed on `as_of` | this spec |
| `quarter` / `annual` | metrics reconstructed from filings | sub-project 4 |

Two primary-key subtleties, each guarding a silent-corruption bug:

- **`period_type` in the key.** NVDA's fiscal year and its Q4 both end 2026-01-31.
  Both rows are real and hold different numbers. Without `period_type` one would
  overwrite the other and produce a plausible, wrong chart.
- **`ref_period` in the key.** Horizon labels are relative; `0y` means a different
  fiscal year after rollover. Keyed by label alone, a rollover renders as an enormous
  fake revision.

Melting a snapshot yields **19 numeric metrics** per ticker. `name`, `sector`,
`subindustry` go to `companies`; `spark` is not stored, being a rendered artifact of
daily closes the store now holds directly.

Deliberate simplifications: no `source` column (derivable from `period_type`);
missing values write **no row** rather than NULL, so "never observed" stays distinct
from "genuinely zero"; restatements overwrite in place, with `ingested_at` recording
when. No revision history of revisions.

### 6.3 Resolving `ref_period`

`0y` resolves to the fiscal year ending on the next fiscal year-end after `as_of`,
derived from the company's `income_stmt` column dates. NVDA (FY ends Jan 31) on
2026-08-15 → `ref_period = '2027-01-31'`. `+1y` is the following one; `0q`/`+1q` use
`quarterly_income_stmt` dates.

If the fiscal year-end cannot be determined — a new ticker with no statements yet —
the estimate row is **skipped, not guessed**. A wrong reference period corrupts
attribution invisibly; a missing point is merely missing.

### 6.4 Modules

Split on what each module is allowed to know:

| Module | Knows | Does not know |
|---|---|---|
| `history.py` (new) | SQLite: schema, upsert, queries | yfinance, HTML |
| `extract.py` (new) | yfinance frames → long rows | SQL, HTML |
| `build_dashboard.py` | fetch + render, orchestrates | internals of either |

Chosen for testability: `extract.py` takes frames and returns rows (fixture-testable,
no network); `history.py` takes rows and returns rows (in-memory SQLite, no network).

`extract.py` is named for what it does rather than for statement backfill
specifically: in this sub-project it parses the estimate frames and resolves fiscal
periods; in sub-project 4 statement reconstruction is added alongside, behind the same
frames-in-rows-out contract.

```python
connect(path) -> Connection
ensure_schema(conn)
upsert_companies(conn, df)
ingest_snapshot(conn, df, as_of)            -> int
ingest_estimates(conn, rows, as_of)         -> int
ingest_prices(conn, closes)                 -> int
latest_and_prior(conn, metric, window)      -> DataFrame[ticker, latest, prior, delta]
series(conn, ticker, metric, period_types)  -> DataFrame
coverage(conn)                              -> DataFrame
```

`latest_and_prior` ships here as tested read API with no caller; sub-project 3 adds
the delta badges that consume it. Building it now costs little and keeps the store's
query surface settled in one pass.

### 6.5 Cadence

| Data | Cadence | Rationale |
|---|---|---|
| Price, returns | daily | bulk call; left side of the identity |
| Forward estimates, multiples | daily | unrecoverable if missed |
| Statement-derived fundamentals | staleness-gated *(sub-project 4)* | only move at filings |

Staleness gating: a ticker's statements refresh when its newest quarterly row is
older than ~100 days. Across 148 names that averages **~10 refreshes/week** rather
than 148, picks up new filings automatically, and needs no separate schedule. New
tickers have zero rows and backfill on first sight.

### 6.6 Run flow

Steps 1–3 are today's behaviour, untouched:

1. `run_weekly.sh` → `build_dashboard.py`
2. `fetch_all()` + `attach_history()` → `df`
3. write `data/fundamentals_YYYYMMDD.csv`
4. **new:** fetch estimate frames → write `data/estimates_YYYYMMDD.csv`
5. **new:** `ensure_schema` → `upsert_companies` → `ingest_snapshot` → `ingest_estimates`
6. **new:** `ingest_prices` — 5-year pull on first run, incremental thereafter
7. render (unchanged this sub-project)
8. record the run in `runs`

Statement backfill is **not** part of this run flow. It arrives in sub-project 4 as an
additional step between 6 and 7; the cadence described in §6.5 is stated there so the
schema and staleness column are designed for it now, not so it ships now.

Changes to `run_weekly.sh`: remove the `-mtime +365 -delete` prune; add `flock` to
prevent overlapping runs; rename to reflect daily cadence. The LaunchAgent
`com.owen.fundamentals-tracker` moves from weekly to daily.

New flag, with existing `--no-fetch` unchanged:

- `--rebuild-history` — drop the DB and rebuild it from the raw CSVs on disk

(`--backfill`, forcing a full statement pass, arrives with sub-project 4.)

**Runtime:** estimate frames add requests per ticker; with the existing
`time.sleep(0.3)` politeness delay (~44s of the current 66s), expect ~3–5 minutes per
daily run.

## 7. Error handling

Posture matches `fetch_all()` today, which never aborts on one bad ticker.

| Failure | Response |
|---|---|
| One ticker fails | Log, skip, continue. A partial day beats no day. |
| Whole run fails | **No rows written.** Never carry-forward or zero-fill; gaps render as gaps. |
| `ref_period` unresolvable | Skip the estimate row. Fail closed on ambiguity. |
| Non-finite / absurd value | Reject NaN and inf always. Per-metric bounds only where genuinely definable — negative margins and negative net debt are legitimate. |
| Overlapping runs | `flock` in the driver; WAL mode and `busy_timeout` on the connection. |

The `runs` table makes gaps diagnosable: a failed run, a market holiday, and a
delisting are otherwise indistinguishable.

## 8. Testing

The project has no tests. Scope here is the store and the reconstruction logic —
where silent corruption lives — not the renderer. No network; fixtures captured from
the real frames in §5.

**`history.py`**
- Upsert idempotency: same row twice → one row, `ingested_at` updated.
- PK collisions coexist: annual vs. quarterly on one date; two `ref_period`s on one
  `as_of`.
- Window delta math over a daily series.
- `--rebuild-history` from raw CSVs reproduces an equivalent store.

**`extract.py`**
- Fixture estimate frames → expected long rows.
- `ref_period` resolution from fiscal year-end, **including the rollover boundary**.
- Unresolvable fiscal year-end → row skipped, not guessed.

**Attribution invariants** (validated here, consumed by sub-project 2)
- Price doubles, EPS flat → re-rating 100%, revision 0.
- Price and EPS both double → revision 100%, re-rating 0.
- The log identity sums to total return within tolerance.

## 9. Decisions made

| Decision | Rationale |
|---|---|
| SQLite over Parquet | Idempotent upsert and provenance are the hard parts; stdlib `sqlite3`, no new dependency |
| Raw CSVs remain the record | Keeps the DB derived and rebuildable, which perishable estimate data requires |
| Daily over weekly | Estimate observations are unrecoverable; daily downsamples, weekly cannot upsample |
| Both annual and quarterly backfill | 5-year arc plus recent detail; same fetch, only reconciliation logic added |
| Capture before backfill | Filings keep, estimates do not |
| New modules, not extensions | `build_dashboard.py` is 1,226 lines and already mixes fetch, compute, and render |

## 10. Open items for implementation planning

- Fiscal-period resolution needs verifying on a **non-calendar-fiscal-year, non-US
  name** in the universe; NVDA was the only ticker probed.
- Confirm whether `eps_trend`'s 7/30/60/90-day columns are calendar or trading days
  before dating the backfilled points.
- Decide whether daily snapshot CSVs should be tracked in git or ignored (~30MB/year).
