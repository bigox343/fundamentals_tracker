# Valuation history and change frames — design

**Date:** 2026-09-05
**Status:** proposed

Give every number on `dashboard.html` more than one reference frame. Today each
cell is scored exactly one way — against sub-industry peers, as of today. This
adds two more: **against the name's own history**, and **against its own recent
past** — and fixes a scoring bug found while probing for the first.

Scope is `dashboard.html` only. `visualize.py` / `history.html` are untouched.

---

## 1. What the dashboard cannot answer today

Every one of the 17 columns in `METRICS` is a point-in-time value tinted by
`relative_scores()`, a percentile within the sub-industry. The only time axis on
the page is the 6M price sparkline and the `ret1m` / `ret6m` / `retYtd` columns.

So the page can say "NVDA's 29x trailing P/E is the 12th percentile among
semis". It cannot say "29x is the 12th percentile **for NVDA**", and it cannot
say what that number was in June. Those are different questions, and the second
is the one that tells you whether a multiple has re-rated.

The store cannot answer it either. That is not a rendering gap, it is a data
gap, and it is measured in §2.1.

---

## 2. Evidence

Everything below was probed on 2026-09-05 against the live store, live SEC
EDGAR and live yfinance. Nothing here is reasoned about in advance.

### 2.1 The store has no valuation history

    period_type  metric        rows      days   earliest
    daily        close       190,422    1,270   2021-08-16
    estimate     epsEst       43,088       71   2026-05-17
    snapshot     forwardPE     2,429       16   2026-08-15
    snapshot     trailingPE    2,216       16   2026-08-15
    snapshot     evEbitda      2,429       16   2026-08-15

Three months before 2026-09-05 is 2026-06-05. There is no snapshot row for it;
the store's first is 2026-08-15. Prices go back five years, multiples go back
sixteen days.

### 2.2 A negative denominator currently scores as "cheapest"

Found while checking §2.3. `relative_scores()` scores a value by its distance
from the band median in MAD units, clipped to [-1, 1], then negates when
`higher_better=False`:

    med, mad = valid.median(), (valid - valid.median()).abs().median()
    z = max(-1.0, min(1.0, (v - med) / (1.5 * spread)))
    scores[idx] = z if higher_better else -z

For a valuation metric this means an *undefined* ratio — a company whose EBITDA
is negative — lands far below the median, clips to -1.0, and is negated to
**+1.0: the most favorable score the scale can express.**

    Software — Infrastructure, EV/EBITDA, as scored on 2026-09-04
    (score 1.00 = deepest blue = most favorable vs peers)

          evEbitda  score
    NET  -27887.75   1.00     <- rendered as the cheapest name in the band
    SNOW    -93.92   0.45     <- ranked cheaper than MSFT and ORCL
    ORCL     19.18   0.18
    MSFT     19.77   0.18
    FTNT     42.84   0.12
    PANW    145.42  -0.12
    ZS      462.38  -0.88
    CRWD   2020.94  -1.00
    DDOG    885.52  -1.00
    MDB    2065.37  -1.00

Scale of the problem on 2026-09-04:

    metric        n    negative    absurd    unrankable
    evEbitda    153       5        16 (>60x)   21  (14%)
    trailingPE  141       0        10 (>100x)  10   (7%)
    forwardPE   153       1         7 (>80x)    8   (5%)
    ps          153       0         3 (>40x)    3   (2%)

Sub-industries carrying a poisoned EV/EBITDA score: Software — Infrastructure
(7 names), Semiconductors (4), Software — Applications (2), Semicap Equipment,
Aerospace & Defense, Internet Retail.

The damage is not confined to the offending cell. Because `med` and `mad` are
computed over the column *including* the undefined values, they contaminate
every other name in the band. Excluding NET and SNOW from Software —
Infrastructure moves the band median from 94.13 to 303.90 and shifts every
surviving score by about half the available scale:

          evEbitda  before  after  shift
    NET  -27887.75    1.00    --      --
    SNOW    -93.92    0.45    --      --
    ORCL     19.18    0.18   0.67   +0.49
    MSFT     19.77    0.18   0.67   +0.49
    FTNT     42.84    0.12   0.61   +0.49
    PANW    145.42   -0.12   0.37   +0.49
    ZS      462.38   -0.88  -0.37   +0.51
    DDOG    885.52   -1.00  -1.00    0.00
    CRWD   2020.94   -1.00  -1.00    0.00
    MDB    2065.37   -1.00  -1.00    0.00

