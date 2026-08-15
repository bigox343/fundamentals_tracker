# Probe Findings: §10 Open Items

**Date:** 2026-08-15
**Probes against:** yfinance 1.4.1, pandas 3.0.3, the live 148-ticker universe
**Resolves:** §10 of `2026-08-15-fundamentals-history-design.md`
**Fixtures:** `docs/superpowers/specs/evidence-20260815/` · `tests/fixtures/`

---

## Summary

All three open items are resolved. Two of the three came back with an answer that
**changes the design**, not just fills a blank:

| # | Open item | Answer | Design impact |
|---|---|---|---|
| 1 | Fiscal resolution on a non-calendar, non-US name | Works — but the rule in §6.3 is wrong for `0q`, and both statement frames go stale | **§6.3 must be rewritten** |
| 2 | `eps_trend` columns: calendar or trading days | Calendar | Backfill dating settled; mark backfilled rows as lower-fidelity |
| 3 | Track daily CSVs in git | Recommend yes | No change |

The headline: **`0q` does not mean what the spec assumes**, and the statement frames
that §6.3 resolves from can lag a full fiscal period behind reality. Both faults
produce plausible, wrong `ref_period` values — exactly the silent corruption §6.2's
primary key was designed to prevent, arriving through a door the key does not guard.

---

## 1. Fiscal-period resolution

### 1.1 What `0q` actually means

§6.3 resolves `0y` as "the fiscal year ending on the next fiscal year-end after
`as_of`" and says `0q`/`+1q` "use `quarterly_income_stmt` dates" — which reads as the
same rule applied to quarters. It isn't.

Ground truth was established independently of any rule, by matching
`earnings_estimate.yearAgoEps` against reported EPS history and the next scheduled
earnings date (`evidence-20260815/probe_a2_report.json`):

| Ticker | Yahoo `0q` is the quarter ending | Reports on | "next quarter-end after `as_of`" gives |
|---|---|---|---|
| NVDA | 2026-07-31 | 2026-08-26 | 2026-10-31 ❌ |
| AVGO | 2026-07-31 | 2026-09-02 | 2026-10-31 ❌ |
| WMT | 2026-07-31 | 2026-08-20 | 2026-10-31 ❌ |
| COST | 2026-08-31 | 2026-09-24 | 2026-08-31 ✓ |
| INFY | 2026-09-30 | 2026-10-23 | 2026-09-30 ✓ |
| CSX | 2026-09-30 | 2026-10-22 | 2026-09-30 ✓ |

`0q` is **the quarter that reports next**, which has usually already *ended*. The
as_of rule is wrong by a full quarter on 3 of 6 — and wrong for precisely the names
whose quarter has closed but not yet been reported, i.e. every company in the two
weeks before its earnings date. Rotating through the calendar, that is most of the
universe most of the time.

**Correct formulation: count forward from the last _reported_ period, never from
`as_of`.** Same rule for years and quarters, which also disposes of the rollover
boundary §8 asks about — it is handled by construction rather than by a special case.

### 1.2 Non-calendar and non-US names resolve fine

12 names probed (`evidence-20260815/probe_a_report.json`), spanning India and UK ADRs (INFY, ARM —
both March FY), Netherlands (ASML), Canada (QSR), and US 52/53-week calendars
(COST, AVGO, ADBE, MU, NKE, WMT, CSCO). Domicile is a non-issue. Two structural
findings instead:

**Yahoo normalizes every statement column to a month-end — 148/148 across the
universe, annual and quarterly alike.** But `info`'s fiscal fields carry the *true*
52/53-week dates: NVDA `lastFiscalYearEnd` = 2026-01-25 where `income_stmt` says
2026-01-31; AVGO 2025-11-02 vs 2025-10-31; MU 2025-08-28 vs 2025-08-31.

