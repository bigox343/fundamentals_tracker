# Portfolio construction — design

**Date:** 2026-08-21
**Status:** draft, awaiting review

Turn the tracker's cross-sectional signals into a dollar- and factor-neutral
long/short book: a `cvxpy` optimizer over the 153-name universe, driven by a
composite alpha score and a Fama-French factor risk model, with a walk-forward
backtest and a persisted daily weight record.

---

## 1. Why this is a new module

Everything the repo does today is **descriptive**: it scores each name against
its sub-industry peers and renders the result. Nothing in it takes a position.
Portfolio construction is the first component that produces an *action* — a
target weight per name — and it is the first that can be wrong in a way that
costs money rather than costing a chart.

That difference drives two design choices that would otherwise look like
over-engineering:

- It stays **out of the daily critical path**. `record_history()` is already
  deliberately non-fatal so a store failure cannot cost you the dashboard. A
  solver failure has the same status: it must not be able to break a run.
- It **records what it recommended, every time**, before anyone acts on it.
  Alpha signals that are only ever evaluated in-sample are indistinguishable
  from numerology. The forward record is the only honest evidence this will
  ever have (§9).

---

## 2. Evidence

Measured against `data/history.db` on 2026-08-21, not reasoned about in advance.

### 2.1 Signal history is far shorter than price history

| Source | Range | Distinct dates |
|---|---|---:|
| Daily closes | 2021-08-16 → 2026-08-21 | 1,260 |
| Insider transactions | 2024-08-15 → 2026-08-19 | 481 |
| 13F positioning | 2024Q3 → 2026Q2 | 8 quarters |
| `epsEst` consensus | 2026-05-17 → 2026-08-21 | **25** |
| Revision breadth (`epsRevUp30`/`epsRevDown30`) | 2026-08-15 → 2026-08-21 | **5** |

This is the single most important fact in this document. Estimate revisions are
the strongest and best-documented of the candidate signals, and they have **five
days of history**. This is the perishability asymmetry the README describes,
observed directly: the closes go back five years because they are recoverable at
any time, and the revisions go back five days because they are not.

**Consequence:** the revision leg cannot be fitted, validated, or even sanity
checked today. It is deferred to v2 (§11), and v1 is built only from signals
with real history. The plumbing is identical; only the column list in the alpha
blend changes.

### 2.2 The universe is well covered but thin per industry

- 153 tickers, with effectively complete close history for all but one (median
  1,260 days, 10th percentile 1,259). **One has 6 observations** (a recent
  addition) and must be excluded rather than allowed to produce a variance
  estimate from six points.
- 3 sectors: TMT 81, Industrials 40, Consumer 32.
- 20 sub-industries: median 7 names, **smallest 4** (Telecom), largest 17
  (Semiconductors).

Barra-style commercial models run ~60 industries over thousands of names —
roughly 30-50 names per bucket. Twenty sub-industry factors here would average
7.6, and a 4-name industry factor return is essentially those four stocks'
mean. It would absorb stock-specific moves into "industry risk" and flatter the
reported specific-risk share. Sector-level industry factors (32+ names each) are
estimable; sub-industry is used where it is sound — as the peer group for
z-scoring and as an optimizer constraint (§6) — but never as an estimated factor.

### 2.3 Commercial risk models are not available

Barra (MSCI), Axioma, Northfield and Bloomberg PORT are licensed products with
no free or retail tier, and their licenses generally do not extend to personal
projects. The risk model is therefore built behind an interface returning
`(B, F, D)`, so a vendor model can be substituted without the optimizer
changing (§5.4).

---

## 3. Module boundaries

Following the existing contract in the README:

| File | Knows about | Deliberately does not know about |
|---|---|---|
| `portfolio.py` | Signal construction, factor risk model, the cvxpy problem | Fetching, HTML, the daily run |
| `factors.py` | The Ken French data library, factor CSV shapes | SQL, HTML, optimization |
| `tools/build_portfolio_report.py` | Rendering one solved book as a page | Fetching, solving |
| `tools/backtest_portfolio.py` | Walk-forward evaluation | Fetching, HTML |

`portfolio.py` reads from `data/history.db` and returns dataframes. It never
writes HTML and never fetches. Persistence goes through a new function in
`history.py`, keeping SQL knowledge where it already lives — the same reason
`MetricRow` is defined there and imported by `extract.py`.