Oracle and Microsoft at 19x are the cheapest names in that band on any honest
reading, and the page renders them barely tinted. Fixing the guard is therefore
not only about the 21 bad cells; it restores the signal on the ~130 good ones
sharing a band with them.

The same class of bug reaches `netDebtEbitda`, where it is worse, because the
sign inverts rather than the magnitude exploding. Net debt divided by a
*negative* EBITDA is negative, which reads as net cash:

    Aerospace & Defense, ND/EBITDA on 2026-09-04
    (score 1.00 = most favorable = safest balance sheet)

         ndEbitda  evEbitda  score
    BA     -10.02    -67.40   1.00     <- ~$50B net debt, negative EBITDA
    GD       0.78     15.68   0.99
    HWM      1.47     38.65   0.34
    LMT      1.73     14.44   0.09
    RTX      1.92     19.17  -0.09
    NOC      2.03     12.38  -0.20
    LHX      3.23     30.30  -1.00
    TDG      6.03     18.61  -1.00

Boeing is rendered as the least leveraged name in its band, ahead of General
Dynamics, Lockheed and Northrop. A near-zero *positive* EBITDA produces the same
failure by magnitude: MDB scores 1.00 on ND/EBITDA of -170.08, CRWD, DDOG and ZS
likewise, purely because their denominators are close to zero.

This is live, it predates this work, and it inverts the signal on the exact
names the universe exists to track.

### 2.3 The source fundamentals are unstable

For a multiple `M` and price `P`, the implied fundamental is `F = P/M`.
Fundamentals do not change daily; any daily change in `F` is a data event, not a
market event. Across the 16 stored days:

    metric        obs    |dlnF| > 2%/day   > 10%/day    worst
    forwardPE    2,276          1.1%          0.4%       74%
    trailingPE   2,075          7.9%          1.3%      398%
    evEbitda     2,274         38.6%          1.8%       94%
    ps           2,276          1.9%          0.3%       20%

EV/EBITDA's underlying fundamental moves more than 2% on 38.6% of
observations. EBITDA does not change four days in ten. That column is
substantially reporting source noise.

### 2.4 A worked case: AVGO, 2026-09-04

    AVGO  2026-09-03   price 357.2   EV/EBITDA 42.6   P/S 22.5
    AVGO  2026-09-04   price 357.9   EV/EBITDA 33.4   P/S 19.1

Price flat, EV/EBITDA down 22% overnight, implied EBITDA up 27%. AVGO's most
recent periodic filing was a 10-Q **filed 2026-06-09** for period 2026-05-03;
nothing was filed near 2026-09-04. So this was not new information — it was the
source revising itself. A reader looking at the page that morning saw a 22%
valuation move with nothing behind it.

### 2.5 SEC XBRL has the depth, with filing dates

`companyconcept` for `us-gaap:EarningsPerShareDiluted`, AAPL:

    338 facts, 2007-09-29 -> 2026-06-27, 70 distinct fiscal periods
    ('2026-06-27', 2.02, '10-Q', filed 2026-07-31)
    ('2007-09-29', 3.93, '10-K',   filed 2009-10-27)
    ('2007-09-29', 3.93, '10-K/A', filed 2010-01-25)

Nineteen years, quarterly, each fact carrying the date it was **filed**.
yfinance by contrast serves 5 annual periods and 5 quarters, with period ends
only and no filing date.

The filing date is the deciding factor. Building a daily series off period ends
credits the market with knowing Q2 earnings on June 30 when they were filed
July 31 — a systematic 30-45 day lookahead across the whole series, in a repo
that already built a no-lookahead guard into `tools/backtest_portfolio.py`.

### 2.6 Coverage

152 of the 153 universe tickers resolve to a CIK via
`https://www.sec.gov/files/company_tickers.json` (10,412 entries, one request,
no key). The single miss is **EA**, which the README already records as
effectively dead — 6 closes in 60 sessions.