Two sources therefore label the same fiscal period with two different dates. If
estimates key off one and sub-project 4's statement backfill keys off the other,
they will never join — and a 52/53-week date drifts a few days *every year*, so
raw dates never stabilize. **`ref_period` needs a canonical form: snap to
month-end.** A tested `snap_month_end` is included below; note that
`ts - pd.offsets.MonthEnd(0)` rolls *forward*, not backward, which silently mapped
AVGO's 2025-11-02 to 2025-11-30 instead of 2025-10-31 in the first draft.

**COST's fiscal year-end and Q4-end are both 2026-08-31** — a second live instance of
the collision §6.2 cites for NVDA, confirming `period_type` belongs in the primary key.

### 1.3 The statement frames go stale — the real hazard

This was not anticipated by the spec and is the most consequential finding.

**Yahoo's `income_stmt` can lag a full fiscal year behind the reported results.**
Six names are in that state today (`evidence-20260815/probe_a4_report.json`):

| Ticker | Reported FY on | `income_stmt` latest | Rule from statements | Truth |
|---|---|---|---|---|
| CSCO | 2026-08-12 | 2025-07-31 | FY 2026-07-31 | **FY 2027-07-31** |
| SMCI | 2026-08-11 | 2025-06-30 | FY 2026-06-30 | **FY 2027-06-30** |
| WDC | 2026-08-05 | 2025-06-30 | FY 2026-06-30 | **FY 2027-06-30** |
| COHR | 2026-08-12 | 2025-06-30 | FY 2026-06-30 | **FY 2027-06-30** |
| LITE | 2026-08-11 | 2025-06-30 | FY 2026-06-30 | **FY 2027-06-30** |
| PH | ~2026-08-07 | 2025-06-30 | FY 2026-06-30 | **FY 2027-06-30** |

Confirmed by `yearAgoEps`: CSCO's `0y` year-ago figure is 3.81, which matches none of
the four annual EPS values in the frame (2.61, 2.54, 3.07, 2.82) — because it is
FY2026, the year the frame is missing.

`info.lastFiscalYearEnd` was **correct for all six**, and agrees with the statement
columns (after snapping) for the other 142. So:

> **Resolve `0y` from `info.lastFiscalYearEnd`, not from `income_stmt` columns.**
> It is free — `fetch_row` already fetches `info` (`build_dashboard.py:195`) — and was
> right on all 18 names checked.

**`quarterly_income_stmt` goes stale the same way**, for roughly the fortnight after a
report: CSCO, SMCI, COHR and LITE all still end at 2026-03-31 having reported their
June quarter days ago. Resolving `0q` from that frame lands a quarter short. The
per-ticker fields that might guard it are each unreliable on their own:

- `info.mostRecentQuarter` — COST returns **2021-05-09**, five years stale.
- `info.earningsTimestampStart` — stale for 7 of 147 (PINS, SNAP, LYV, TTWO, JCI,
  CMG, YUM all return a July/August date already in the past). The tell is that it
  falls *before* the resolved quarter-end, ~55-60 days.
- `get_earnings_dates()` — accurate everywhere it was checked, but costs an extra
  HTTP call per ticker per day.

A staleness guard keyed on the last earnings date works on every case tested
(advance one quarter when `last_earnings_date > latest_quarterly_column + 60 days`;
60 sits above the normal reporting lag and below a full quarter), but the threshold
is a heuristic and the extra fetch is a real cost. **This is the one genuine design
decision the implementation plan still owes**, and it should be made against the
fixtures rather than in the abstract.

### 1.4 Annual and quarterly estimates collide on the primary key

Found by the first live capture, not by the probes and not by the test suite —
worth recording because it is the one fault that reached real stored data.

§6.2 puts `period_type` in the primary key so NVDA's fiscal year and its Q4,
both ending 2026-01-31, cannot overwrite each other. That works for statement
rows, which are `annual` vs `quarter`. It does nothing for estimates, where all
four horizons carry `period_type = 'estimate'`. When a company's fiscal year and
one of its quarters end on the same day, `0y` and `0q`/`+1q` resolve to the same
`ref_period` and the key collapses:

| Ticker | Colliding horizons | Shared date | Stored | Lost |
|---|---|---|---|---|
| AVGO | `+1q` and `0y` | 2026-10-31 | 11.62543 (annual) | 3.87377 (quarterly) |
| COST | `0q` and `0y` | 2026-08-31 | 20.59016 (annual) | 6.56439 (quarterly) |
| CSX | `+1q` and `0y` | 2026-12-31 | — | — |

Three of the eight names in the first capture. The surviving value is finite,
plausible and wrong by roughly 3x, and nothing in the pipeline flags it.

**Fix: `ref_period` carries the period's granularity — `FY2026-10-31` against
`FQ2026-10-31`.** A fiscal year and a fiscal quarter ending the same day are
different periods; a bare end date is not a complete identifier for one. This
also keeps `period_type` as §6.2 defines it rather than splitting `estimate` in
two, and leaves the join to sub-project 4's statement rows a prefix strip.

### 1.5 Coverage, and the skip path

Across all 148: **zero** missing annual statements, zero missing quarterly, zero
missing `eps_trend`. §6.3's "skip, not guess" branch will therefore never fire on
today's universe — it exists for newly added tickers, and needs a fixture test
rather than a live one.

### 1.6 Verified resolver

Passes 6/6 against ground truth, plus the edge cases. Shipped as
`extract.snap_month_end` / `extract.resolve_ref_periods`, with the ground truth
encoded as a parametrized table in `tests/test_extract.py`.

```python
def snap_month_end(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts).normalize()
    # NB: `ts - MonthEnd(0)` rolls *forward*, not backward -- do not use it here.
    this_me = ts + pd.offsets.MonthEnd(0)
    prev_me = ts.replace(day=1) - pd.Timedelta(days=1)
    return prev_me if (ts - prev_me) < (this_me - ts) else this_me
```

| Input | Snaps to | Why it matters |
|---|---|---|
| 2026-07-30 | 2026-07-31 | `DateOffset(months=3)` drift off a 30-day month-end |
| 2026-01-25 | 2026-01-31 | NVDA's true 52/53-week FY end |
| 2025-11-02 | 2025-10-31 | AVGO's — the case the buggy first draft got wrong |

---

## 2. `eps_trend` day convention: **calendar days**

The naive test — does the largest jump land in the bracket containing the earnings
date — favours *trading* days, 18 to 10 among >5% movers. That test is confounded:
analyst revisions trickle in for days after a report, so the jump routinely shows up
one bracket later than the news that caused it, which biases every reading toward the
longer lookback.

The causality test is not confounded — a jump cannot precede its cause. Under a
hypothesis, if the largest observed jump sits entirely *before* the earnings date,
that hypothesis is wrong. Over 228 ticker-horizon cases with a move ≥1%:

| Hypothesis | Impossible orderings |
|---|---|
| **Calendar days** | **14 / 228 (6%)** |
| Trading days | 41 / 228 (18%) |

The cleanest cut is the group reporting 71-95 days ago, where the two conventions are
furthest apart: **40 of 44 put the largest jump in the oldest bracket.** Under calendar
that bracket is 2026-05-17 → 06-16, which contains their late-May/early-June reports.
Under trading it is 2026-04-10 → 05-22, which mostly precedes them — DELL's 46% `+1y`
revision would have to have happened six weeks before the 2026-05-28 report that
caused it.

**Backfilled points therefore date to as_of − {90, 60, 30, 7, 0} calendar days:**
2026-05-17, 06-16, 07-16, 08-08, 08-15.

Two caveats worth carrying into the plan:

- The spill effect means these are snapshots of a slow-moving consensus, accurate to
  a few days at best. Fine for attribution over 30-day-plus windows; **not** fine for
  a 7-day window. The schema already distinguishes them for free — a backfilled row
  has `ingested_at` far later than its `as_of`, where a captured row has the two
  within a day. Worth stating as the provenance rule rather than leaving implicit.