`factors.py` is separate from `extract.py` because it is the only component that
talks to a non-Yahoo, non-EDGAR source, and because factor returns are a
market-wide series with no ticker grain — they do not belong in `metrics`.

---

## 4. Signal construction (μ)

Three legs, each z-scored **within sub-industry** (matching how the dashboard
already scores), winsorized at ±3σ, then equal-weighted:

| Leg | Definition | Source |
|---|---|---|
| `z_momentum` | 12-month return skipping the most recent month | stored closes |
| `z_13f` | QoQ change in aggregate share count held across the 27 tracked managers | `thirteenf` |
| `z_insider` | Net open-market insider buying over trailing 6 months, value-weighted | `insider_txns` |

Details that are load-bearing:

- **Momentum skips the most recent month.** The 12-1 construction is standard
  because the most recent month carries short-term reversal, which fights the
  medium-term momentum effect. Including it degrades the signal.
- **13F share counts must be split-adjusted before differencing.** Counts are
  as-filed while stored closes are back-adjusted; the README records that a 25:1
  split otherwise renders as a manager adding 2,400%. The same trap applies to a
  QoQ *change*, more severely — it is the difference that explodes.
- **Insider transactions are filtered to open-market buys and sells.** Option
  exercises, grants and tax withholding carry no information about conviction
  and would dominate by count.
- **Staleness decays rather than persists.** Each leg carries its own `as_of`.
  13F data is 45+ days stale the day it arrives and updates quarterly; past its
  validity window a signal decays to zero rather than holding stale conviction
  indefinitely. Forward-filling a quarterly signal daily would otherwise present
  one observation as ninety.

Equal weighting in v1 is a deliberate prior, not an estimate. §2.1 means there
is not enough history to fit three weights without overfitting, and a fitted
weight presented as an estimate would be worse than an honest prior.

---

## 5. Risk model (Σ)

A structural factor model replaces sample covariance:

```
Σ = B F Bᵀ + D
```

### 5.1 Factors

Nine, all observable:

- **FF5** — Mkt-RF, SMB, HML, RMW, CMA (Ken French data library, daily)
- **UMD** — momentum (same source)
- **3 sector dummies** — TMT, Industrials, Consumer

Parameter count is the point. A full 153×153 sample covariance has **11,781**
free parameters estimated from 504 observations. The factor model has
1,377 loadings + 45 factor-covariance terms + 153 specific variances =
**1,575**, and is positive-definite by construction.

### 5.2 UMD is coupled to the momentum alpha leg

`z_momentum` (§4) is an explicit alpha leg. If UMD is absent from `B`, the
optimizer cannot see momentum as a *risk* factor and will read the book's
momentum loading as alpha, sizing into it while reporting the exposure as
specific risk.

**Therefore: UMD and `z_momentum` are included or excluded together.** Strict
FF5 is a defensible choice, but it requires dropping the momentum alpha leg too.
This is a single config flag; the coupling is recorded here so the trade-off
cannot be made accidentally.

### 5.3 Estimation

- Window: **504 trading days**, balancing conditioning against the assumption
  that covariance is stationary over the window.
- `B` by time-series regression of each name's excess return on the nine factors.
- `F` as the sample covariance of factor returns over the same window.
- `D` from regression residual variances, floored at a small positive value so no
  name is treated as riskless.
- **Universe filter:** require ≥400 of the trailing 504 days present. This drops
  the 6-observation ticker in §2.2 and any future addition until it seasons.

### 5.4 Substitution interface

The model is produced by one function returning `(B, F, D)` with a documented
index contract. Any vendor model — or the PCA alternative — can be dropped in
without the optimizer changing.

### 5.5 Known limitations, stated rather than discovered later

- **SMB will contribute little.** The universe is 153 large caps; there is
  minimal size dispersion for the factor to price.
- **HML and CMA will be weak** in a TMT-heavy universe.
- Market, UMD and the sector dummies are expected to carry most of the
  explanatory power. The backtest reports variance explained per factor so this
  is measured rather than assumed.

---

## 6. The optimization problem