Concept availability confirmed on AAPL: `EarningsPerShareDiluted`, `Revenues`,
`RevenueFromContractWithCustomerExcludingAssessedTax`, `NetIncomeLoss`,
`OperatingIncomeLoss`, `StockholdersEquity`, `CommonStockSharesOutstanding`,
`Assets`, `LiabilitiesCurrent`, `CashAndCashEquivalentsAtCarryingValue`,
`LongTermDebtNoncurrent`.

### 2.7 Payload sizes

A full `companyfacts` document is **3.8 MB** for AAPL — 152 of them is ~580 MB
per sweep. Per-concept `companyconcept` documents are gzipped on the wire and
measured far smaller:

    EarningsPerShareDiluted                            4 KB   338 facts
    WeightedAverageNumberOfDilutedSharesOutstanding    4 KB   234
    OperatingIncomeLoss                                3 KB   234
    CashAndCashEquivalentsAtCarryingValue              3 KB   228
    LongTermDebtNoncurrent                             2 KB    90
    NetCashProvidedByUsedInOperatingActivities         2 KB   134
    PaymentsToAcquirePropertyPlantAndEquipment         2 KB   105
    DepreciationDepletionAndAmortization               1 KB    75
    Revenues                                           1 KB    11
    ------------------------------------------------------------
    AAPL, 9 concepts                                  22 KB

152 tickers x 9 concepts is **3 MB and 1,368 requests**, about 2.3 minutes at
SEC's 10/s limit — 190x lighter than `companyfacts`, and it makes the concept
list explicit in code rather than implicit in a filter. The sweep uses
`companyconcept`.

AAPL's `Revenues` carrying only 11 facts is itself evidence for the fallback
chain in §5: AAPL tags revenue as
`RevenueFromContractWithCustomerExcludingAssessedTax` almost throughout.

---

## 3. Module boundaries

Following the existing "knows / deliberately does not know" contract:

| File | Knows about | Deliberately does not know about |
|---|---|---|
| `xbrl.py` (new) | SEC XBRL fact shapes, concept normalization, duration disambiguation, filing dates | SQL, HTML, the universe, yfinance |
| `valuation.py` (new) | Turning point-in-time facts + closes into daily multiple series | Fetching, HTML, SQL writes |
| `render.py` (new, extracted) | CSS, the JS template, cell construction, page assembly | Fetching, SQL, the universe |
| `tools/backfill_xbrl.py` (new) | The initial sweep and the incremental top-up | Rendering |

`xbrl.py` imports exactly one name from `edgar.py`: the SEC-polite HTTP getter,
promoted from `_get` to `sec_get`. That is the same precedent as `MetricRow`
living in `history.py` and being imported by `extract.py` — a shared contract
crossing a module line, not a leak of knowledge.

`render.py` is an extraction, not new design: `CSS`, `JS_TMPL`, `TOOLBAR`,
`fmt`, `parse_spark`, `sparkline`, `spark_cell`, `cell_style`, `band_perf`,
`render_sector`, `render_spx`, `build_js`, `render_html` move out of
`build_dashboard.py` unchanged. That file is 1,569 lines today and this work
roughly doubles the JS. Only `tests/test_prune.py` imports `build_dashboard`,
so the move is near-zero risk — and the render layer currently has no tests,
which the extraction makes it possible to fix.

---

## 4. A0 — the scoring domain guard

Ships first, independent of everything else.

Each entry in `METRICS` gains a validity domain. A value outside its domain is
**excluded from the peer percentile** and rendered greyed with no tint and a
hover reason, rather than silently ranked.

    forwardPE, trailingPE   value > 0
    evEbitda                value > 0
    ps                      value > 0
    netDebtEbitda           EBITDA > 0   -- not merely finite; see below
    fcfYield, margins       finite       -- negative is genuinely meaningful

`netDebtEbitda` needs the denominator's sign, not its own. A negative
ND/EBITDA is meaningful — it means net cash — but *only when EBITDA is
positive*. With EBITDA negative the ratio flips sign and a heavily indebted
company reads as net cash (BA, §2.2). The guard must therefore be evaluated
against EBITDA rather than against the published ratio, which means the domain
for this one metric depends on a second field.