- Dirty values exist and are not NaN. LITE returns `0y avg` == `0y yearAgoEps` ==
  `+1y yearAgoEps` == 8.22662 with `+1y avg` = 33.01 (+300%). §7's "reject NaN and inf"
  does not catch this. A cheap structural check — `avg` equal to its own `yearAgoEps`
  is degenerate — catches it without inventing per-metric bounds.

### The revision signal is real, and it is perishable

Of 281 ticker-horizon series across 144 tickers, **not one was flat** over the 90-day
window. Median absolute move 3.4%, 90th percentile 21%. Today's capture is saved to
`data/eps_trend_20260815.csv`; every day before implementation lands is a day of this
lost permanently, which is §4's argument, now measured.

---

## 3. Track daily CSVs in git: **yes**

The status quo already does (`data/fundamentals_20260815.csv` is committed;
`.gitignore` excludes only `*.db`). Keeping it means the repo alone can rebuild the
store, which is the whole point of §6.1 given the raw CSVs are the durable record for
data that cannot be refetched. ~30MB/year is not a constraint. Also drop the
`-mtime +365 -delete` prune in `run_weekly.sh` before the next run — §6.1 calls the
raw CSVs never-pruned, and that line still deletes them.

---

## 4. What this changes in the design

1. **§6.3 rewritten.** Resolve from the last reported period, not from `as_of`;
   `0y` from `info.lastFiscalYearEnd`; `ref_period` canonicalized by snapping to
   month-end; `0q` guarded against a stale quarterly frame using the last
   earnings date (`extract.STALE_QUARTER_DAYS = 60`).
2. **§6.2 gains a rule on `ref_period` form.** Estimate labels carry `FY`/`FQ`
   because `period_type` cannot separate a fiscal year from a quarter ending the
   same day — see §1.4. The schema itself is unchanged.
3. **§7 gains a row** for structurally degenerate estimate values (LITE):
   a mean outside its own low/high, or equal to its own prior-year actual.
4. **§8 gains fixture cases** — the six stale-annual names, COST's FY/Q4
   collision, AVGO's 52/53-week snap, and the three FY/FQ key collisions. All
   are encoded as a parametrized ground-truth table in `tests/test_extract.py`
   rather than left as captured frames, since the live state behind them
   refreshes within days and becomes unreproducible.
5. **§10 closes.**

Nothing here changes the storage design, the module split, the cadence argument, or
the run flow. The spec's structure holds; §6.3's arithmetic does not, and §6.2's
key needed one more distinction than it made.

---

## 5. Impact on the in-flight implementation plan

A separate session wrote `docs/superpowers/plans/2026-08-15-fundamentals-history-capture.md`
at 18:13 today. Its `extract.resolve_ref_periods` implements the as_of rule:

```python
def next_period_end(anchor, as_of, step_months):
    """The first period end strictly after as_of, stepping from a known end."""
```

Run against the verified ground truth, it mismatches on **3 of 7** names — NVDA, AVGO
and WMT all resolve `0q` to 2026-10-31 where the truth is 2026-07-31.

The plan is also internally inconsistent: `test_resolve_ref_periods_for_nvda` asserts
`got["0q"] == "2026-07-31"`, which is the correct value, but the implementation
directly beneath it returns `2026-10-31`. **That test fails as written**, so the
inconsistency surfaces at Step 4 rather than silently shipping. The test's expectation
is right; the implementation is what needs replacing.

Two further gaps, neither of which the plan's fixtures can currently catch because
they only appear against live frames:

- `resolve_ref_periods` takes `annual_ends` from `income_stmt`, which is stale by a
  full fiscal year for six names today (§1.3). It needs `info.lastFiscalYearEnd`.
- `test_resolve_ref_periods_across_year_rollover` steps `as_of` across 2027-01-31 with
  the statement fixtures held constant, so it tests the arithmetic but not the rollover
  as it actually occurs — where the frames lag the report by days to weeks.

Everything else in the plan — the `MetricRow` shape, the upsert, the schema, the
module split, `--rebuild-history` — is unaffected by these findings.
