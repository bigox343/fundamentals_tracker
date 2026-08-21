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
| `python build_dashboard.py --rebuild-13f` | Re-ingest 13F from `data/13f_*.csv.gz`. No network. |
| `python build_dashboard.py --rebuild-history` | Drop `history.db` and reconstruct it from the raw CSVs, then exit |

Only one instance runs at a time — `build_dashboard.py` takes an `fcntl` lock on
`.run.lock` itself, because macOS ships no `flock(1)`.

### Tests

```bash
python -m pytest tests/ -q
```

152 tests, no network. Every parsing function is exercised against captured
fixtures in [tests/fixtures/](tests/fixtures/), which is why `extract.py` and
`edgar.py` both keep their network-touching helpers confined to the bottom of
the module.

---

## How a run works

```
run_daily.sh
  └── build_dashboard.py
        ├── fetch_all()            fundamentals for 148 tickers
        ├── attach_history()       1y closes (YTD/1Y returns need the full year)
        │     └── data/fundamentals_YYYYMMDD.csv     (written, never pruned)
        ├── extract.collect_estimates()
        │     └── data/estimates_YYYYMMDD.csv.gz     (written, never pruned)
        ├── fetch_13f()            quarterly; usually makes no request at all
        │     └── data/13f_YYYYQN.csv.gz             (written, never pruned)
        ├── fetch_closes(period=5y)
        ├── record_history()  ──►  data/history.db
        └── render_html()     ──►  dashboard.html
```

`record_history()` is deliberately non-fatal: a store failure must not cost you
the dashboard.

### Modules

| File | Knows about | Deliberately does not know about |
|---|---|---|
| [build_dashboard.py](build_dashboard.py) | The universe, the fund roster, scoring, HTML rendering, the run flow | — |
| [extract.py](extract.py) | yfinance frame shapes, fiscal calendars | SQL, HTML |
| [edgar.py](edgar.py) | SEC EDGAR, 13F information tables | SQL, HTML, the universe |
| [history.py](history.py) | SQLite | yfinance, EDGAR, HTML |
| [visualize.py](visualize.py) | Rendering the store as `history.html` | Fetching anything |

`MetricRow` is defined in `history.py` and imported by `extract.py` — it is the
shared contract between producer and store. Importing a `NamedTuple` is not
knowledge of SQL.

---

## The data

### `data/` is the record of truth

The dated CSVs are gzipped text and are **never pruned**. They compress 5.9x
blended (estimates 8.8x, insiders 6.3x, fundamentals only 2.2x), which takes
`data/` from 433 MB/yr to 73 MB/yr.

Compressing rather than pruning is deliberate. Deleting old dated files would
not lose data today — it is all in `history.db` — but it would delete the
*rebuild path*, inverting the guarantee below: pruned, the database becomes the
only copy of the perishable estimate history. Files written before the switch
are still read, so no migration is required. `history.db` is derived
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

## 13F hedge-fund holdings

27 marquee managers — the Tiger lineage, the multi-strats, and a
concentrated/activist sleeve — pulled from their own 13F-HR filings on SEC
EDGAR and joined into the universe. Both the per-company drilldown and a
fund-first view live in `history.html`.

This is a different dataset from the `holdings` table, despite the similar
shape. `holdings` is Yahoo's top-10 list per ticker, which is BlackRock,
Vanguard and State Street on every name and contains **no hedge fund at all**.
13F is filed per *manager*, so it inverts the grain of every other fetch here:
you pull N funds and join in, rather than pulling N tickers.

`FUNDS` in [build_dashboard.py](build_dashboard.py) pins each manager's CIK.
Pinning is not fussiness — a wrong CIK does not error, it returns perfectly
parseable XML from the wrong book. Three of the first 28 resolved by
company-name search were wrong and looked fine, including a Baupost shell whose
last 13F was filed in **2002**. `edgar.STALE_QUARTERS` catches the rest.

### Four things that silently corrupt this data

Each was found against live filings, not reasoned about in advance:

| | What happens if you miss it |
|---|---|
| Filers differ on XML namespaces (`<n1:infoTable>` vs `<infoTable>`) | A regex parser returns **zero rows** for half your funds, which reads as "holds nothing" rather than as an error |
| Nearly half of a multi-strat's lines are options (Citadel 47%) | Positions overstated roughly 2x |
| Convertible-bond CUSIPs match the issuer name | A fund's SNOW convert counts as SNOW equity |
| Share counts are as-filed; stored closes are back-adjusted | A 25:1 split renders as the manager **adding 2,400%** |

### The CUSIP map

13F carries a CUSIP and an issuer name but never a ticker, and CUSIP is
licensed, so there is no free authoritative mapping. `data/cusip_map.csv` is
built from the filings themselves and then *proved*: a candidate pair is
accepted only when the price implied by the filers, `median(value / shares)`,
matches the ticker's close in `history.db`. Ten managers independently agreeing
that NVDA was $200.09 on 2026-06-30 is strong evidence, and the same check
catches filers reporting thousands rather than dollars, since they miss by
exactly 1000x. 148/148 names resolve at a median price error of **0.0000**.

Rebuild it with `python tools/build_cusip_map.py` after editing `UNIVERSE`,
then `python build_dashboard.py --rebuild-13f`. Neither touches the network.

### Reading the page

The **% of book** column is the one that matters. It is the position against
that manager's entire reported equity book, and it separates conviction from
flow without special-casing: Altimeter's NVDA is 19.2% of book, Citadel's is
1.4%. Cohort labels reinforce it.

An **absent position is an exit**, which is why `thirteenf_filings` records
every filing that was successfully ingested. Without it a network failure and a
liquidation are the same absence. Pershing Square had not filed Q2 2026 as of
2026-08-20; its Q1 positions correctly render as nothing at all rather than as
six fabricated exits.

13F is long US equity only — no shorts, no swaps — and is filed 45 days after
quarter end, so it is always a lagged picture.

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
- [specs/2026-08-20-13f-holdings-design.md](docs/superpowers/specs/2026-08-20-13f-holdings-design.md)
  — the 13F design, including the evidence probed from live EDGAR and the
  reasoning behind gzipping the dated CSVs rather than pruning them.

The design doc lists five further sub-projects that build on this one: return
attribution (`return = re-rating + revision`), delta badges, statement backfill,
inflection detection, and per-metric drill-down.