The rule is the one the store already applies at its write boundary — "a
missing value writes no row", and its sibling `extract.is_degenerate_estimate`.
An undefined ratio is not an extreme value; it is an absent one. Excluding it
also repairs the scores of every *other* name in the band, because `med` and
`mad` are computed over the surviving values (§2.2, measured at ~0.49 of scale
for Software — Infrastructure).

Expect visible change: 21 names stop being tinted on EV/EBITDA, 10 on trailing
P/E, 8 on forward P/E, and BA plus the four near-zero-EBITDA software names stop
being tinted on ND/EBITDA.

---

## 5. Point-in-time valuation series (`valuation.py`)

For each ticker and each date `d` in the daily close series:

1. Take all quarterly facts for the concept with `filed <= d`.
2. Where a period has several rows (an original and its amendments), keep the
   one with the greatest `filed` that is still `<= d`.
3. Sum the four most recent distinct periods. That is the trailing-twelve-month
   figure **as it was known on `d`**.
4. Divide the adjusted close by it.

Four traps. Each silently produces a wrong series rather than an error:

| Trap | Consequence if missed |
|---|---|
| Each period-end carries both a year-to-date and a three-month fact | TTM double-counts. AAPL's 2026-03-28 holds both `4.85` and `2.01`; they are distinguishable only by `period_end - period_start` |
| A 10-K carries no Q4 fact, only the annual figure | Every Q4 is a hole, or TTM silently spans five quarters. Q4 must be reconstructed as `FY - (Q1+Q2+Q3)` |
| Filers tag revenue three different ways | Mixing tags mid-series renders as a fake revision. Choose one chain member per ticker — the one with the most facts — and hold it fixed for that ticker's whole series |
| **Reported EPS is not split-adjusted; stored closes are back-adjusted** | NVDA's 2024 10:1 split makes every pre-split P/E read **10x too high** |

The last is this repo's 13F lesson in a new dataset — *"share counts are
as-filed; stored closes are back-adjusted; a 25:1 split renders as the manager
adding 2,400%"*. `history.split_ratios` and `history._snap_split` already exist
for it and are reused.

Metrics produced, and the concepts each is built from:

| Metric | Construction | Concepts |
|---|---|---|
| Trailing P/E | `close / TTM EPS` | `EarningsPerShareDiluted` |
| P/S | `close / TTM revenue per share` | revenue chain, `WeightedAverageNumberOfDilutedSharesOutstanding` |
| EV/EBITDA | `(mktcap + debt - cash) / TTM EBITDA` | `OperatingIncomeLoss`, `DepreciationDepletionAndAmortization`, `LongTermDebtNoncurrent`, `CashAndCashEquivalentsAtCarryingValue`, shares |
| FCF yield | `TTM FCF / mktcap` | `NetCashProvidedByUsedInOperatingActivities`, `PaymentsToAcquirePropertyPlantAndEquipment`, shares |

The revenue chain is
`RevenueFromContractWithCustomerExcludingAssessedTax` -> `Revenues` ->
`SalesRevenueNet`, resolved per ticker to whichever member carries the most
facts and then held fixed for that ticker's whole series.

EBITDA is not an XBRL concept; it is reconstructed as
`OperatingIncomeLoss + DepreciationDepletionAndAmortization`. That definition is
pinned in `valuation.py` and is what the §6 proof harness validates against
Yahoo's own figure — if the two disagree systematically, the definition is
wrong and the metric is withheld rather than rendered.

**Forward P/E gets no own-history frame** — forward consensus is perishable, Yahoo carries roughly 90
days, and nothing before 2026-08-15 exists to recover. It begins accruing now.

The series is **not stored**. 153 x 1,270 x 4 is ~780k values, computed in
pandas in about a second, and a derived table would add a staleness mode for no
gain. Percentiles are computed from the full daily series so they are exact;
only the drilldown *chart* is downsampled.

---

## 6. The proof harness

The normalization risk in §5 is managed the same way `data/cusip_map.csv` is:
the mapping is not trusted, it is **proved**.

