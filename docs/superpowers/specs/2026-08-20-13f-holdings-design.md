# 13F hedge-fund holdings — design

**Date:** 2026-08-20
**Status:** approved, implementing

Track what ~27 marquee hedge funds — the Tiger lineage, the multi-strats, and a
concentrated/activist sleeve — hold in the 148-name universe, and how those
positions changed quarter over quarter.

---

## 1. Why the existing `holdings` table cannot answer this

`extract.py` already writes a table called `holdings`, and `build_dashboard.py`
prints `"Fetching ownership (13F holders, funds, insider filings)"` while doing
it. Both names are misleading. The source is yfinance's `institutional_holders`
and `mutualfund_holders`, which return a **top-10 list per ticker**. Across all
2,961 rows of `data/holdings_20260820.csv` the holders are BlackRock, Vanguard,
State Street, Geode, iShares and similar. **Zero hedge funds appear**, and no
top-10 list ever will contain one — a Lone Pine position is real money and still
nowhere near the tenth-largest holder of a mega cap.

Real 13F data is filed **per manager, not per issuer**, so it inverts the grain
of every fetch in this repo: you pull N funds and join into the universe, rather
than pulling N tickers. That is why this is a new module and not a new column.

The misleading `print()` is corrected as part of this work. Leaving two
different datasets in the codebase both called "13F" guarantees a future
mistake.

---

## 2. Evidence

Everything below was probed against live EDGAR on 2026-08-20, not reasoned about
in advance. Q2 2026 filings were due 2026-08-14 and were present.

### 2.1 Coverage

113 of 148 universe names are held by at least one of the 28 probed funds.

| Name | Funds | Name | Funds |
|---|---:|---|---:|
| GOOGL, AMZN | 21 | AMD | 14 |
| META | 18 | SNOW, LRCX | 13 |
| NVDA | 16 | RDDT, QCOM, INTC, AVGO | 12 |
| MSFT | 15 | TSLA, TDG, SPOT | 10 |

### 2.2 Four traps, each of which silently produces wrong numbers

**XML namespaces are not consistent between filers.** Tiger Global emits
`<infoTable>`; Millennium emits `<n1:infoTable>`. A regex parser keyed on the
bare tag returns *zero rows* for the namespaced half — which does not look like
a parse failure, it looks like a fund that holds nothing. Five of 28 funds
parsed to 0 positions before this was found. The parser matches on XML
local-name and ignores the prefix.

**Options are not ownership.** 7,627 of Citadel's 16,127 lines carry a
`<putCall>` tag (47%); Point72 is 50%, Millennium 34%. Counting these as share
ownership overstates multi-strat positions by roughly a factor of two.

**Convertible bonds match issuer names.** Name-matching pulled `833445AB5` (a
Snowflake convertible note) and `25809KAB1` (a DoorDash note) alongside the real
equity CUSIPs. A fund's convert position is not an equity stake.

**A pinned CIK can silently point at the wrong book.** Of four funds whose
filings looked stale, three were resolved to the wrong entity and returned
perfectly parseable XML from it:

| Fund | Wrong CIK | Last 13F there | Correct CIK |
|---|---|---|---|
| Baupost | `1054420` (`/ADV`) | 2002-03-31 | `1061768` (`/MA`) |
| Marshall Wace | `1519964` (MW *Asia*) | 2021-09-30 | `1318757` (MW *LLP*) |
| Greenlight | `1079114` | 2023-12-31 | `1489933` (DME Capital) |
| Tybourne | `1553936` | 2025-09-30 | **wound down — dropped** |

Company-name search is not a reliable way to find a manager's filer: "Balyasny"
returns three entities that file no 13F at all, while the real filer
(`1218710`) files under two different legal names. **CIKs are pinned as a
checked-in constant and verified, never resolved by name at runtime.**

---

## 3. Module boundaries

EDGAR is a second upstream. `extract.py`'s charter is "yfinance frame shapes,
fiscal calendars", so EDGAR gets its own module rather than being wedged in.

