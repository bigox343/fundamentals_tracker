# fundamentals_tracker

A daily fundamentals dashboard and history store for 148 bellwether names across
TMT, Industrials, and Consumer.

Each run pulls live fundamentals from Yahoo Finance, scores every metric against
**sub-industry** peers (semis vs semis, infra software vs infra software), renders
a single self-contained `dashboard.html`, and appends the day's observations to a
durable history in `data/`.

The dashboard answers "how does this company look against its peers today". The
history store exists so that, later, it can answer "what changed, and was it the
multiple or the estimates".

---

## Quickstart

```bash
python build_dashboard.py      # fetch live, write dashboard.html, record history
open dashboard.html
```

Runtime dependencies are `pandas` and `yfinance` (verified against yfinance
1.4.1). There is no runtime `requirements.txt` — only `requirements-dev.txt`,
which pins `pytest>=8.0` for the test suite.

### Flags

| Command | Effect |
|---|---|
| `python build_dashboard.py` | Fetch live, write `dashboard.html`, record the day to `data/` |
| `python build_dashboard.py --no-fetch` | Re-render from the newest cached CSV. No network, no new history. |
| `python build_dashboard.py --rebuild-history` | Drop `history.db` and reconstruct it from the raw CSVs, then exit |

Only one instance runs at a time — `build_dashboard.py` takes an `fcntl` lock on
`.run.lock` itself, because macOS ships no `flock(1)`.

### Tests

```bash
python -m pytest tests/ -q
```

96 tests, no network. Every parsing function is exercised against captured
fixtures in [tests/fixtures/](tests/fixtures/), which is why `extract.py` keeps
its network-touching helpers confined to the bottom of the module.

---

## How a run works

```
run_daily.sh
  └── build_dashboard.py
        ├── fetch_all()            fundamentals for 148 tickers
        ├── attach_history()       1y closes (YTD/1Y returns need the full year)
        │     └── data/fundamentals_YYYYMMDD.csv     (written, never pruned)
        ├── extract.collect_estimates()
        │     └── data/estimates_YYYYMMDD.csv        (written, never pruned)
        ├── fetch_closes(period=5y)
        ├── record_history()  ──►  data/history.db
        └── render_html()     ──►  dashboard.html
```

`record_history()` is deliberately non-fatal: a store failure must not cost you
the dashboard.

### Modules

| File | Knows about | Deliberately does not know about |
|---|---|---|
| [build_dashboard.py](build_dashboard.py) | The universe, scoring, HTML rendering, the run flow | — |
| [extract.py](extract.py) | yfinance frame shapes, fiscal calendars | SQL, HTML |
| [history.py](history.py) | SQLite | yfinance, HTML |

`MetricRow` is defined in `history.py` and imported by `extract.py` — it is the
shared contract between producer and store. Importing a `NamedTuple` is not
knowledge of SQL.

---

## The data

### `data/` is the record of truth

The dated CSVs are plain text and are **never pruned**. `history.db` is derived
from them and therefore disposable — a corrupted or deleted database costs a
`--rebuild-history`, not data.

This matters because the two datasets have opposite urgency. Statements, closes
and share counts are **recoverable**: equally available in six months. Forward
estimates, analyst dispersion and revision breadth are **perishable** — only
`eps_trend` carries any history at all (~90 days), and every observation not
taken is lost permanently. That asymmetry is also why the cadence is daily:
daily can always be downsampled to weekly, weekly can never be upsampled to daily.

One exception to the rebuild guarantee: **daily closes are not restored** by
`--rebuild-history`, because no CSV holds them — unlike estimates they can be
refetched in full at any time. A rebuilt store is complete in the perishable data
and empty of prices until the next normal run. `report['prices']` reports the
shortfall so the gap cannot be mistaken for data loss.

### Schema

Everything lands in one observation table, keyed so that nothing can silently
overwrite anything else:

```sql
metrics (ticker, as_of, period_type, metric, ref_period, value, ingested_at)
PRIMARY KEY (ticker, as_of, period_type, metric, ref_period)
```