For each `(ticker, metric)`, reconstruct the metric from `reported` plus the
stored close on each of the 16 days where Yahoo's `snapshot` value also exists,
and accept the pair only when the **median relative error is under 1%**. The
median rather than the mean, so one bad day cannot reject a good mapping; 1%
rather than exact, because Yahoo rounds and may use a slightly different EBITDA
definition. That constant is pinned in code and revisited against the first
sweep's distribution — the CUSIP map's equivalent check achieved a median price
error of 0.0000, so a wide spread here is a signal that something is wrong
rather than that the tolerance is too tight.

- A pair that fails the proof gets **no own-history frame**, never a wrong one.
- The per-metric pass rate is reported after every sweep. A metric passing on
  under ~90% of names indicates broken normalization, not bad data.

This is the same evidence standard as ten managers independently agreeing NVDA
was $200.09 on 2026-06-30. It converts "hope the tags are right" into a number
that is read after each run.

---

## 7. Change frames and restatement flags

From §2.3, `dln M = dln P - dln F`. Price is known daily, so `dln F` is
recoverable and any non-zero daily value is a data event. `reported.filed`
splits those events in two:

- **`*` new report** — the flag falls within a few days of an actual 10-Q or
  10-K. Real information; the multiple genuinely changed.
- **`!` source revision** — no filing accounts for it. The source changed its
  mind. AVGO on 2026-09-04 (§2.4) resolves here.

Change windows: 1 week and 1 month. For the four valuation metrics these are
computed from the derived daily series and are therefore **available
immediately at full depth**. For the remaining metrics — margins, growth, ROE,
forward P/E — they come from the `snapshot` table and are limited to what it has
accrued (16 days as of 2026-09-05, so 1w works and 1m does not yet). Cells
without enough depth render an em dash, never a zero, with the reason in the
legend.

---

## 8. Schema

One new table:

```sql
reported (
  ticker       TEXT NOT NULL,
  concept      TEXT NOT NULL,   -- us-gaap tag, as filed
  period_start TEXT NOT NULL,   -- '' for instant facts
  period_end   TEXT NOT NULL,
  fy           INTEGER,
  fp           TEXT,            -- 'Q1'|'Q2'|'Q3'|'FY'
  form         TEXT NOT NULL,   -- '10-Q' | '10-K' | '10-K/A' | ...
  filed        TEXT NOT NULL,
  value        REAL NOT NULL,
  ingested_at  TEXT NOT NULL,
  PRIMARY KEY (ticker, concept, period_end, period_start, filed)
)
```

Two deliberate choices in that key.

`filed` is part of it so an original and its amendment **coexist as separate
rows** rather than one overwriting the other. That is the entire basis of
point-in-time: on 2026-06-15 you want the number the market actually had, not
the number it was later corrected to. It also makes restatements queryable
instead of invisible.

`period_start` is part of it because a single `period_end` carries both a
year-to-date and a three-month fact (§5, trap 1), and they are otherwise
indistinguishable.

`value` is `NOT NULL` and non-finite values are dropped at the write boundary,
matching `metrics`.

---

## 9. Rendering

A `frame` select is added beside the existing `tint` control:

    Frame:  Peers | Own history (5y) | Change 1w | Change 1m

`tint` (sub-industry vs sector) greys out when `frame != Peers`, since the
basis only means something for the peer frame.

Cells gain `data-oh`, `data-c1w`, `data-c1m` and `data-rs` alongside today's
`data-v` / `data-ss` / `data-sc`. The existing JS already swaps which basis
paints a cell; this extends that mechanism rather than replacing it.

An always-on one-character direction arrow renders from `data-c1w`, with a
**0.5% deadband** — below that the cell shows no arrow rather than a misleading
one. The same threshold defines a `dln F` data event in §7, so a single
constant governs both.

Clicking a valuation cell opens a drilldown showing that multiple's own five-year
chart, today's percentile within its own range, the range endpoints, and the
recent change log with any `*` / `!` marks.

Payload: the chart series is downsampled to weekly — 4 metrics x ~260 weeks x
153 names is ~159k values, an estimated 500-700 KB, taking `dashboard.html` from
~476 KB to roughly 1.2 MB. This is measured during implementation rather than
assumed; if it overshoots, the chart drops to monthly points, which costs
nothing visually at five-year scale.