```
maximize    μᵀw  −  κ·‖w − w_prev‖₁

subject to  wᵀΣw ≤ σ_target²           ex-ante vol cap, 8% annualized
            Bᵀw = 0                     factor neutral (all 9)
            1ᵀw = 0                     dollar neutral
            ‖w‖₁ ≤ 2.0                  gross ≤ 200%
            |wᵢ| ≤ 0.04                 position cap
            |Σ_{i∈subindustry} wᵢ| ≤ 0.10   sub-industry band
```

Convex (a QCQP), and trivial at N=153.

**No `λ·wᵀΣw` penalty term.** μ is in z-score units, not return units, so λ has
no natural scale and tuning it means turning a knob with no interpretation. A
hard volatility constraint sets the scale determinately and is a quantity that
can actually be reasoned about. This is the one place the formulation departs
from textbook mean-variance, and it does so deliberately.

**`Bᵀw = 0` subsumes the earlier `βᵀw = 0`.** Market beta is the first column of
`B`, so market neutrality falls out of factor neutrality rather than being a
separate constraint.

**The turnover penalty is required, not optional.** 13F updates quarterly and
insider filings arrive sporadically; without κ, a monthly rebalance churns on
signal staleness rather than signal change.

---

## 7. Schema

```sql
target_weights (as_of, ticker, weight, mu, contrib_momentum,
                contrib_13f, contrib_insider, solver_status)
PRIMARY KEY (as_of, ticker)
```

Per-leg contributions are stored, not just the blended μ, so the forward record
can attribute performance to individual signals later. Without them the record
answers "did the book work" but never "which leg worked" — and the second
question is the one that determines what v2 should keep.

`solver_status` is stored so a relaxed or failed solve is visible in the record
rather than looking like a normal day (§8).

---

## 8. Error handling

**Infeasibility is expected, not exceptional.** Factor neutrality across nine
factors plus a vol cap plus position caps plus sub-industry bands can
over-constrain, especially on days when the signal is concentrated.

The solver relaxes in a documented, recorded order:

1. Sub-industry band
2. Gross exposure cap
3. Volatility target

**Dollar and factor neutrality are never relaxed.** They define what the
portfolio *is*; a book that quietly stopped being market neutral is worse than
no book. If the problem is still infeasible after step 3, the solve fails, the
previous weights stand, and the shortfall is reported — mirroring how
`report['prices']` surfaces gaps rather than hiding them.

Every relaxation is written to `solver_status`, so a run that only solved
because the vol cap was loosened is distinguishable afterwards from one that
solved cleanly.

---

## 9. Backtest

Monthly rebalance, 2024-09 → 2026-08, bounded by 13F availability (§2.1).

- **Point-in-time throughout.** Signals reconstructed from `as_of` ≤ the
  rebalance date; `B`, `F`, `D` from the trailing 504 days only.
- **10 bps per unit turnover** assumed.
- Reports IR, turnover, max drawdown, factor exposures, and per-leg attribution.

**What this backtest can and cannot establish.** 24 monthly observations with 7
independent 13F changes is a thin sample. It will detect a catastrophically
broken signal or a lookahead bug. It will **not** reliably distinguish a good
signal from a mediocre one, and no amount of presentation should imply
otherwise. It is a sanity check, not evidence. The forward record in §7 is the
real evidence, and it starts accumulating the day this ships.

---

## 10. Testing

Fixture-driven and no-network, matching `tests/`:

- **Signals** — 12-1 momentum against a known series; split-adjustment on a
  fixture containing a real split; stale-13F decay; insider filtering excludes
  grants and option exercises.
- **Risk model** — `Σ` positive-definite; factor model better-conditioned than
  sample covariance on the same window; `D` floor respected.
- **Optimizer** — every constraint verified to hold in the returned solution; a
  known-analytic case solved correctly; infeasibility relaxes in exactly the
  §8 order and records it.
- **No lookahead** — a shuffled-future signal must produce no backtest edge.
  This is the test most likely to catch a real bug, because point-in-time
  reconstruction across three sources with different cadences is where lookahead
  hides.

---

## 11. Deferred

- **`z_revision` as a fourth alpha leg** — once ~12 months of revision history
  has accumulated (§2.1). No re-architecture; one entry in the blend config.
- **Fitting the blend weights** — requires the same history. Equal weight until
  then.
- **Wiring the solve into the daily run** as a non-fatal call, once the
  optimizer has proven stable standing alone.
- **Sub-industry or merged-group industry factors** — if the universe grows
  enough that buckets reach ~30 names (§2.2).