| Module | Knows about | Deliberately does not know about |
|---|---|---|
| `edgar.py` *(new)* | EDGAR HTTP, 13F XML shapes, equity filtering | SQL, HTML, the universe |
| `history.py` | + `thirteenf`, `thirteenf_filings` | yfinance, HTML |
| `build_dashboard.py` | + `FUNDS`, wiring, two renders | — |
| `tools/build_cusip_map.py` *(new)* | one-off map build + price validation | the daily run |

`ThirteenFRow` is defined in `history.py` beside `MetricRow` and `HoldingRow`,
preserving the existing producer↔store contract. `FUNDS` lives in
`build_dashboard.py` beside `UNIVERSE` and is passed into `edgar.py`, so the
fetch layer is testable without either universe.

---

## 4. Schema

```sql
CREATE TABLE thirteenf (
  cik TEXT NOT NULL, fund TEXT NOT NULL, quarter TEXT NOT NULL,
  cusip TEXT NOT NULL, ticker TEXT, issuer TEXT, title_class TEXT,
  shares REAL, value REAL, ingested_at TEXT NOT NULL,
  PRIMARY KEY (cik, quarter, cusip)
);
CREATE TABLE thirteenf_filings (
  cik TEXT NOT NULL, fund TEXT NOT NULL, quarter TEXT NOT NULL,
  accession TEXT, filed_date TEXT, n_positions INTEGER, n_universe INTEGER,
  status TEXT NOT NULL, ingested_at TEXT NOT NULL,
  PRIMARY KEY (cik, quarter)
);
```

### 4.1 `thirteenf_filings` is load-bearing

This is the `runs` table argument, and the stakes are higher. `runs` exists
because "a failed run, a market holiday and a delisting all look identical in
the data: an absent row."

In 13F data, **a complete exit is encoded as the absence of a row** — and an
exit is the most interesting signal the dataset carries. Lone Pine liquidating
its entire NVDA stake and an EDGAR timeout are the same absence. Without a
separate record that the filing was successfully ingested, the dashboard
reports a fabricated exit every time the network hiccups.

Rows are stored at CUSIP grain, never pre-aggregated, so Alphabet's Class A and
Class C stay separable. QoQ deltas are **computed at query time** from adjacent
quarters and never stored; derived data does not belong in the store.

---

## 5. CUSIP resolution

13F reports CUSIP and issuer name, never a ticker, and CUSIP is licensed so no
free authoritative map exists. Two stages.

**Filter to equity**, three rules, each validated against live data:

- `sshPrnamtType == "SH"` — drops `PRN`, which is bond principal
- no `<putCall>` — drops the options half of the multi-strat books
- `cusip[6:8].isdigit()` — issue-type characters are digits for equity and
  letters for debt (`833445109` ✓ vs `833445AB5` ✗)

**Verify by price.** For each candidate CUSIP→ticker pair,
`median(value ÷ shares)` across all filers must agree with
`close(ticker, quarter_end)` from `history.db`, which holds 182,441 daily
closes back to 2021. Ten independent filers agreeing that NVDA is $200.09 on
2026-06-30 confirms the mapping. The same check catches filers that report
value in thousands rather than dollars, since they miss by exactly 1000x.

Output is `data/cusip_map.csv` (checked in, ~150 rows), carrying residual price
error and filer count per row so the **test suite asserts the map** rather than
trusting it. Names that do not auto-match are pinned manually once.

---

## 6. Cadence

Steady state is **zero EDGAR requests**. The run computes the expected quarter
(deadline = quarter end + 45 days), diffs it against `thirteenf_filings`, and
fetches only missing `(cik, quarter)` pairs: a burst in mid-Feb/May/Aug/Nov and
nothing on the other ~85 days of each quarter. A `13F-HR/A` amendment arriving
under a new accession for an already-ingested quarter triggers re-ingest,
because amendments restate rather than supplement.