`period_type` is one of `snapshot` | `daily` | `estimate` | `quarter` | `annual`.
`ref_period` carries the absolute fiscal period for estimates and is `''`
otherwise. Alongside it sit `companies` (name, sector, sub-industry) and `runs`,
which records each run's status and ticker counts — without it, a failed run, a
market holiday and a delisting all look identical in the data: an absent row.

### Three invariants worth knowing before you edit

These are each load-bearing, and each was learned from live data rather than
reasoned about in advance. The code comments carry the full evidence.

**Estimates store an absolute `ref_period`, never a relative label.** Yahoo's
horizons are relative (`0y`, `+1q`), so storing by label renders a fiscal-year
rollover as an enormous fake revision. Horizons resolve forward from the last
*reported* period, not from `as_of` — Yahoo's `0q` is the quarter that reports
next, which has usually already ended. Labels are prefixed `FY`/`FQ` because a
fiscal year and a fiscal quarter can end on the same day: 3 of 8 probed names
collided, and the annual figure was observed overwriting the quarterly one in
place.

**A missing value writes no row.** "Never observed" must stay distinct from a
genuine zero. This is why non-finite values are dropped at the write boundary,
and why an exact `0.0` in an `eps_trend` lookback column is treated as missing —
Yahoo pads columns it has no data for with `0.0` rather than `NaN`, and stored,
that reads as "consensus was $0.00" and renders the next observation as an
infinite revision.

**CSV reads use `float_precision="round_trip"`.** The default pandas parser is
fast rather than exact, and turns a written `1.9626000000000001` back into
`1.9626`. Small enough to look like nothing, but it means a rebuilt store does not
equal the one it replaced — which is the one property the raw CSVs exist to
guarantee.

---

## Scheduling

[run_daily.sh](run_daily.sh) is the driver, invoked by the LaunchAgent
`com.owen.fundamentals-tracker`. It is safe to run by hand:

```bash
./run_daily.sh
```

It appends to `logs/run.log`, trimming to the last 2000 lines so the log cannot
grow without bound. `logs/` is gitignored. The script hardcodes its interpreter
(`/Users/owen/opt/anaconda3/envs/py312/bin/python3`) because a LaunchAgent does
not inherit a shell environment — change that line if the env moves.

---

## The universe

148 tickers, defined in `UNIVERSE` at the top of
[build_dashboard.py](build_dashboard.py). Edit the lists to taste.

| Sector | Tickers | Sub-industries |
|---|---:|---:|
| TMT (Tech · Media · Telecom) | 80 | 9 |
| Industrials | 36 | 6 |
| Consumer (Staples · Discretionary) | 32 | 5 |

Each sub-industry is benchmarked against the liquid ETF that best stands in for
it (`PROXY_ETFS`), falling back to the peer median where no clean proxy exists.
There is also an SPX section for top-down context.

Heatmap coloring uses a colorblind-safe diverging blue↔red palette — blue is more
favorable than sub-industry peers, red less. The 6M sparklines reuse that same
pair rather than green/red, which fails protanope separation (OKLab ΔE 2.9
against an 8.0 target; blue/red clears it at 20.4).

---

## Design docs

Working documents under [docs/superpowers/](docs/superpowers/):

- [specs/2026-08-15-fundamentals-history-design.md](docs/superpowers/specs/2026-08-15-fundamentals-history-design.md)
  — the history capture design. §6.2 is the schema; §7 is error handling.
- [specs/2026-08-15-probe-findings.md](docs/superpowers/specs/2026-08-15-probe-findings.md)
  — what the yfinance API actually returns, probed against live data.
- [plans/2026-08-15-fundamentals-history-capture.md](docs/superpowers/plans/2026-08-15-fundamentals-history-capture.md)
  — the implementation plan.

The design doc lists five further sub-projects that build on this one: return
attribution (`return = re-rating + revision`), delta badges, statement backfill,
inflection detection, and per-metric drill-down.