---

## 10. Cadence and retention

The sweep runs from `tools/`, never inside `build_dashboard.py` — a SEC outage
must not cost you the dashboard, the same reasoning that keeps `portfolio.py`
out of the daily path and `record_history()` non-fatal.

Steady state follows the 13F quiet-state pattern. A ticker is re-checked only
once its newest known filing is more than ~85 days old; `submissions/CIK*.json`
is small and is consulted first, and `companyconcept` is pulled only when a new
accession appears. Most days that is zero requests.

Freshness comes from `SELECT ticker, MAX(filed) FROM reported GROUP BY ticker` —
the store's own record, not a file mtime.

**Retention.** As of `faeab61` (2026-09-05) the repo keeps only the newest dated
file of each kind and no longer guarantees a full rebuild. The rebuild path for
this dataset is therefore a single **undated** `data/reported.csv.gz`, rewritten
after each sweep. Undated means `extract.dated_stamp` cannot match it and the
pruner cannot reach it, placing it with `cusip_map.csv` and `ff_factors.csv.gz`
as a reference file. It is also genuinely recoverable: SEC serves superseded
facts alongside current ones — AAPL's 2007 10-K and its 10-K/A both appear in
today's response — so a lost archive costs a re-sweep, not data.

---

## 11. Error handling

- One ticker failing does not abort a sweep; per-ticker status is recorded and
  reported.
- A `(ticker, metric)` pair failing the proof gets no own-history frame.
- A date preceding a company's first filing renders blank, never zero.
- A ticker with no CIK (EA) gets no own-history frame and is reported once, not
  per run.
- Housekeeping and reporting never raise into the render path.

---

## 12. Testing

Fixtures only, no network, matching the existing 229-test discipline. The
fixture set is chosen so that each trap in §4-§7 has a test that fails without
its fix:

| Fixture | Covers |
|---|---|
| A `Revenues` filer and a `RevenueFromContractWithCustomer` filer | Concept fallback chain, held fixed per ticker |
| AAPL 2026-03-28 (`4.85` YTD and `2.01` quarterly) | Duration disambiguation |
| A 10-K with no Q4 fact | Q4 reconstruction as `FY - (Q1+Q2+Q3)` |
| AAPL 2007-09-29 10-K plus 10-K/A | Amendment coexistence, and point-in-time selection by `filed` |
| AAPL (Sept), AVGO (Nov), NVDA (Jan) fiscal year ends | Non-December calendars |
| NVDA 2024 10:1 split | Split adjustment against back-adjusted closes |
| NET negative EBITDA | The §4 domain guard, and that it is not ranked |
| AVGO 2026-09-04 | The `!` source-revision flag |
| A pair with a deliberately wrong tag | The proof harness rejects rather than renders |

---

## 13. Phasing

Each phase leaves `dashboard.html` working and shippable.

| | Deliverable | Visible result |
|---|---|---|
| 0 | Extract `render.py` | None — mechanical |
| 1 | A0 scoring domain guard (§4) | NET stops rendering as the cheapest infra-software name |
| 2 | `xbrl.py`, `reported`, backfill, proof harness (§5, §6, §8, §10) | None — data only; pass rate reported |
| 3 | `valuation.py`, own-history frame, drilldown (§5, §9) | "Is 29x cheap **for NVDA**", answered five years deep |
| 4 | Change frames and `*` / `!` flags (§7) | "What moved, and was any of it real" |

---

## 14. Deferred

Named here so the spec does not quietly grow.

- **The attention rail** — a ranked shortlist above the sector tables composing
  these signals with the three alpha legs already in `portfolio.py` (momentum,
  13F ownership change, insider buys). Its own spec, once phases 3-4 produce
  signals worth ranking. Built before them it would be a hand-tuned heuristic
  thrown away later.
- **Own-history frames on margins, growth and ROE.** The XBRL concepts exist
  (§2.6) but each widens the normalization surface the proof harness must cover.
- **Forward P/E own-history before 2026-08-15.** Permanently unrecoverable.
- **`visualize.py` / `history.html`.** Out of scope by decision; this work is
  `dashboard.html` only.