First run backfills 8 quarters × 27 funds ≈ 216 filings at ~0.25s spacing.

**Amended during implementation.** Two things were needed to actually reach the
quiet state this section claims:

*A filing that does not exist must be recorded.* Viking Global filed nothing for
Q1 2026 and Pershing Square nothing for Q2. Counted as missing, they force a
sweep of all 27 funds on every run forever, since the absent filing never
arrives. They are now stored with status `no-filing` and count as settled. The
periodic re-check that catches amendments is bounded to
`THIRTEENF_RECHECK_DAYS`.

*The position archives are not a sufficient record of truth.* A filing that
reports no positions leaves no rows in them — Viking's Q1 2026 information table
was empty. Rebuilt from positions alone, the store forgets the filing happened,
and §4.1's invariant collapses: Viking's 15 prior holdings render as exits it
never made. `data/13f_filings.csv.gz` carries one row per filing looked at,
whatever the outcome, and `--rebuild-13f` reads it in preference to inferring
filings from positions.

---

## 7. Rendering

**Drilldown panel**, per company: fund, cohort, shares, value, Δshares, Δ%, and
a `NEW`/`ADD`/`TRIM`/`EXIT` badge, sorted by value. The header carries the
crowding line — "held by 16 of 27 funds, net +2.1M shares QoQ".

**Fund view**, a new tab: one fund's entire footprint across the 148 names,
with its largest adds and trims.

Both carry a **% of the fund's 13F book** column. This is how the multi-strat
problem is solved without special-casing: a top Lone Pine position reads ~8%,
while everything in Citadel's 16,127-line market-making book reads ~0.1%. One
column, and conviction is distinguishable from flow at a glance. Cohort labels
(Tiger / Multi-strat / Concentrated) reinforce it.

---

## 8. Error handling

Follows existing precedent: a per-fund failure must not lose the other funds (as
in `collect_ownership`), and the whole 13F step is non-fatal to the dashboard (as
in `record_history`). Two guards are new:

- **Staleness assertion** — a pinned CIK whose newest filing is more than two
  quarters behind the expected quarter fails loudly. This is exactly what
  silently broke on 3 of 28 funds during the probe.
- **Unmapped-CUSIP warning** — a CUSIP appearing across many filers with no map
  entry means the map has drifted from the universe.

---

## 9. Testing

Fixtures captured from the probe: bare-namespace (Tiger Global), `n1:`-namespaced
(Millennium), and an options/notes-heavy excerpt (Citadel). Tests cover
namespace-agnostic parsing, each equity filter rule, QoQ deltas including new and
exit, the **exit vs. missing-filing distinction**, and price validation of the
checked-in map. All offline, matching the existing no-network suite.

---

## 10. Retention: gzip the dated CSVs

Measured on 2026-08-20: `data/` is on track for **433 MB/yr**, and the content is
overwhelmingly redundant — only 152 of 11,228 estimate rows change day over day
(1.4%), and in `fundamentals` every price-derived column churns 147/148 while
the actual fundamentals move in 4–24 names.

Deleting old dated CSVs was considered and rejected. It would not lose data
today, since everything is already in `history.db` — it would delete the
*rebuild path*, inverting the guarantee the README rests on: "`history.db` is
derived from them and therefore disposable." The 45 MB SQLite file would become
the sole copy of the perishable estimates history, which is the one dataset here
that genuinely cannot be refetched.

These files compress 5.9x blended (estimates 8.8x, insiders 6.3x, holdings
4.1x, fundamentals only 2.2x), so:
`write_rows_csv`/`read_rows_csv` move to `.csv.gz`, reads fall back to plain
`.csv` so existing files keep working, and existing files are compressed once.
**433 → 73 MB/yr with nothing lost**, measured after migrating the
existing files rather than extrapolated from the best-compressing one.

`float_precision="round_trip"` is preserved through the change — that invariant
is the reason a rebuilt store equals the one it replaced. Acceptance test:
`--rebuild-history` from gzipped input produces an identical store.
