# fundamentals_tracker

A daily fundamentals dashboard and history store for 153 bellwether names across
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
| `python tools/build_portfolio_report.py [YYYY-MM-DD]` | Solve the market-neutral book, record weights, write `reports/portfolio_*.html` |
| `python tools/backtest_portfolio.py` | Walk-forward backtest of the same book |

Only one instance runs at a time — `build_dashboard.py` takes an `fcntl` lock on
`.run.lock` itself, because macOS ships no `flock(1)`.

### Tests

```bash
python -m pytest tests/ -q
```

207 tests, no network. Every parsing function is exercised against captured
fixtures in [tests/fixtures/](tests/fixtures/), which is why `extract.py` and
`edgar.py` both keep their network-touching helpers confined to the bottom of
the module.

---

## How a run works

```
run_daily.sh
  └── build_dashboard.py
        ├── fetch_all()            fundamentals for 153 tickers
        ├── attach_history()       1y closes (YTD/1Y returns need the full year)
        │     └── data/fundamentals_YYYYMMDD.csv     (written, never pruned)
        ├── extract.collect_estimates()
        │     └── data/estimates_YYYYMMDD.csv.gz     (written, never pruned)
        ├── fetch_13f()            quarterly; usually makes no request at all
        │     ├── data/13f_YYYYQN.csv.gz             (written, never pruned)
        │     └── data/13f_filings.csv.gz            (manifest of every filing)
        ├── fetch_closes(period=5y)
        ├── record_history()  ──►  data/history.db
        └── render_html()     ──►  dashboard.html
  ├── visualize.py            ──►  history.html
  ├── tools/build_13f_report.py ──► reports/13f_YYYYQN.html
  └── tools/build_portfolio_report.py
        ├── data/history.db (target_weights)
        └── reports/portfolio_YYYYMMDD.html
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
| [tools/build_13f_report.py](tools/build_13f_report.py) | One quarter's 13F as a shareable page | Fetching anything |
| [portfolio.py](portfolio.py) | Alpha legs, the factor risk model, the cvxpy problem | Fetching, HTML, SQL writes |
| [factors.py](factors.py) | The Ken French library's CSV shapes | SQL, HTML, optimization |
| [tools/build_portfolio_report.py](tools/build_portfolio_report.py) | One day's book as a shareable page | Fetching anything |
| [tools/backtest_portfolio.py](tools/backtest_portfolio.py) | Walk-forward evaluation | Fetching, HTML |

`MetricRow` is defined in `history.py` and imported by `extract.py` — it is the
shared contract between producer and store. Importing a `NamedTuple` is not
knowledge of SQL.

---

## The data

### `data/` and the store

Observations live in `data/history.db`. The dated CSVs are gzipped text and
are the path *back* to it if the database is lost.

By default only the newest of each is kept — `RETAIN_DATED` in
[build_dashboard.py](build_dashboard.py). That keeps `data/` to a handful of
files rather than accumulating four per run forever, at a real cost worth
stating plainly: **the database is no longer fully reconstructable.** A rebuild
recovers the retained days and nothing before them, and `--rebuild-history`
says so rather than reporting a smaller number as though it were complete.

Two things make that safe enough to be the default. The run prunes a file only
once the store confirms it holds that date, so a fetch that failed before
recording cannot have its evidence deleted by the next run. And the two
datasets differ in how much the loss matters: statements, closes and share
counts are **recoverable** — equally available in six months — while forward
estimates, analyst dispersion and revision breadth are **perishable**, since
only `eps_trend` carries any history at all (~90 days) and every observation
not taken is lost permanently. Pruned estimates are not gone, they are simply
held in one place instead of two.

Raise `RETAIN_DATED` if you would rather trade the disk for the second copy;
at four files a run it costs about 300 KB a day.

Never pruned, deliberately: `data/13f_*.csv.gz` and `data/13f_filings.csv.gz`
are quarterly rather than daily, and a 13F amended away cannot be refetched;
`data/cusip_map.csv` and `data/ff_factors.csv.gz` are checked-in references.

One further exception to the rebuild guarantee: **daily closes are not
restored** by `--rebuild-history`, because no CSV holds them — unlike estimates
they can be refetched in full at any time. A rebuilt store is empty of prices
until the next normal run. `report['prices']` reports the shortfall so the gap
cannot be mistaken for data loss.

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

`runs` counts fetched companies and stored closes **separately**, because they
fail separately. On 2026-08-28 every one of the 153 `.info` pulls succeeded and
only 108 closes landed, and the run still recorded `ok, 153`. A run whose price
coverage falls below `history.PARTIAL_CLOSE_RATIO` of the universe is now
recorded as `partial` rather than `ok`. The threshold is 0.95 rather than 1.0
because a name can be legitimately dead — EA carries 6 closes in 60 sessions —
and exact equality would mark every run partial forever.

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

### Cadence, and how it stays quiet

`fetch_13f()` runs inside the normal daily run and almost always makes **no
network request at all**. A quarter enters the window the day its 45-day
deadline passes, which forces a sweep because that quarter has no rows yet;
otherwise EDGAR is re-checked only every `THIRTEENF_RECHECK_DAYS` (7), which is
what catches amendments and late filers. `run_daily.sh` then re-renders
`history.html`, since the 13F views are built from the store rather than the
fetch and would otherwise never show a new quarter.

Two records make the quiet state reachable. A manager that simply did not file
for a quarter is recorded as `no-filing`, so it counts as settled rather than
missing forever — without it, Viking Global's absent Q1 and Pershing Square's
absent Q2 would trigger a full sweep every single day. And
`data/13f_filings.csv.gz` is a manifest of every filing ever looked at, because
the position archives cannot carry a filing that reported **no positions**:
Viking's Q1 2026 information table was empty, and rebuilt from positions alone
the store would forget it filed and turn its 15 prior holdings into exits that
never happened.

### The CUSIP map

13F carries a CUSIP and an issuer name but never a ticker, and CUSIP is
licensed, so there is no free authoritative mapping. `data/cusip_map.csv` is
built from the filings themselves and then *proved*: a candidate pair is
accepted only when the price implied by the filers, `median(value / shares)`,
matches the ticker's close in `history.db`. Ten managers independently agreeing
that NVDA was $200.09 on 2026-06-30 is strong evidence, and the same check
catches filers reporting thousands rather than dollars, since they miss by
exactly 1000x. Every name the roster holds resolves this way, at a median
price error of **0.0000**.

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

### The file-descriptor limit is load-bearing

The plist sets `SoftResourceLimits / NumberOfFiles` to 4096. **Do not drop it.**

`yf.download(threads=True)` runs a thread pool in which every worker holds an
HTTPS socket *and* a SQLite connection to yfinance's own tz cache. launchd's
default soft limit is 256 — a quarter of what a 153-ticker pull needs — so the
run exhausted its descriptors partway through and yfinance reported the
casualties two different ways:

```
OperationalError('unable to open database file')     # the SQLite side
$AAPL: possibly delisted; no price data found        # the socket side
```

Neither message mentions descriptors, and the affected tickers differ every run,
because it is a resource race rather than anything about the data. AAPL was not
delisted. Reproduced by varying only `ulimit -n` against the same 153 symbols:

| `ulimit -n` | Result |
|---:|---|
| 1048576 (interactive shell) | 153/153, no errors |
| 256 (launchd default) | 150/153, false "possibly delisted" |
| 96 (emulating the run's other open files) | `unable to open database file` |

This is why price coverage is recorded per run rather than assumed: the failure
is environmental, silent, and costs a third of the universe when it returns.

---

## The universe

153 tickers, defined in `UNIVERSE` at the top of
[build_dashboard.py](build_dashboard.py). Edit the lists to taste.

| Sector | Tickers | Sub-industries |
|---|---:|---:|
| TMT (Tech · Media · Telecom) | 81 | 9 |
| Industrials | 40 | 6 |
| Consumer (Staples · Discretionary) | 32 | 5 |

### On adding names

Adding a ticker costs nothing in 13F terms. The archives hold the *full*
information tables, not just universe hits, so a new name picks up its whole
13F history with no network call:

```bash
python tools/build_cusip_map.py            # re-resolve, no network
python build_dashboard.py --rebuild-13f    # re-ingest, no network
```

Its fundamentals and estimates, though, start from the day it is added. Prices
and statements are recoverable; forward consensus is not, and Yahoo carries
about 90 days of it. A name added late is permanently missing that history.

Two things worth checking before adding one. Scoring is a percentile **within
the sub-industry**, so a name only scores meaningfully against a peer set of
real size — below about five members the percentile is coarse enough to be
decorative. And the peer set has to be *comparable*: `Telecom` sits at four
members and was deliberately left there, because every available US fill is
distressed or sub-scale (Cable One at $0.1B, Lumen loss-making) against a set
whose median is $180B. Adding them would have made the percentile worse, not
better. Where no coherent peer exists, the `PROXY_ETFS` benchmark is doing the
work instead.

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

---

## Portfolio construction

A dollar-, sector- and factor-neutral long/short book over the same universe,
solved with `cvxpy`. It runs **outside** the daily path — `build_dashboard.py`
never imports it, so a solver failure cannot cost you the dashboard, for the
same reason `record_history()` is non-fatal.

```
portfolio.py                     alpha composite, factor risk model, the solve
factors.py                       Fama-French factor returns (Ken French library)
tools/build_portfolio_report.py  solve -> record -> reports/portfolio_*.html
tools/backtest_portfolio.py      walk-forward evaluation
```

**Alpha** is an equal-weight blend of three legs, z-scored within sub-industry:
12-1 momentum, quarter-over-quarter change in tracked-manager 13F ownership,
and open-market insider purchases. The insider leg standardizes universe-wide
instead, because only 32% of names have a purchase within 12 months and
sub-industry groups of median 7 cannot support a within-group moment.

**Risk** is `Σ = B F Bᵀ + D` over 8 observable return series — FF5, UMD, and two
universe-relative sector factors with the third sector as base. That is 1,413
estimated parameters against 11,781 for a full sample covariance, and it is
positive-definite by construction.

**The book** maximizes `μᵀw` less a turnover penalty, subject to an 8% ex-ante
vol cap, `Bᵀw = 0`, dollar and per-sector neutrality, gross ≤ 200%, a 4%
position cap, and a 10% sub-industry band. There is no `λ·wᵀΣw` term: with `μ`
in z-score units λ has no interpretable scale, so the vol cap sets the scale
instead.

### What the backtest does and does not establish

Over 2024-09 to 2026-07 (23 monthly rebalances, bounded by 13F availability)
the book returned **0.55%/month after 10bps turnover costs, IR 0.76, max
drawdown 5.8%**, with every period solving cleanly.

**That IR is not significant.** t = 1.05, p = 0.30, and SE(IR) ≈ 0.72 at n = 23.
It is consistent with a real edge and equally consistent with noise. Seven
independent 13F changes cannot distinguish those. This is a sanity check that
the pipeline is not broken — not evidence that the signals work. The
`target_weights` record accumulates genuine out-of-sample evidence from the day
it ships, and that is what should eventually settle the question.

Turnover averages 1.43 but splits sharply: **2.61 at quarter ends versus 0.80
elsewhere**, as the 13F leg refreshes when filings land. The turnover penalty
exists to damp exactly that.

### Deferred

Estimate revisions are the strongest candidate signal and are **not** in the
blend: the store holds 25 days of `epsEst` and 5 of revision breadth. The leg
is a one-line addition to `LEG_WEIGHTS` once roughly 12 months accumulate.
