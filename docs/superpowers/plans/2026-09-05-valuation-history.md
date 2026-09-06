# Valuation History and Change Frames — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every cell on `dashboard.html` three reference frames instead of one — against peers, against its own multi-year history, and against its own recent change — and fix a live scoring bug that currently renders undefined ratios as the most favorable values in their band.

**Architecture:** A new `xbrl.py` pulls point-in-time fundamentals from SEC XBRL (facts carry the date they were filed) into a new `reported` table. A new `valuation.py` combines those aggregates with daily prices to produce a five-year daily series for each multiple, in memory, never stored. `render.py` is extracted from `build_dashboard.py` first so the render work has a focused home. The dashboard gains a `frame` select that re-paints the existing table rather than adding columns.

**Tech Stack:** Python 3.12, pandas, yfinance, sqlite3, `urllib` against `data.sec.gov` (no key, no new dependency). Tests are pytest against captured fixtures, no network.

**Spec:** [docs/superpowers/specs/2026-09-05-valuation-history-design.md](../specs/2026-09-05-valuation-history-design.md)

## Global Constraints

- **No new runtime dependency.** SEC access uses `urllib` via the existing `edgar` HTTP path. Runtime deps stay `pandas` + `yfinance`.
- **Tests never touch the network.** Every network-touching helper stays at the bottom of its module, as in `extract.py` and `edgar.py`. 229 tests pass today and must pass after every task.
- **A missing value writes no row.** Non-finite values are dropped at the write boundary. "Never observed" must stay distinct from a stored zero.
- **CSV reads use `float_precision="round_trip"`.** A rebuilt store must equal the one it replaced.
- **The backfill never runs inside `build_dashboard.py`.** A SEC outage must not cost the dashboard, same rule as `portfolio.py`.
- **SEC requests carry `edgar.USER_AGENT`** (`fundamentals_tracker (li.gang.nju@gmail.com)`) and respect a 10 req/s ceiling.
- **Multiples are built from aggregates, never per-share figures.** The share basis enters exactly once, in market cap.
- **Proof tolerance is 1% median relative error.** A `(ticker, metric)` pair that fails gets no own-history frame rather than a wrong one.
- **The change/arrow deadband is 0.5%**, one constant governing both the direction arrow and what counts as a `dln F` data event.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `render.py` | create (extracted) | CSS, JS template, toolbar, cell construction, page assembly |
| `xbrl.py` | create | SEC XBRL fact shapes, concept chains, duration disambiguation, filing dates |
| `valuation.py` | create | Point-in-time aggregates + prices -> daily multiple series, own-range percentiles |
| `tools/backfill_xbrl.py` | create | The initial sweep and the incremental top-up |
| `build_dashboard.py` | modify | Loses the render layer; gains `DOMAINS`, `closeRaw` capture, frame wiring |
| `history.py` | modify | Gains the `reported` table, its upsert, and its readers |
| `extract.py` | modify | Gains `closeRaw` to `NUMERIC_METRICS`-adjacent daily capture |
| `edgar.py` | modify | `_get` promoted to `sec_get` (one-line rename plus call sites) |
| `tests/test_render.py` | create | The render layer, which has no tests today |
| `tests/test_xbrl.py` | create | Concept chains, durations, Q4 reconstruction, amendments |
| `tests/test_valuation.py` | create | TTM point-in-time, split adjustment, series, percentiles |
| `tests/test_domains.py` | create | The scoring domain guard |
| `tests/fixtures/xbrl_*.json` | create | Captured SEC responses covering each trap |

---

## Task 1: Extract the render layer into `render.py`

**Files:**
- Create: `render.py`
- Create: `tests/test_render.py`
- Modify: `build_dashboard.py` (remove the moved names, import them back)

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `render.CSS`, `render.JS_TMPL`, `render.TOOLBAR`, `render.fmt(val, kind) -> str`, `render.parse_spark(raw) -> list[float]`, `render.sparkline(raw, ret, label, w, h) -> str`, `render.spark_cell(row) -> str`, `render.cell_style(score) -> str`, `render.band_perf(band, proxies) -> str`, `render.render_sector(sector, df, proxies, metrics, universe, group_labels) -> str`, `render.render_spx(spx, df, proxies) -> str`, `render.build_js(metrics) -> str`, `render.render_html(df, spx, proxies, metrics, universe, group_labels) -> str`

Later tasks extend both signatures: Task 2 adds `domains`, Task 12 adds `own=`, Task 14 adds `changes=`, Task 15 adds `events=`.

**Note on direction:** `render.py` must not import `build_dashboard` — that would be a cycle. `METRICS`, `GROUP_LABELS` and `UNIVERSE` are passed in as arguments instead. This is the one behavioural difference in an otherwise verbatim move.

- [ ] **Step 1: Capture the current output as a baseline**

```bash
python build_dashboard.py --no-fetch
cp dashboard.html /tmp/dashboard-baseline.html
wc -c /tmp/dashboard-baseline.html
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_render.py`:

```python
import math

import pandas as pd
import pytest

import render


def test_fmt_handles_each_kind():
    assert render.fmt(12.345, "x") == "12.3x"
    assert render.fmt(1.5e12, "bigusd") == "$1.50T"
    assert render.fmt(42.0, "pct") == "42.0%"
    assert render.fmt(9.5, "usd") == "$9.50"


def test_fmt_renders_missing_as_a_dash():
    assert "na" in render.fmt(None, "x")
    assert "na" in render.fmt(float("nan"), "x")


def test_cell_style_is_blue_above_zero_and_red_below():
    assert "37,106,191" in render.cell_style(0.8)
    assert "208,59,59" in render.cell_style(-0.8)
    assert render.cell_style(None) == ""


def test_cell_style_alpha_saturates_at_the_documented_ceiling():
    assert "0.340" in render.cell_style(1.0)
    assert "0.340" in render.cell_style(5.0)
```

- [ ] **Step 3: Run it to make sure it fails**

Run: `python -m pytest tests/test_render.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'render'`

- [ ] **Step 4: Create `render.py` and move the names into it**

Move these verbatim from `build_dashboard.py` into a new `render.py`: the `CSS`
string, `JS_TMPL`, `TOOLBAR`, `fmt`, `parse_spark`, `sparkline`, `spark_cell`,
`cell_style`, `band_perf`, `render_sector`, `render_spx`, `build_js`,
`render_html`.

`render.py` starts:

```python
"""Render the dashboard as a self-contained HTML page.

Holds every string and function that turns a scored DataFrame into markup, so
build_dashboard.py is left with the universe, the fetch and the run flow.

Knows nothing about where the data came from. METRICS, GROUP_LABELS and
UNIVERSE are passed in rather than imported, because importing them would make
a cycle with build_dashboard.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
```

- [ ] **Step 5: Change the moved signatures to take what they used to import**

`render_sector`, `build_js` and `render_html` referenced module globals. Give
them parameters:

```python
def render_sector(sector: str, df, proxies: dict,
                  metrics: list, universe: dict, group_labels: dict) -> str:
    # body unchanged, except METRICS -> metrics, UNIVERSE -> universe,
    # GROUP_LABELS -> group_labels


def build_js(metrics: list) -> str:
    keys = [k for k, _l, _g, _f, _hb in metrics]
    labels = [l for _k, l, _g, _f, _hb in metrics]
    return (JS_TMPL
            .replace("__KEYS__", json.dumps(keys))
            .replace("__LABELS__", json.dumps(labels))
            .replace("__STAMP__", json.dumps(datetime.now().strftime("%Y%m%d"))))


def render_html(df, spx: dict, proxies: dict,
                metrics: list, universe: dict, group_labels: dict) -> str:
    # body unchanged, except it calls render_sector(...) with the passed-in
    # metrics/universe/group_labels and build_js(metrics)
```

- [ ] **Step 6: Import them back in `build_dashboard.py`**

Delete the moved definitions and add near the other imports:

```python
import render
from render import cell_style, fmt, render_html, spark_cell, sparkline
```

Update the one call site of `render_html` in `main()`/`_run()`:

```python
html = render.render_html(df, spx, proxies, METRICS, UNIVERSE, GROUP_LABELS)
```

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests/ -q`
Expected: 233 passed (229 existing + 4 new)

- [ ] **Step 8: Prove the move changed no output**

```bash
python build_dashboard.py --no-fetch
diff <(sed 's/Data as of.*//' /tmp/dashboard-baseline.html) \
     <(sed 's/Data as of.*//' dashboard.html) && echo "IDENTICAL"
```

Expected: `IDENTICAL`. The `sed` strips the timestamp line, which is the only
legitimate difference between two renders of the same data.

If it is not identical, the move was not verbatim. Find the difference before
continuing — every later task assumes this baseline.

- [ ] **Step 9: Commit**

```bash
git add render.py build_dashboard.py tests/test_render.py
git commit -m "refactor: extract the render layer into render.py

build_dashboard.py held the universe, the roster, the fetch, the scoring, the
CSS, the JS template, every render function and the run flow in 1,569 lines, and
the valuation-history work roughly doubles the JS. The render layer moves out
verbatim; METRICS, UNIVERSE and GROUP_LABELS are passed in rather than imported
so there is no cycle. Output is byte-identical apart from the timestamp.

It also had no tests. It has four now."
```

---

## Task 2: Metric validity domains

**Files:**
- Modify: `build_dashboard.py` (add `DOMAINS`, change `relative_scores`)
- Create: `tests/test_domains.py`

**Interfaces:**
- Consumes: `render.cell_style` from Task 1
- Produces: `build_dashboard.DOMAINS: dict[str, Callable[[pd.DataFrame], pd.Series]]`, and `relative_scores(df, key, higher_better, domain=None) -> dict`

**Why:** spec §2.2 and §4. `relative_scores` scores distance from the band
median in MAD units and clips to [-1, 1]. An undefined ratio — a negative
EV/EBITDA — lands far below the median, clips to `-1.0`, and is negated by
`higher_better=False` into `+1.0`, the most favorable score available. NET's
`-27,888x` renders as the cheapest name in Software — Infrastructure. It also
drags the band's median, costing every *other* name in the band about half the
scale.

- [ ] **Step 1: Write the failing test**

Create `tests/test_domains.py`:

```python
import pandas as pd
import pytest

import build_dashboard as bd


def _band():
    """Software — Infrastructure as it stood on 2026-09-04, EV/EBITDA."""
    return pd.DataFrame(
        {"evEbitda": [-27887.75, -93.92, 19.18, 19.77, 42.84,
                      145.42, 462.38, 885.52, 2020.94, 2065.37]},
        index=["NET", "SNOW", "ORCL", "MSFT", "FTNT",
               "PANW", "ZS", "DDOG", "CRWD", "MDB"],
    )


def test_negative_ev_ebitda_is_not_scored_at_all():
    df = _band()
    scores = bd.relative_scores(df, "evEbitda", False, bd.DOMAINS["evEbitda"])
    assert "NET" not in scores
    assert "SNOW" not in scores


def test_excluding_them_repairs_the_rest_of_the_band():
    df = _band()
    before = bd.relative_scores(df, "evEbitda", False)
    after = bd.relative_scores(df, "evEbitda", False, bd.DOMAINS["evEbitda"])
    # ORCL and MSFT are the cheapest real names and were barely tinted.
    assert before["ORCL"] == pytest.approx(0.18, abs=0.02)
    assert after["ORCL"] == pytest.approx(0.67, abs=0.02)
    assert after["ORCL"] > before["ORCL"] + 0.4


def test_net_debt_ebitda_keys_off_the_ebitda_sign_not_its_own():
    # BA: ~$50B net debt against negative EBITDA renders as -10.02, which reads
    # as net cash. GD's 0.78 is a real, positive-EBITDA ratio.
    df = pd.DataFrame({"netDebtEbitda": [-10.02, 0.78, 1.47],
                       "evEbitda": [-67.40, 15.68, 38.65]},
                      index=["BA", "GD", "HWM"])
    scores = bd.relative_scores(df, "netDebtEbitda", False,
                                bd.DOMAINS["netDebtEbitda"])
    assert "BA" not in scores
    assert "GD" in scores


def test_a_genuinely_negative_metric_is_still_scored():
    # A negative FCF yield or margin is meaningful, not undefined.
    df = pd.DataFrame({"fcfYield": [-2.0, 1.0, 3.0, 5.0]},
                      index=["A", "B", "C", "D"])
    scores = bd.relative_scores(df, "fcfYield", True, bd.DOMAINS.get("fcfYield"))
    assert set(scores) == {"A", "B", "C", "D"}


def test_a_band_that_falls_under_three_valid_names_scores_nothing():
    df = pd.DataFrame({"evEbitda": [-5.0, -3.0, 12.0]}, index=["A", "B", "C"])
    assert bd.relative_scores(df, "evEbitda", False,
                              bd.DOMAINS["evEbitda"]) == {}
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_domains.py -v`
Expected: FAIL with `AttributeError: module 'build_dashboard' has no attribute 'DOMAINS'`

- [ ] **Step 3: Add `DOMAINS` above `relative_scores` in `build_dashboard.py`**

```python
# --------------------------------------------------------------------------- #
# Scoring domains                                                              #
# --------------------------------------------------------------------------- #
# A ratio whose denominator is zero or negative is not an extreme value, it is
# an absent one, and relative_scores() cannot tell the difference: a negative
# EV/EBITDA lands far below the band median, clips to -1.0, and is negated by
# higher_better=False into +1.0 -- the most favorable score on the scale. NET
# rendered as the cheapest name in Software — Infrastructure on -27,888x.
#
# Excluding these also repairs the rest of the band, because med and mad are
# computed over the surviving values. Dropping NET and SNOW moved that band's
# median from 94.13 to 303.90 and every remaining name by ~0.49 of scale.
#
# Each entry maps a metric to a predicate over the whole frame, because one of
# them needs a second column: net debt over a *negative* EBITDA is negative,
# which reads as net cash, so netDebtEbitda's domain keys off the sign of
# evEbitda rather than its own. Boeing, ~$50B net debt against negative EBITDA,
# scored 1.00 -- the safest balance sheet in Aerospace & Defense.
#
# Not guarded, deliberately: a large negative ND/EBITDA arising from a small but
# *positive* EBITDA (MDB at -170x) is a real ratio describing a real net cash
# position. The clip already bounds it.
def _positive(key: str):
    return lambda df: pd.to_numeric(df[key], errors="coerce") > 0


DOMAINS: dict = {
    "forwardPE":     _positive("forwardPE"),
    "trailingPE":    _positive("trailingPE"),
    "evEbitda":      _positive("evEbitda"),
    "ps":            _positive("ps"),
    "netDebtEbitda": _positive("evEbitda"),
}
```

- [ ] **Step 4: Give `relative_scores` the optional domain**

```python
def relative_scores(df: pd.DataFrame, key: str, higher_better: bool | None,
                    domain=None) -> dict:
    """Signed z-ish score per row within a sector, clipped to [-1,1].

    `domain` is a predicate over the frame marking which rows carry a
    *meaningful* value. Rows outside it are dropped before med/mad are taken,
    so an undefined ratio neither scores itself nor shifts its peers.
    """
    if higher_better is None:
        return {}
    col = pd.to_numeric(df[key], errors="coerce")
    if domain is not None:
        col = col.where(domain(df))
    valid = col.dropna()
    if len(valid) < 3:
        return {}
    med, mad = valid.median(), (valid - valid.median()).abs().median()
    spread = mad if mad and mad > 0 else (valid.std() or 1)
    scores = {}
    for idx, v in col.items():
        if pd.isna(v):
            continue
        z = (v - med) / (1.5 * spread)
        z = max(-1.0, min(1.0, z))
        scores[idx] = z if higher_better else -z
    return scores
```

- [ ] **Step 5: Pass the domain at both call sites in `render_sector`**

In `render.py`, both score dictionaries take it:

```python
    sec_scores = {
        key: relative_scores(tdf, key, hb, domains.get(key))
        for key, _l, _g, _f, hb in metrics if hb is not None
    }
    ...
        sub_scores = {
            key: relative_scores(sdf, key, hb, domains.get(key))
            for key, _l, _g, _f, hb in metrics if hb is not None
        }
```

`render_sector` and `render_html` take `domains: dict` as a new parameter, and
`build_dashboard` passes `DOMAINS`. `relative_scores` moves to `render.py`
alongside the call sites, and `build_dashboard` imports it back for the tests
above — keep `import render` plus `from render import relative_scores`.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/ -q`
Expected: 238 passed

- [ ] **Step 7: Confirm the change on real data**

```bash
python build_dashboard.py --no-fetch
python - <<'EOF'
import re
h = open("dashboard.html").read()
row = re.search(r"data-tk=\"NET\".*?</tr>", h, re.S).group(0)
cell = re.search(r"<td class='num g-val'[^>]*>[^<]*-27[^<]*</td>", row)
print("NET EV/EBITDA cell:", cell.group(0) if cell else "not found")
assert "data-ss" not in (cell.group(0) if cell else ""), "still carries a score"
print("OK - NET is no longer scored on EV/EBITDA")
EOF
```

- [ ] **Step 8: Commit**

```bash
git add build_dashboard.py render.py tests/test_domains.py
git commit -m "fix: stop scoring ratios whose denominator is undefined

relative_scores() scores distance from the band median in MAD units and clips to
[-1,1], so a negative EV/EBITDA lands below the median, clips to -1.0, and is
negated by higher_better=False into +1.0 -- the most favorable score there is.
NET rendered as the cheapest name in Software — Infrastructure on -27,888x, and
SNOW ranked cheaper than MSFT. ND/EBITDA is worse: the sign inverts rather than
the magnitude exploding, and BA's ~\$50B of net debt against negative EBITDA
scored it the safest balance sheet in Aerospace & Defense.

21 of 153 names on EV/EBITDA, 10 on trailing P/E, 8 on forward P/E.

The damage was not confined to those cells. med and mad were taken over the
column including them, so dropping NET and SNOW moves that band's median from
94.13 to 303.90 and every surviving name by about half the scale: ORCL and MSFT
at 19x go from 0.18 to 0.67. The guard restores the signal on the ~130 good
cells, not only the 21 bad ones."
```

---

## Task 3: Render out-of-domain cells as visibly undefined

**Files:**
- Modify: `render.py` (`render_sector` cell loop, `CSS`)
- Modify: `tests/test_render.py`

**Interfaces:**
- Consumes: `DOMAINS` and the domain-aware `relative_scores` from Task 2
- Produces: cells carrying `class="num g-val undef"` and a `title` attribute

**Why:** after Task 2 an undefined cell is merely untinted, which is
indistinguishable from a thin peer set. It should say why.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_render.py`:

```python
def test_an_out_of_domain_cell_is_marked_and_explained():
    df = pd.DataFrame({
        "ticker": ["NET", "ORCL", "MSFT", "FTNT"],
        "name": ["Cloudflare", "Oracle", "Microsoft", "Fortinet"],
        "subindustry": ["Infra"] * 4,
        "evEbitda": [-27887.75, 19.18, 19.77, 42.84],
    }).set_index("ticker", drop=False)
    metrics = [("evEbitda", "EV/EBITDA", "val", "x", False)]
    html = render.render_sector(
        "TMT", df, {}, metrics, {"TMT": ["Infra"]}, {"val": "Valuation"},
        {"evEbitda": lambda d: pd.to_numeric(d["evEbitda"], errors="coerce") > 0},
    )
    net = html[html.index('data-tk="NET"'):]
    net = net[:net.index("</tr>")]
    assert "undef" in net
    assert "title=" in net
    assert "data-ss" not in net


def test_an_in_domain_cell_is_not_marked():
    df = pd.DataFrame({
        "ticker": ["ORCL", "MSFT", "FTNT"],
        "name": ["Oracle", "Microsoft", "Fortinet"],
        "subindustry": ["Infra"] * 3,
        "evEbitda": [19.18, 19.77, 42.84],
    }).set_index("ticker", drop=False)
    metrics = [("evEbitda", "EV/EBITDA", "val", "x", False)]
    html = render.render_sector(
        "TMT", df, {}, metrics, {"TMT": ["Infra"]}, {"val": "Valuation"},
        {"evEbitda": lambda d: pd.to_numeric(d["evEbitda"], errors="coerce") > 0},
    )
    assert "undef" not in html
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_render.py -k undef -v`
Expected: FAIL — `assert 'undef' in net`

- [ ] **Step 3: Mark the cell in the `render_sector` loop**

Replace the cell-building block:

```python
            for key, _lab, g, f, _hb in metrics:
                v = r.get("ret6m") if f == "spark" else r.get(key)
                attrs = ""
                if isinstance(v, (int, float)) and not (
                    isinstance(v, float) and math.isnan(v)
                ):
                    attrs += f" data-v='{v}'"
                ss = sub_scores.get(key, {}).get(tk)
                sc = sec_scores.get(key, {}).get(tk)
                if ss is not None:
                    attrs += f" data-ss='{ss:.4f}'"
                if sc is not None:
                    attrs += f" data-sc='{sc:.4f}'"
                # A metric with a domain, holding a value, that still scored
                # nothing, is outside its domain -- an undefined ratio rather
                # than a thin peer set. Say so instead of leaving it blank.
                cls = f"num g-{g}"
                if (key in domains and ss is None
                        and isinstance(v, (int, float))
                        and not (isinstance(v, float) and math.isnan(v))):
                    cls += " undef"
                    attrs += (" title=\"Denominator is zero or negative — "
                              "this ratio is undefined and is excluded from "
                              "peer scoring\"")
                cell = spark_cell(r) if f == "spark" else fmt(v, f)
                body += f"<td class='{cls}'{attrs}{cell_style(ss)}>{cell}</td>"
```

- [ ] **Step 4: Add the style to `CSS`**

```css
td.undef{color:var(--muted);font-style:italic;opacity:.65;cursor:help;}
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/ -q`
Expected: 240 passed

- [ ] **Step 6: Commit**

```bash
git add render.py tests/test_render.py
git commit -m "feat: mark undefined ratios instead of leaving them blank

After the domain guard an excluded cell is merely untinted, which looks the same
as a thin peer set. It now carries .undef and a title saying the denominator is
zero or negative and the ratio is excluded from peer scoring."
```

---

## Task 4: Promote the SEC getter and resolve tickers to CIKs

**Files:**
- Modify: `edgar.py` (rename `_get` -> `sec_get`, update call sites)
- Create: `xbrl.py` (the CIK map only)
- Create: `tests/test_xbrl.py`
- Create: `tests/fixtures/company_tickers_sample.json`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `edgar.sec_get(url: str, tries: int = 3) -> bytes`, `xbrl.parse_ticker_map(raw: bytes) -> dict[str, str]` returning ticker -> zero-padded 10-digit CIK, `xbrl.ticker_cik_map() -> dict[str, str]` (network, bottom of module)

- [ ] **Step 1: Create the fixture**

`tests/fixtures/company_tickers_sample.json` — the real shape, five entries:

```json
{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."},
 "1":{"cik_str":789019,"ticker":"MSFT","title":"MICROSOFT CORP"},
 "2":{"cik_str":1045810,"ticker":"NVDA","title":"NVIDIA CORP"},
 "3":{"cik_str":1730168,"ticker":"AVGO","title":"Broadcom Inc."},
 "4":{"cik_str":1477333,"ticker":"NET","title":"Cloudflare, Inc."}}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_xbrl.py`:

```python
import json
from pathlib import Path

import pytest

import xbrl

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_ticker_map_zero_pads_to_ten_digits():
    raw = (FIXTURES / "company_tickers_sample.json").read_bytes()
    m = xbrl.parse_ticker_map(raw)
    assert m["AAPL"] == "0000320193"
    assert m["NVDA"] == "0001045810"
    assert len(m["NET"]) == 10


def test_parse_ticker_map_covers_every_entry():
    raw = (FIXTURES / "company_tickers_sample.json").read_bytes()
    assert set(xbrl.parse_ticker_map(raw)) == {
        "AAPL", "MSFT", "NVDA", "AVGO", "NET"}
```

- [ ] **Step 3: Run it to make sure it fails**

Run: `python -m pytest tests/test_xbrl.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'xbrl'`

- [ ] **Step 4: Rename the getter in `edgar.py`**

`_get` becomes `sec_get`, with a docstring saying why it is public:

```python
def sec_get(url: str, tries: int = 3) -> bytes:
    """Fetch from SEC with the polite headers, gzip handling and backoff.

    Public because xbrl.py needs the same manners against data.sec.gov. That is
    a shared contract crossing a module line, the same precedent as MetricRow
    living in history.py and being imported by extract.py.
    """
```

Update its call sites inside `edgar.py`:

```bash
grep -n "_get(" edgar.py
# expect the definition plus the calls in submissions/filing_index/fetch paths
sed -i '' 's/\b_get(/sec_get(/g' edgar.py
grep -n "sec_get" edgar.py
```

- [ ] **Step 5: Create `xbrl.py`**

```python
"""SEC XBRL company facts: point-in-time fundamentals with filing dates.

A different SEC API from the 13F information tables in edgar.py, and a
different shape: facts keyed by us-gaap concept, each carrying the period it
covers *and the date it was filed*. That second date is the whole reason this
module exists -- it is what makes a historical valuation series free of
lookahead. yfinance serves period ends only.

Knows nothing about SQL, HTML, the universe or yfinance. Borrows exactly one
name from edgar: the SEC-polite HTTP getter.

Usage:  facts = fetch_ticker_facts("AAPL", "0000320193")
"""
from __future__ import annotations

import json
from typing import NamedTuple

from edgar import sec_get

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/"
               "CIK{cik}/us-gaap/{concept}.json")


def parse_ticker_map(raw: bytes) -> dict[str, str]:
    """ticker -> zero-padded 10-digit CIK, from company_tickers.json.

    Zero-padded because every data.sec.gov path wants CIK0000320193, not
    CIK320193, and the file stores the integer.
    """
    payload = json.loads(raw)
    return {row["ticker"]: f"{int(row['cik_str']):010d}"
            for row in payload.values() if row.get("ticker")}


# --------------------------------------------------------------------------- #
# Network                                                                      #
# --------------------------------------------------------------------------- #
def ticker_cik_map() -> dict[str, str]:
    """One request, no key. 10,412 entries as of 2026-09-05."""
    return parse_ticker_map(sec_get(TICKER_MAP_URL))
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/ -q`
Expected: 242 passed

- [ ] **Step 7: Confirm coverage against the real universe (network, one request)**

```bash
python - <<'EOF'
import build_dashboard as bd, xbrl
tk = {t for sec in bd.UNIVERSE.values() for sub in sec.values() for t in sub}
m = xbrl.ticker_cik_map()
missing = sorted(t for t in tk if t not in m)
print(f"{len(tk) - len(missing)}/{len(tk)} resolved; missing: {missing}")
assert missing == ["EA"], f"expected only EA to miss, got {missing}"
EOF
```

Expected: `152/153 resolved; missing: ['EA']`

- [ ] **Step 8: Commit**

```bash
git add edgar.py xbrl.py tests/test_xbrl.py tests/fixtures/company_tickers_sample.json
git commit -m "feat: resolve the universe to SEC CIKs

edgar._get becomes edgar.sec_get so xbrl.py can borrow the polite headers, gzip
handling and backoff rather than growing a second copy. 152 of 153 tickers
resolve; the miss is EA, which the store already shows as effectively dead at 6
closes in 60 sessions."
```

---

## Task 5: Parse XBRL facts — concept chains and duration disambiguation

**Files:**
- Modify: `xbrl.py`
- Modify: `tests/test_xbrl.py`
- Create: `tests/fixtures/xbrl_aapl_netincome.json`, `tests/fixtures/xbrl_aapl_revenues.json`, `tests/fixtures/xbrl_ora_revenues.json`

**Interfaces:**
- Consumes: `edgar.sec_get`, `xbrl.parse_ticker_map` from Task 4
- Produces: `xbrl.Fact` (NamedTuple with fields `ticker, concept, period_start, period_end, fy, fp, form, filed, value`), `xbrl.CONCEPTS: dict[str, tuple[str, ...]]`, `xbrl.classify_span(start, end) -> str`, `xbrl.parse_concept(ticker, tag, raw) -> list[Fact]`, `xbrl.pick_tag(ticker, chain, fetched) -> str | None`, `xbrl.quarterly(facts) -> list[Fact]`, `xbrl.fetch_concept(cik, tag) -> bytes` (network)

**Why:** spec §5, traps 1-3. A period end carries both a year-to-date and a
three-month fact (AAPL's 2026-03-28 holds `4.85` and `2.01`); a 10-K carries no
Q4 fact at all; and filers tag revenue three different ways, so mixing tags
mid-series renders as a fake revision.

- [ ] **Step 1: Capture the fixtures (network, one-time)**

```bash
python - <<'EOF'
from pathlib import Path
from edgar import sec_get
F = Path("tests/fixtures")
for name, cik, tag in [
    ("xbrl_aapl_netincome", "0000320193", "NetIncomeLoss"),
    ("xbrl_aapl_revenues", "0000320193",
     "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("xbrl_ora_revenues", "0001341439", "Revenues"),
]:
    url = ("https://data.sec.gov/api/xbrl/companyconcept/"
           f"CIK{cik}/us-gaap/{tag}.json")
    (F / f"{name}.json").write_bytes(sec_get(url))
    print(name, (F / f"{name}.json").stat().st_size, "bytes")
EOF
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_xbrl.py`:

```python
def _raw(name):
    return (FIXTURES / f"{name}.json").read_bytes()


def test_classify_span_separates_a_quarter_from_a_ytd_and_a_year():
    assert xbrl.classify_span("2026-03-29", "2026-06-27") == "quarter"
    assert xbrl.classify_span("2025-09-29", "2026-03-28") == "ytd"
    assert xbrl.classify_span("2025-09-29", "2026-09-27") == "annual"
    assert xbrl.classify_span("", "2026-06-27") == "instant"


def test_a_period_end_carrying_both_ytd_and_quarter_keeps_only_the_quarter():
    facts = xbrl.parse_concept("AAPL", "NetIncomeLoss", _raw("xbrl_aapl_netincome"))
    same_end = [f for f in facts if f.period_end == "2026-03-28"
                and f.period_start]
    spans = {xbrl.classify_span(f.period_start, f.period_end) for f in same_end}
    assert "quarter" in spans and "ytd" in spans, "fixture must contain both"

    q = xbrl.quarterly(facts)
    ends = [f.period_end for f in q]
    assert ends.count("2026-03-28") == 1
    kept = next(f for f in q if f.period_end == "2026-03-28")
    assert xbrl.classify_span(kept.period_start, kept.period_end) == "quarter"


def test_an_amendment_does_not_replace_the_original():
    facts = xbrl.parse_concept("AAPL", "NetIncomeLoss", _raw("xbrl_aapl_netincome"))
    by_key = {}
    for f in facts:
        by_key.setdefault((f.period_start, f.period_end), set()).add(f.filed)
    assert any(len(v) > 1 for v in by_key.values()), \
        "at least one period should carry an original and a later filing"


def test_pick_tag_chooses_the_chain_member_with_the_most_facts():
    fetched = {
        "RevenueFromContractWithCustomerExcludingAssessedTax":
            _raw("xbrl_aapl_revenues"),
        "Revenues": b'{"units":{"USD":[]}}',
    }
    tag = xbrl.pick_tag("AAPL", xbrl.CONCEPTS["revenue"], fetched)
    assert tag == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_pick_tag_falls_through_to_the_second_member():
    fetched = {
        "RevenueFromContractWithCustomerExcludingAssessedTax":
            b'{"units":{"USD":[]}}',
        "Revenues": _raw("xbrl_ora_revenues"),
    }
    assert xbrl.pick_tag("ORCL", xbrl.CONCEPTS["revenue"], fetched) == "Revenues"


def test_pick_tag_returns_none_when_nothing_in_the_chain_has_facts():
    fetched = {t: b'{"units":{"USD":[]}}' for t in xbrl.CONCEPTS["revenue"]}
    assert xbrl.pick_tag("ZZZZ", xbrl.CONCEPTS["revenue"], fetched) is None


def test_q4_is_reconstructed_from_the_annual_minus_the_first_three():
    # A 10-K carries no Q4 fact. Without reconstruction every Q4 is a hole.
    facts = [
        xbrl.Fact("T", "NetIncomeLoss", "2025-01-01", "2025-03-31",
                  2025, "Q1", "10-Q", "2025-04-30", 100.0),
        xbrl.Fact("T", "NetIncomeLoss", "2025-04-01", "2025-06-30",
                  2025, "Q2", "10-Q", "2025-07-30", 110.0),
        xbrl.Fact("T", "NetIncomeLoss", "2025-07-01", "2025-09-30",
                  2025, "Q3", "10-Q", "2025-10-30", 120.0),
        xbrl.Fact("T", "NetIncomeLoss", "2025-01-01", "2025-12-31",
                  2025, "FY", "10-K", "2026-02-15", 500.0),
    ]
    q = xbrl.quarterly(facts)
    q4 = [f for f in q if f.period_end == "2025-12-31"]
    assert len(q4) == 1
    assert q4[0].value == pytest.approx(170.0)      # 500 - (100+110+120)
    assert q4[0].fp == "Q4"
    # It became knowable when the 10-K was filed, not at period end.
    assert q4[0].filed == "2026-02-15"


def test_a_november_fiscal_year_end_still_classifies_cleanly():
    # AVGO's fiscal year ends in early November and its quarters are 4-4-5, so
    # a calendar-quarter assumption would misclassify every one of them.
    assert xbrl.classify_span("2026-02-02", "2026-05-03") == "quarter"
    assert xbrl.classify_span("2025-11-03", "2026-11-01") == "annual"
    assert xbrl.classify_span("2025-11-03", "2026-05-03") == "ytd"


def test_a_53_week_year_is_still_an_annual_span():
    # A 52/53-week filer runs 371 days in the long year.
    assert xbrl.classify_span("2025-09-29", "2026-10-05") == "annual"


def test_non_finite_values_are_dropped_at_the_parse_boundary():
    raw = (b'{"units":{"USD":[{"start":"2025-01-01","end":"2025-03-31",'
           b'"val":null,"fy":2025,"fp":"Q1","form":"10-Q","filed":"2025-04-30"}]}}')
    assert xbrl.parse_concept("T", "NetIncomeLoss", raw) == []
```

- [ ] **Step 3: Run it to make sure it fails**

Run: `python -m pytest tests/test_xbrl.py -v`
Expected: FAIL with `AttributeError: module 'xbrl' has no attribute 'classify_span'`

- [ ] **Step 4: Implement in `xbrl.py`**

```python
from datetime import date

# Concept chains. Multiples are built from aggregates rather than per-share
# figures, so that a split cannot corrupt them: a dollar total carries no share
# basis. epsDiluted is fetched only as an independent cross-check in the proof
# harness -- it is never a numerator's source.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "netIncome": ("NetIncomeLoss",),
    "revenue":   ("RevenueFromContractWithCustomerExcludingAssessedTax",
                  "Revenues", "SalesRevenueNet"),
    "opIncome":  ("OperatingIncomeLoss",),
    "dep":       ("DepreciationDepletionAndAmortization",
                  "DepreciationAmortizationAndAccretionNet"),
    "shares":    ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "cash":      ("CashAndCashEquivalentsAtCarryingValue",),
    "debtLT":    ("LongTermDebtNoncurrent",),
    "debtST":    ("LongTermDebtCurrent",),
    "cfo":       ("NetCashProvidedByUsedInOperatingActivities",),
    "capex":     ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "epsDiluted": ("EarningsPerShareDiluted",),
}

# Balance-sheet concepts are reported as an instant -- a level on a date -- not
# as a duration. They must never go through quarterly(), which keeps only
# three-month spans and would drop every one of them, nor be summed over four
# quarters, which would count the same cash four times.
INSTANT_CONCEPTS = frozenset({"cash", "debtLT", "debtST"})


class Fact(NamedTuple):
    ticker: str
    concept: str        # the us-gaap tag as filed
    period_start: str   # '' for an instant fact (balance sheet items)
    period_end: str
    fy: int | None
    fp: str | None
    form: str
    filed: str
    value: float


# Fiscal periods are not exactly 91/365 days: filers use 4-4-5 calendars and
# 52/53-week years. These windows are wide enough for both and narrow enough
# that a 6-month year-to-date figure can never be mistaken for a quarter.
_SPANS = (("quarter", 80, 100), ("ytd", 165, 290), ("annual", 350, 380))


def classify_span(start: str, end: str) -> str:
    """'instant' | 'quarter' | 'ytd' | 'annual' | 'other'."""
    if not start:
        return "instant"
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    for name, lo, hi in _SPANS:
        if lo <= days <= hi:
            return name
    return "other"


def parse_concept(ticker: str, tag: str, raw: bytes) -> list[Fact]:
    """Flatten one companyconcept document into Facts.

    Every unit is taken, not only the first: a concept can be reported in more
    than one unit and picking one arbitrarily silently loses the rest. Values
    that are not finite write no Fact, matching the store's write boundary.
    """
    payload = json.loads(raw)
    out: list[Fact] = []
    for entries in payload.get("units", {}).values():
        for e in entries:
            value = e.get("val")
            if not isinstance(value, (int, float)) or value != value:
                continue
            if not e.get("end") or not e.get("filed"):
                continue
            out.append(Fact(ticker, tag, e.get("start") or "", e["end"],
                            e.get("fy"), e.get("fp"), e.get("form", ""),
                            e["filed"], float(value)))
    return out


def pick_tag(ticker: str, chain: tuple[str, ...],
             fetched: dict[str, bytes]) -> str | None:
    """The chain member carrying the most facts, or None.

    Held fixed for a ticker's whole series. Mixing tags mid-series renders as a
    revision that never happened: AAPL tags revenue as
    RevenueFromContractWithCustomerExcludingAssessedTax throughout and carries
    only 11 facts under `Revenues`, so a fallback taken per-period would jump
    between two different definitions of the same line.
    """
    best, best_n = None, 0
    for tag in chain:
        raw = fetched.get(tag)
        if not raw:
            continue
        n = len(parse_concept(ticker, tag, raw))
        if n > best_n:
            best, best_n = tag, n
    return best


def _sum_first_three(year_facts: list[Fact]) -> tuple[float, str] | None:
    quarters = {f.fp: f for f in year_facts if f.fp in ("Q1", "Q2", "Q3")}
    if len(quarters) != 3:
        return None
    total = sum(f.value for f in quarters.values())
    return total, max(f.period_end for f in quarters.values())


def quarterly(facts: list[Fact]) -> list[Fact]:
    """Three-month facts only, with Q4 reconstructed from the annual figure.

    A 10-K reports the year, never its fourth quarter, so without this every Q4
    is a hole and a trailing-twelve-month sum silently spans five quarters.
    The synthesized Q4 is filed on the 10-K's date, because that is when it
    became knowable -- which is the whole point of a point-in-time series.
    """
    kept = [f for f in facts
            if classify_span(f.period_start, f.period_end) == "quarter"]
    seen = {(f.period_start, f.period_end, f.filed) for f in kept}

    by_year: dict[int, list[Fact]] = {}
    for f in kept:
        if f.fy is not None:
            by_year.setdefault(f.fy, []).append(f)

    for f in facts:
        if classify_span(f.period_start, f.period_end) != "annual":
            continue
        if f.fy is None:
            continue
        parts = _sum_first_three(by_year.get(f.fy, []))
        if parts is None:
            continue
        total, q3_end = parts
        key = (q3_end, f.period_end, f.filed)
        if key in seen:
            continue
        seen.add(key)
        kept.append(Fact(f.ticker, f.concept, q3_end, f.period_end,
                         f.fy, "Q4", f.form, f.filed, f.value - total))
    return kept


# --------------------------------------------------------------------------- #
# Network                                                                      #
# --------------------------------------------------------------------------- #
def fetch_concept(cik: str, tag: str) -> bytes:
    """One companyconcept document. Measured 1-4 KB gzipped."""
    return sec_get(CONCEPT_URL.format(cik=cik, concept=tag))
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_xbrl.py -v`
Expected: PASS, 9 tests in this file

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: 249 passed

- [ ] **Step 7: Commit**

```bash
git add xbrl.py tests/test_xbrl.py tests/fixtures/xbrl_*.json
git commit -m "feat: parse SEC XBRL facts with their filing dates

Three traps, each of which silently produces a wrong series rather than an
error. A period end carries both a year-to-date and a three-month fact -- AAPL's
2026-03-28 holds 4.85 and 2.01 -- so TTM double-counts unless they are separated
by duration. A 10-K carries no Q4 fact, only the annual figure, so Q4 is
reconstructed as FY minus the first three and filed on the 10-K's date, which is
when it became knowable. And filers tag revenue three ways: the chain member
with the most facts is chosen once per ticker and held fixed, because switching
mid-series renders as a revision that never happened."
```

---

## Task 6: The `reported` table

**Files:**
- Modify: `history.py` (`SCHEMA`, upsert, readers, `rebuild`)
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `xbrl.Fact` from Task 5
- Produces: `history.upsert_reported(conn, facts) -> int`, `history.reported(conn, ticker=None, concept=None) -> pd.DataFrame`, `history.last_filed(conn) -> dict[str, str]`, `history.write_reported_csv(conn, path) -> int`, `history.read_reported_csv(path) -> list[Fact]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_history.py`:

```python
from xbrl import Fact


def _fact(**kw):
    base = dict(ticker="AAPL", concept="NetIncomeLoss",
                period_start="2026-03-29", period_end="2026-06-27",
                fy=2026, fp="Q3", form="10-Q", filed="2026-07-31",
                value=29789000000.0)
    base.update(kw)
    return Fact(**base)


def test_an_amendment_coexists_with_the_original(conn):
    history.upsert_reported(conn, [
        _fact(form="10-K", filed="2026-10-30", value=100.0),
        _fact(form="10-K/A", filed="2027-01-25", value=105.0),
    ])
    rows = history.reported(conn, ticker="AAPL")
    assert len(rows) == 2, "filed is part of the key; neither may overwrite"
    assert set(rows.value) == {100.0, 105.0}


def test_a_ytd_and_a_quarter_on_the_same_end_are_distinct_rows(conn):
    history.upsert_reported(conn, [
        _fact(period_start="2026-03-29", value=2.01),
        _fact(period_start="2025-09-29", value=4.85),
    ])
    assert len(history.reported(conn, ticker="AAPL")) == 2


def test_reingesting_the_same_fact_does_not_duplicate_it(conn):
    history.upsert_reported(conn, [_fact()])
    history.upsert_reported(conn, [_fact()])
    assert len(history.reported(conn, ticker="AAPL")) == 1


def test_non_finite_values_write_no_row(conn):
    assert history.upsert_reported(conn, [_fact(value=float("nan"))]) == 0
    assert len(history.reported(conn)) == 0


def test_last_filed_reports_the_newest_filing_per_ticker(conn):
    history.upsert_reported(conn, [
        _fact(ticker="AAPL", filed="2026-07-31"),
        _fact(ticker="AAPL", filed="2026-04-30", period_end="2026-03-28"),
        _fact(ticker="MSFT", filed="2026-05-02"),
    ])
    assert history.last_filed(conn) == {"AAPL": "2026-07-31",
                                        "MSFT": "2026-05-02"}


def test_the_csv_round_trips_exactly(conn, tmp_path):
    facts = [_fact(value=1.9626000000000001), _fact(period_end="2026-03-28",
                                                    value=3.0)]
    history.upsert_reported(conn, facts)
    path = tmp_path / "reported.csv.gz"
    assert history.write_reported_csv(conn, path) == 2
    back = history.read_reported_csv(path)
    assert sorted(back) == sorted(facts), \
        "round_trip float precision is what the raw archive exists to guarantee"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_history.py -k reported -v`
Expected: FAIL with `AttributeError: module 'history' has no attribute 'upsert_reported'`

- [ ] **Step 3: Add the table to `SCHEMA` in `history.py`**

```sql
CREATE TABLE IF NOT EXISTS reported (
  ticker       TEXT NOT NULL,
  concept      TEXT NOT NULL,   -- the us-gaap tag as filed
  period_start TEXT NOT NULL,   -- '' for instant facts
  period_end   TEXT NOT NULL,
  fy           INTEGER,
  fp           TEXT,
  form         TEXT NOT NULL,
  filed        TEXT NOT NULL,
  value        REAL NOT NULL,
  ingested_at  TEXT NOT NULL,
  -- `filed` is in the key so an original and its 10-K/A coexist rather than one
  -- overwriting the other: a point-in-time series needs the number the market
  -- actually had on a date, not the number it was later corrected to.
  -- `period_start` is in the key because one period_end carries both a
  -- year-to-date and a three-month fact, distinguishable only by duration.
  PRIMARY KEY (ticker, concept, period_end, period_start, filed)
);

CREATE INDEX IF NOT EXISTS reported_ticker_filed ON reported (ticker, filed);
```

- [ ] **Step 4: Add the writers and readers**

```python
_UPSERT_REPORTED = """
INSERT INTO reported (ticker, concept, period_start, period_end, fy, fp,
                      form, filed, value, ingested_at)
VALUES (?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(ticker, concept, period_end, period_start, filed)
DO UPDATE SET value = excluded.value, ingested_at = excluded.ingested_at
"""

REPORTED_COLUMNS = ("ticker", "concept", "period_start", "period_end",
                    "fy", "fp", "form", "filed", "value")


def upsert_reported(conn: sqlite3.Connection, facts) -> int:
    """Write facts, dropping non-finite values at the boundary."""
    stamp = _now()
    payload = [(f.ticker, f.concept, f.period_start, f.period_end, f.fy, f.fp,
                f.form, f.filed, float(f.value), stamp)
               for f in facts if is_finite(f.value)]
    if not payload:
        return 0
    conn.executemany(_UPSERT_REPORTED, payload)
    conn.commit()
    return len(payload)


def reported(conn: sqlite3.Connection, ticker: str | None = None,
             concept: str | None = None) -> pd.DataFrame:
    sql = f"SELECT {', '.join(REPORTED_COLUMNS)} FROM reported"
    where, args = [], []
    if ticker:
        where.append("ticker = ?")
        args.append(ticker)
    if concept:
        where.append("concept = ?")
        args.append(concept)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return pd.read_sql_query(sql + " ORDER BY ticker, period_end, filed",
                             conn, params=args)


def last_filed(conn: sqlite3.Connection) -> dict[str, str]:
    """Newest filing date seen per ticker -- the sweep's freshness marker.

    Read from the store rather than from a file mtime, so it survives whatever
    retention policy data/ is on.
    """
    return {t: d for t, d in conn.execute(
        "SELECT ticker, MAX(filed) FROM reported GROUP BY ticker")}


def write_reported_csv(conn: sqlite3.Connection, path) -> int:
    """The rebuild path for `reported`, as one undated archive.

    Undated so extract.dated_stamp cannot match it and the pruner cannot reach
    it -- it sits with cusip_map.csv and ff_factors.csv.gz as a reference file.
    """
    frame = reported(conn)
    frame.to_csv(path, index=False, compression="gzip")
    return len(frame)


def read_reported_csv(path) -> list:
    from xbrl import Fact
    frame = pd.read_csv(path, float_precision="round_trip",
                        keep_default_na=False, na_values=[""])
    out = []
    for r in frame.to_dict("records"):
        out.append(Fact(
            ticker=r["ticker"], concept=r["concept"],
            period_start=str(r["period_start"] or ""),
            period_end=str(r["period_end"]),
            fy=None if pd.isna(r["fy"]) else int(r["fy"]),
            fp=None if r["fp"] in ("", None) or pd.isna(r["fp"]) else r["fp"],
            form=r["form"], filed=r["filed"], value=float(r["value"])))
    return out
```

- [ ] **Step 5: Teach `rebuild` about it**

In `history.rebuild`, after the existing kinds, add:

```python
    archive = Path(data_dir) / "reported.csv.gz"
    counts["reported"] = (
        upsert_reported(conn, read_reported_csv(archive))
        if archive.exists() else 0
    )
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/ -q`
Expected: 255 passed

- [ ] **Step 7: Commit**

```bash
git add history.py tests/test_history.py
git commit -m "feat: store point-in-time reported facts

filed is part of the primary key so an original and its 10-K/A coexist as
separate rows. A point-in-time series needs the number the market actually had
on a date, not the number it was later corrected to, and keeping both also makes
restatements queryable rather than invisible. period_start is in the key because
one period_end carries both a year-to-date and a three-month fact.

The rebuild path is one undated data/reported.csv.gz rather than a dated file,
so the pruner introduced in faeab61 cannot reach it."
```

---

## Task 7: The backfill tool

**Files:**
- Create: `tools/backfill_xbrl.py`
- Create: `tests/test_backfill_xbrl.py`

**Interfaces:**
- Consumes: `xbrl.CONCEPTS`, `xbrl.fetch_concept`, `xbrl.pick_tag`, `xbrl.parse_concept`, `xbrl.quarterly`, `xbrl.ticker_cik_map`, `history.upsert_reported`, `history.last_filed`, `history.write_reported_csv`
- Produces: `backfill_xbrl.due(tickers, last, today, stale_days=85) -> list[str]`, `backfill_xbrl.sweep_ticker(ticker, cik, fetch) -> list[Fact]`, `backfill_xbrl.main(argv) -> int`

**Why:** spec §10. The sweep runs from `tools/`, never inside
`build_dashboard.py`, so a SEC outage cannot cost the dashboard.

- [ ] **Step 1: Write the failing test**

Create `tests/test_backfill_xbrl.py`:

```python
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "backfill_xbrl", Path(__file__).parent.parent / "tools" / "backfill_xbrl.py")
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_ticker_never_swept_is_always_due():
    assert backfill.due(["AAPL"], {}, "2026-09-05") == ["AAPL"]


def test_a_ticker_filed_inside_the_window_is_not_due():
    assert backfill.due(["AAPL"], {"AAPL": "2026-07-31"}, "2026-09-05") == []


def test_a_ticker_whose_last_filing_has_gone_stale_is_due():
    # 2026-04-30 is 128 days before 2026-09-05, past the ~85-day window.
    assert backfill.due(["AAPL"], {"AAPL": "2026-04-30"}, "2026-09-05") == ["AAPL"]


def test_sweep_ticker_holds_one_tag_per_concept(monkeypatch):
    calls = []

    def fake_fetch(cik, tag):
        calls.append(tag)
        if tag == "RevenueFromContractWithCustomerExcludingAssessedTax":
            return (FIXTURES / "xbrl_aapl_revenues.json").read_bytes()
        if tag == "NetIncomeLoss":
            return (FIXTURES / "xbrl_aapl_netincome.json").read_bytes()
        return b'{"units":{"USD":[]}}'

    facts = backfill.sweep_ticker("AAPL", "0000320193", fake_fetch)
    tags = {f.concept for f in facts}
    assert "RevenueFromContractWithCustomerExcludingAssessedTax" in tags
    assert "Revenues" not in tags, "the chain must resolve to one tag, not mix"
    assert all(f.ticker == "AAPL" for f in facts)


def test_a_ticker_that_fails_does_not_abort_the_sweep(monkeypatch):
    def fake_fetch(cik, tag):
        raise RuntimeError("503 from SEC")

    facts = backfill.sweep_ticker("AAPL", "0000320193", fake_fetch)
    assert facts == [], "a failure yields nothing, it does not raise"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_backfill_xbrl.py -v`
Expected: FAIL — the file does not exist

- [ ] **Step 3: Write `tools/backfill_xbrl.py`**

```python
"""Sweep SEC XBRL into the reported table.

Runs outside build_dashboard.py on purpose: a SEC outage, a changed tag or a
bad parse must not cost you the dashboard, the same reasoning that keeps
portfolio.py off the daily path and record_history() non-fatal.

Steady state makes almost no requests. A ticker is re-checked only once its
newest known filing has gone stale, because a 10-Q lands roughly every 90 days
and nothing changes in between.

Usage:
    python tools/backfill_xbrl.py            # only what is due
    python tools/backfill_xbrl.py --all      # every ticker, the first sweep
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history                      # noqa: E402
import xbrl                         # noqa: E402
from build_dashboard import UNIVERSE  # noqa: E402

DATA_DIR = ROOT / "data"

# A 10-Q lands roughly every 90 days. 85 gives a few days of slack before the
# next one is expected without re-checking a name that just filed.
STALE_DAYS = 85

# SEC allows 10 requests a second. This is the per-request floor, not a target.
FETCH_SLEEP = 0.11


def universe_tickers() -> list[str]:
    return sorted({t for sec in UNIVERSE.values()
                   for sub in sec.values() for t in sub})


def due(tickers, last: dict[str, str], today: str,
        stale_days: int = STALE_DAYS) -> list[str]:
    """Tickers worth re-checking: never swept, or gone stale."""
    now = date.fromisoformat(today)
    out = []
    for t in tickers:
        seen = last.get(t)
        if not seen or (now - date.fromisoformat(seen)).days > stale_days:
            out.append(t)
    return out


def sweep_ticker(ticker: str, cik: str, fetch) -> list:
    """Every concept for one ticker, chain resolved once and held fixed.

    Returns [] rather than raising: one bad ticker must not abort a sweep of
    152 others.
    """
    facts: list = []
    try:
        for name, chain in xbrl.CONCEPTS.items():
            fetched = {}
            for tag in chain:
                try:
                    fetched[tag] = fetch(cik, tag)
                except Exception:       # noqa: BLE001 - a missing tag is normal
                    continue
                time.sleep(FETCH_SLEEP)
            tag = xbrl.pick_tag(ticker, chain, fetched)
            if tag is None:
                continue
            parsed = xbrl.parse_concept(ticker, tag, fetched[tag])
            # An instant fact has no duration, so quarterly() would discard it.
            facts.extend(parsed if name in xbrl.INSTANT_CONCEPTS
                         else xbrl.quarterly(parsed))
    except Exception as exc:            # noqa: BLE001 - reported, never raised
        print(f"  {ticker}: {exc}", file=sys.stderr)
        return []
    return facts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="sweep every ticker, not only those due")
    args = ap.parse_args(argv)

    conn = history.connect()
    history.ensure_schema(conn)
    tickers = universe_tickers()
    targets = (tickers if args.all
               else due(tickers, history.last_filed(conn),
                        date.today().isoformat()))
    if not targets:
        print("Nothing due.")
        return 0

    cik_map = xbrl.ticker_cik_map()
    missing = [t for t in targets if t not in cik_map]
    if missing:
        print(f"No CIK for {', '.join(missing)} — skipped")

    total = 0
    for i, ticker in enumerate(t for t in targets if t in cik_map):
        facts = sweep_ticker(ticker, cik_map[ticker], xbrl.fetch_concept)
        total += history.upsert_reported(conn, facts)
        print(f"  [{i+1}] {ticker}: {len(facts)} facts")

    written = history.write_reported_csv(conn, DATA_DIR / "reported.csv.gz")
    conn.close()
    print(f"Stored {total} facts; archive holds {written} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_backfill_xbrl.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Run the first real sweep (network, ~10 minutes)**

```bash
python tools/backfill_xbrl.py --all 2>&1 | tail -20
python - <<'EOF'
import history
c = history.connect()
n, t = c.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM reported").fetchone()
lo, hi = c.execute("SELECT MIN(period_end), MAX(period_end) FROM reported").fetchone()
print(f"{n:,} facts over {t} tickers, {lo} to {hi}")
assert t >= 145, f"expected ~152 tickers, got {t}"
EOF
```

- [ ] **Step 6: Commit**

```bash
git add tools/backfill_xbrl.py tests/test_backfill_xbrl.py data/reported.csv.gz
git commit -m "feat: sweep SEC XBRL into the store

Runs from tools/ and never inside build_dashboard.py, so a SEC outage cannot
cost the dashboard -- the same reasoning that keeps portfolio.py off the daily
path. A ticker is re-checked only once its newest known filing is more than 85
days old, so steady state makes almost no requests, and one ticker failing
returns nothing rather than aborting a sweep of 151 others.

Measured at 3 MB and about 1,368 requests for the universe, against 580 MB had
this used companyfacts."
```

---

## Task 8: The proof harness

**Files:**
- Create: `tools/prove_xbrl.py`
- Create: `tests/test_prove_xbrl.py`

**Interfaces:**
- Consumes: `history.reported`, `valuation` is not yet written, so this task proves the *aggregates* directly
- Produces: `prove_xbrl.proof(recon: pd.Series, reference: pd.Series) -> tuple[float, bool]` returning `(median_rel_error, passed)`, `prove_xbrl.PASS_TOLERANCE = 0.01`. `summarize` and `report` arrive in Task 11, once `valuation` can produce a reconstruction to compare against.

**Why:** spec §6. The normalization risk in Task 5 is managed the way
`data/cusip_map.csv` is: the mapping is not trusted, it is proved. Ten managers
agreeing NVDA was $200.09 is strong evidence; the same standard applies here.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prove_xbrl.py`:

```python
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location(
    "prove_xbrl", Path(__file__).parent.parent / "tools" / "prove_xbrl.py")
prove = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prove)


def test_an_exact_reconstruction_passes():
    s = pd.Series([10.0, 11.0, 12.0], index=["a", "b", "c"])
    err, ok = prove.proof(s, s)
    assert err == pytest.approx(0.0)
    assert ok


def test_a_reconstruction_inside_tolerance_passes():
    ref = pd.Series([100.0, 100.0, 100.0])
    err, ok = prove.proof(pd.Series([100.5, 99.6, 100.4]), ref)
    assert err < prove.PASS_TOLERANCE
    assert ok


def test_one_bad_day_does_not_reject_a_good_mapping():
    # The median, not the mean: a single source glitch must not fail a ticker.
    ref = pd.Series([100.0] * 9 + [100.0])
    recon = pd.Series([100.0] * 9 + [4000.0])
    err, ok = prove.proof(recon, ref)
    assert ok, "nine exact days and one outlier should still pass"


def test_a_wrong_tag_is_rejected():
    ref = pd.Series([100.0, 100.0, 100.0])
    err, ok = prove.proof(pd.Series([250.0, 249.0, 251.0]), ref)
    assert err > prove.PASS_TOLERANCE
    assert not ok


def test_too_few_overlapping_days_cannot_pass():
    err, ok = prove.proof(pd.Series([100.0]), pd.Series([100.0]))
    assert not ok, "one day is not evidence"


def test_no_overlap_at_all_is_not_a_pass():
    err, ok = prove.proof(pd.Series(dtype=float), pd.Series(dtype=float))
    assert not ok
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_prove_xbrl.py -v`
Expected: FAIL — the file does not exist

- [ ] **Step 3: Write `tools/prove_xbrl.py`**

```python
"""Prove the XBRL extraction against the source it is replacing.

The same standard as data/cusip_map.csv, which is not trusted but proved: a
candidate is accepted only when an independent observation agrees. Here the
independent observation is Yahoo's own published multiple over the days where
the snapshot table and the reconstruction overlap.

A (ticker, metric) pair that fails gets no own-history frame. That is the store's
"a missing value writes no row" extended to derived data: a wrong multiple is
worse than an absent one, because it looks like knowledge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history  # noqa: E402

# 1% rather than exact, because Yahoo rounds and may define EBITDA slightly
# differently. The median rather than the mean, so one bad day cannot reject a
# good mapping. The CUSIP map's equivalent check reached a median price error of
# 0.0000, so a wide spread here means something is wrong, not that this is tight.
PASS_TOLERANCE = 0.01

# Two days is the floor for a median to mean anything at all.
MIN_OVERLAP = 2


def proof(recon: pd.Series, reference: pd.Series) -> tuple[float, bool]:
    """(median relative error, passed) over the overlapping index."""
    pair = pd.concat([recon.rename("r"), reference.rename("y")],
                     axis=1, join="inner").dropna()
    pair = pair[pair.y != 0]
    if len(pair) < MIN_OVERLAP:
        return float("nan"), False
    err = float(((pair.r - pair.y) / pair.y).abs().median())
    return err, err < PASS_TOLERANCE
```

`report(conn)` is added in Task 11, once `valuation` can produce the
reconstruction to compare. This task delivers and tests the decision rule.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/ -q`
Expected: 261 passed

- [ ] **Step 5: Commit**

```bash
git add tools/prove_xbrl.py tests/test_prove_xbrl.py
git commit -m "feat: the decision rule for accepting an XBRL reconstruction

Median relative error under 1% against Yahoo's own published multiple over the
overlapping days. Median rather than mean so one source glitch cannot reject a
good mapping; 1% rather than exact because Yahoo rounds. A pair that fails gets
no own-history frame rather than a wrong one -- a wrong multiple is worse than
an absent one because it looks like knowledge."
```

---

## Task 9: Capture the dividend-unadjusted close

**Files:**
- Modify: `build_dashboard.py` (`fetch_closes`, `record_history` call path)
- Modify: `history.py` (`price_rows` gains the metric)
- Modify: `tests/test_history.py`
- Create: `tools/backfill_close_raw.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: a `metrics` row with `period_type='daily', metric='closeRaw'`; `build_dashboard.fetch_closes(symbols, period, adjusted=True) -> pd.DataFrame`

**Why:** spec §5. `fetch_closes` runs `auto_adjust=True`, so the stored `close`
is dividend-adjusted. A market cap built from it understates the price then, by
an amount that compounds with yield — measured at 2021-09-01 against `Close`:
**VZ 37.2%, IBM 21.1%, KO 15.9%, PG 13.7%, MSFT 4.2%**. Every historical
multiple would read that much too cheap, worst on exactly the income names where
a P/E history is most often consulted. The existing `close` stays untouched and
keeps serving returns and sparklines, where dividend adjustment is correct.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_history.py`:

```python
def test_price_rows_labels_the_unadjusted_series_separately(conn):
    closes = pd.DataFrame(
        {"AAPL": [100.0, 101.0]},
        index=pd.to_datetime(["2026-09-03", "2026-09-04"]))
    history.ingest_prices(conn, closes)
    history.ingest_prices(conn, closes * 1.1, metric="closeRaw")
    got = dict(conn.execute(
        "SELECT metric, COUNT(*) FROM metrics WHERE period_type='daily' "
        "GROUP BY metric"))
    assert got == {"close": 2, "closeRaw": 2}


def test_the_two_price_series_do_not_overwrite_each_other(conn):
    closes = pd.DataFrame({"AAPL": [100.0]},
                          index=pd.to_datetime(["2026-09-04"]))
    history.ingest_prices(conn, closes)
    history.ingest_prices(conn, closes * 1.2, metric="closeRaw")
    vals = dict(conn.execute(
        "SELECT metric, value FROM metrics WHERE period_type='daily'"))
    assert vals["close"] == pytest.approx(100.0)
    assert vals["closeRaw"] == pytest.approx(120.0)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_history.py -k unadjusted -v`
Expected: FAIL with `TypeError: ingest_prices() got an unexpected keyword argument 'metric'`

- [ ] **Step 3: Give `price_rows` and `ingest_prices` the metric name**

```python
def price_rows(closes, metric: str = "close") -> list[MetricRow]:
    """Melt a wide close frame into daily observations.

    `metric` distinguishes the two price bases the store now keeps. `close` is
    split- and dividend-adjusted and serves returns and sparklines. `closeRaw`
    is split-adjusted only, and is the one a historical market cap must use --
    a dividend-adjusted price understates what the market actually paid, by
    37% on VZ over five years.
    """
    # body unchanged except MetricRow(..., metric, ...) takes the argument


def ingest_prices(conn: sqlite3.Connection, closes, metric: str = "close") -> int:
    return upsert_rows(conn, price_rows(closes, metric))
```

- [ ] **Step 4: Give `fetch_closes` the choice, and fetch both in the daily run**

In `build_dashboard.py`:

```python
def fetch_closes(symbols: list[str], period: str = HIST_PERIOD,
                 adjusted: bool = True) -> pd.DataFrame:
    ...
    data = yf.download(..., auto_adjust=adjusted, ...)
```

and where the run records prices:

```python
    history.ingest_prices(conn, closes)
    # The unadjusted basis, for valuation only. yfinance's Close is still
    # split-adjusted under auto_adjust=False -- only the dividend adjustment
    # comes off, which is exactly the one a market cap must not carry.
    history.ingest_prices(conn, fetch_closes(symbols, adjusted=False),
                          metric="closeRaw")
```

- [ ] **Step 5: Write the one-time backfill**

`tools/backfill_close_raw.py`, mirroring `tools/backfill_xbrl.py`'s shape:

```python
"""Backfill five years of dividend-unadjusted closes.

One yf.download over the universe, same plumbing as the daily run. Only needed
once; the daily run keeps it current from here.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history                                        # noqa: E402
from build_dashboard import HIST_PERIOD, UNIVERSE, fetch_closes  # noqa: E402


def main() -> int:
    tickers = sorted({t for sec in UNIVERSE.values()
                      for sub in sec.values() for t in sub})
    closes = fetch_closes(tickers, period=HIST_PERIOD, adjusted=False)
    conn = history.connect()
    history.ensure_schema(conn)
    n = history.ingest_prices(conn, closes, metric="closeRaw")
    conn.close()
    print(f"Stored {n:,} closeRaw rows over {closes.shape[1]} tickers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the tests, then the backfill**

```bash
python -m pytest tests/ -q          # expect 263 passed
python tools/backfill_close_raw.py
python - <<'EOF'
import history, pandas as pd
c = history.connect()
d = pd.read_sql_query(
    "SELECT metric, COUNT(*) n, MIN(as_of) lo, MAX(as_of) hi FROM metrics "
    "WHERE period_type='daily' GROUP BY metric", c)
print(d.to_string(index=False))
EOF
```

- [ ] **Step 7: Commit**

```bash
git add build_dashboard.py history.py tools/backfill_close_raw.py tests/test_history.py
git commit -m "feat: keep a dividend-unadjusted close for valuation

fetch_closes runs auto_adjust=True, so the stored close is dividend-adjusted and
a market cap built from it understates what the market actually paid. Measured
at 2021-09-01, Close against Adj Close: VZ 37.2%, IBM 21.1%, KO 15.9%, PG 13.7%,
MSFT 4.2%. Every historical multiple would read that much too cheap, worst on
the income names where a P/E history is most often consulted.

close is untouched and still serves returns and sparklines, where dividend
adjustment is the correct choice. closeRaw is split-adjusted only."
```

---

## Task 10: Point-in-time TTM aggregates and split-adjusted shares

**Files:**
- Create: `valuation.py`
- Create: `tests/test_valuation.py`

**Interfaces:**
- Consumes: `history.reported`, `xbrl.Fact`
- Produces: `valuation.ttm_at(facts, on) -> float | None`, `valuation.ttm_series(facts, dates) -> pd.Series`, `valuation.split_factors(share_facts) -> pd.Series`, `valuation.shares_series(share_facts, dates) -> pd.Series`

- [ ] **Step 1: Write the failing test**

Create `tests/test_valuation.py`:

```python
import pandas as pd
import pytest

import valuation
from xbrl import Fact


def _q(end, filed, value, start=None, fy=2026, fp="Q1"):
    return Fact("T", "NetIncomeLoss", start or "", end, fy, fp,
                "10-Q", filed, value)


def _four_quarters():
    return [
        _q("2025-09-30", "2025-10-30", 100.0, "2025-07-01", 2025, "Q3"),
        _q("2025-12-31", "2026-02-15", 110.0, "2025-10-01", 2025, "Q4"),
        _q("2026-03-31", "2026-04-30", 120.0, "2026-01-01", 2026, "Q1"),
        _q("2026-06-30", "2026-07-31", 130.0, "2026-04-01", 2026, "Q2"),
    ]


def test_ttm_sums_the_four_most_recent_filed_quarters():
    assert valuation.ttm_at(_four_quarters(), "2026-08-01") == pytest.approx(460.0)


def test_ttm_uses_only_what_had_been_filed_on_the_date():
    # On 2026-07-15 the June quarter had not been filed yet.
    facts = _four_quarters() + [
        _q("2025-06-30", "2025-07-30", 90.0, "2025-04-01", 2025, "Q2")]
    assert valuation.ttm_at(facts, "2026-07-15") == pytest.approx(420.0)


def test_a_date_before_any_filing_has_no_ttm():
    assert valuation.ttm_at(_four_quarters(), "2020-01-01") is None


def test_fewer_than_four_filed_quarters_has_no_ttm():
    assert valuation.ttm_at(_four_quarters()[:3], "2026-08-01") is None


def test_an_amendment_supersedes_the_original_only_after_it_is_filed():
    facts = _four_quarters() + [
        Fact("T", "NetIncomeLoss", "2026-04-01", "2026-06-30", 2026, "Q2",
             "10-Q/A", "2026-09-20", 900.0)]
    assert valuation.ttm_at(facts, "2026-09-01") == pytest.approx(460.0)
    assert valuation.ttm_at(facts, "2026-10-01") == pytest.approx(1230.0)


def test_ttm_series_is_a_step_function_that_moves_on_filing_dates():
    dates = pd.to_datetime(["2026-07-30", "2026-07-31", "2026-08-01"])
    s = valuation.ttm_series(_four_quarters(), dates)
    assert s.iloc[0] != s.iloc[1], "it must step on the filing date"
    assert s.iloc[1] == s.iloc[2]


def test_split_factors_recover_a_ten_for_one_from_the_share_count():
    shares = [
        _q("2024-03-31", "2024-04-30", 2.47e9, "2024-01-01", 2024, "Q1"),
        _q("2024-06-30", "2024-07-30", 24.6e9, "2024-04-01", 2024, "Q2"),
        _q("2024-09-30", "2024-10-30", 24.7e9, "2024-07-01", 2024, "Q3"),
    ]
    f = valuation.split_factors(shares)
    # Pre-split filings must be multiplied by 10 to reach today's basis.
    assert f.loc["2024-03-31"] == pytest.approx(10.0)
    assert f.loc["2024-06-30"] == pytest.approx(1.0)


def test_split_factors_ignore_ordinary_buybacks():
    shares = [
        _q("2025-03-31", "2025-04-30", 15.0e9, "2025-01-01", 2025, "Q1"),
        _q("2025-06-30", "2025-07-30", 14.8e9, "2025-04-01", 2025, "Q2"),
        _q("2025-09-30", "2025-10-30", 14.7e9, "2025-07-01", 2025, "Q3"),
    ]
    assert set(valuation.split_factors(shares).round(6)) == {1.0}
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_valuation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'valuation'`

- [ ] **Step 3: Write `valuation.py`**

```python
"""Point-in-time fundamentals plus prices, as daily multiple series.

Every figure is an aggregate -- a dollar total -- rather than a per-share
number, so that a split cannot corrupt it. The share basis enters exactly once,
in the market cap, and is reconciled there.

Knows nothing about fetching, HTML, or SQL writes. Frames go in, frames come
out, so every trap below has a test that does not touch a database.
"""
from __future__ import annotations

import pandas as pd

import history

TTM_QUARTERS = 4

# A split shows up as a near-integer jump in the diluted share count between
# consecutive filings. 1.5 is comfortably above any plausible issuance and
# below the smallest split anyone runs (2:1).
SPLIT_MIN_RATIO = 1.5


def _known_at(facts, on: str) -> dict:
    """Newest value per period among the facts filed on or before `on`.

    An amendment supersedes its original only once it has been filed. Before
    that date the market had the original number, and a point-in-time series
    must say so.
    """
    best: dict = {}
    for f in facts:
        if f.filed > on:
            continue
        key = (f.period_start, f.period_end)
        if key not in best or f.filed > best[key].filed:
            best[key] = f
    return best


def ttm_at(facts, on: str) -> float | None:
    """Trailing-twelve-month total as it was knowable on `on`."""
    known = _known_at(facts, on)
    if len(known) < TTM_QUARTERS:
        return None
    newest = sorted(known.values(), key=lambda f: f.period_end,
                    reverse=True)[:TTM_QUARTERS]
    return float(sum(f.value for f in newest))


def ttm_series(facts, dates) -> pd.Series:
    """A daily step function, moving only on filing dates.

    Computed once per distinct filing date rather than once per day: the value
    can only change when something is filed, and 1,270 dates against ~70
    filings is 18x more work for the same answer.
    """
    index = pd.DatetimeIndex(dates)
    if not len(facts):
        return pd.Series(index=index, dtype=float)
    steps = sorted({f.filed for f in facts})
    values = {s: ttm_at(facts, s) for s in steps}
    stepped = pd.Series(values, index=pd.DatetimeIndex(steps)).sort_index()
    return stepped.reindex(stepped.index.union(index)).ffill().reindex(index)


def split_factors(share_facts) -> pd.Series:
    """Multiplier taking each period's as-filed share count to today's basis.

    Recovered from the share count itself: a 10:1 split appears as a 10x jump
    between consecutive filings, which no issuance or buyback can imitate.
    Snapped to the nearest sensible ratio by history._snap_split, which exists
    for exactly this in the 13F path -- share counts there are as-filed while
    stored closes are back-adjusted, and a 25:1 split rendered as a manager
    adding 2,400%.
    """
    ordered = sorted({f.period_end: f for f in share_facts}.values(),
                     key=lambda f: f.period_end)
    ends = [f.period_end for f in ordered]
    factors = pd.Series(1.0, index=ends)
    if len(ordered) < 2:
        return factors
    cumulative = 1.0
    for i in range(len(ordered) - 1, 0, -1):
        prev, cur = ordered[i - 1].value, ordered[i].value
        ratio = (cur / prev) if prev else 1.0
        if ratio >= SPLIT_MIN_RATIO:
            cumulative *= history._snap_split(ratio)
        factors.iloc[i - 1] = cumulative
    return factors


def shares_series(share_facts, dates) -> pd.Series:
    """Split-adjusted diluted shares, forward-filled onto `dates`."""
    factors = split_factors(share_facts)
    adjusted = [
        (f.filed, f.value * float(factors.get(f.period_end, 1.0)))
        for f in sorted(share_facts, key=lambda f: f.filed)
    ]
    if not adjusted:
        return pd.Series(index=pd.DatetimeIndex(dates), dtype=float)
    stepped = pd.Series(dict(adjusted))
    stepped.index = pd.DatetimeIndex(stepped.index)
    stepped = stepped.sort_index()
    index = pd.DatetimeIndex(dates)
    return stepped.reindex(stepped.index.union(index)).ffill().reindex(index)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_valuation.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: 272 passed

- [ ] **Step 6: Commit**

```bash
git add valuation.py tests/test_valuation.py
git commit -m "feat: point-in-time TTM aggregates and split-adjusted shares

A trailing-twelve-month total is the four most recent quarters filed on or
before a date, so an amendment supersedes its original only once it has been
filed -- before that the market had the original number. The series steps on
filing dates rather than period ends, which is the difference between a
lookahead-free history and one that credits the market with knowing Q2 earnings
a month before they were published.

Splits are recovered from the diluted share count, where a 10:1 appears as a 10x
jump no issuance can imitate, and snapped by history._snap_split -- which exists
for exactly this problem in the 13F path."
```

---

## Task 10.5: Correct the chain election and the Q4 reconstruction, then rebuild

Inserted after Task 10 landed, by two findings measured against the live store.
Task 11's proof gate cannot pass without it. The full brief, with the tests and
the two hard gates, is at
`.superpowers/sdd/2026-09-05-valuation-history/task-10.5-brief.md`; the
measurements are in that directory's `progress.md`.

**Part 1 -- `xbrl.splice` replaces `pick_tag`.** `pick_tag` elected "the chain
member carrying the most facts, held fixed for a ticker's whole series". For
revenue that reliably elects a tag that stopped being filed: eleven years of
pre-2018 `SalesRevenueNet` outnumber eight years of post-ASC-606 filings. In
the store, 143 tickers have revenue facts and only 70 have any dated
2025-06-01 or later; 57 of the stale ones stop in 2018. `ttm_series`
forward-fills, so P/S for a third of the universe would have been an
eight-year-old revenue figure. `splice` takes the live tag as primary and
fills strictly earlier periods from the next-best, so no period is ever
reported by two tags -- which was `pick_tag`'s real reason for holding one
fixed. The backfill already fetches every chain member and discards the losers,
so storing them costs no additional requests.

**Part 2 -- three defects in `quarterly()`**, all surfaced by the guard Task 10
added to `valuation._known_at`, which fired on 439 of 1,428 (ticker, concept)
pairs in the live store. It synthesizes a Q4 even when the filer published one
(the dedup key uses a `period_start` that differs by a single day); it
decomposes any annual-length span rather than only a fiscal year, so Amazon's
rolling twelve-month facts produced a Q4 of -518,000,000 against the filer's
own +82,000,000; and it never re-checks the span it emits, so a 183-day
remainder shipped as a quarter.

Where a filer published its own Q4, the reconstruction already matched it to
the cent in 4,615 of 6,193 cases -- so the method is sound and stays. Only the
guards around it change.

Both halves change what belongs in `reported`, so the table is cleared and
rebuilt once at the end rather than twice.

**Gates:** `valuation.ttm_at` must raise zero times across the store, down from
439. `revenue` must reach at least 130 tickers with a recent fact, up from 70.

---

## Task 11: Daily multiple series, own-range percentiles, and the proof report

**Files:**
- Modify: `valuation.py`
- Modify: `tools/prove_xbrl.py` (add `report`)
- Modify: `tests/test_valuation.py`, `tests/test_prove_xbrl.py`

**Interfaces:**
- Consumes: `valuation.ttm_series`, `valuation.shares_series` from Task 10; `closeRaw` from Task 9; `prove_xbrl.proof` from Task 8
- Produces: `valuation.METRIC_SPECS: dict[str, tuple[str, ...]]`, `valuation.multiple_series(ticker, facts_by_tag, closes_raw) -> pd.DataFrame` (index dates, columns `trailingPE, ps, evEbitda, fcfYield`), `valuation.own_percentile(series) -> float | None`, `valuation.build_all(conn, tickers) -> dict[str, pd.DataFrame]`, `prove_xbrl.report(conn) -> pd.DataFrame`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_valuation.py`:

```python
def test_market_cap_uses_the_unadjusted_close_and_adjusted_shares():
    dates = pd.to_datetime(["2026-08-01"])
    facts = {
        "netIncome": _four_quarters(),
        "shares": [_q("2026-06-30", "2026-07-31", 1000.0, "2026-04-01",
                      2026, "Q2")],
    }
    closes = pd.Series([50.0], index=dates)
    out = valuation.multiple_series("T", facts, closes)
    # cap = 50 x 1000 = 50,000; TTM net income = 460 -> P/E = 108.7
    assert out.loc[dates[0], "trailingPE"] == pytest.approx(50000 / 460, rel=1e-6)


def test_a_negative_ttm_earnings_yields_no_pe_rather_than_a_negative_one():
    dates = pd.to_datetime(["2026-08-01"])
    losses = [_q(e, f, -50.0, s, y, p) for e, f, s, y, p in [
        ("2025-09-30", "2025-10-30", "2025-07-01", 2025, "Q3"),
        ("2025-12-31", "2026-02-15", "2025-10-01", 2025, "Q4"),
        ("2026-03-31", "2026-04-30", "2026-01-01", 2026, "Q1"),
        ("2026-06-30", "2026-07-31", "2026-04-01", 2026, "Q2")]]
    facts = {"netIncome": losses,
             "shares": [_q("2026-06-30", "2026-07-31", 1000.0, "2026-04-01",
                           2026, "Q2")]}
    out = valuation.multiple_series("T", facts, pd.Series([50.0], index=dates))
    assert pd.isna(out.loc[dates[0], "trailingPE"]), \
        "the same domain rule as the peer frame: undefined, not negative"


def test_a_balance_sheet_level_is_not_summed_over_four_quarters():
    # Cash is a level on a date. Summing four quarters of it would count the
    # same money four times and understate EV/EBITDA badly.
    cash = [_q(e, f, 50.0, None, 2026, p) for e, f, p in [
        ("2025-09-30", "2025-10-30", "Q3"), ("2025-12-31", "2026-02-15", "Q4"),
        ("2026-03-31", "2026-04-30", "Q1"), ("2026-06-30", "2026-07-31", "Q2")]]
    s = valuation.latest_series(cash, pd.to_datetime(["2026-08-01"]))
    assert s.iloc[0] == pytest.approx(50.0), "the level, not 200.0"


def test_own_percentile_places_today_in_its_own_range():
    s = pd.Series(range(100), dtype=float)
    assert valuation.own_percentile(s) == pytest.approx(0.99, abs=0.02)
    assert valuation.own_percentile(pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])) \
        == pytest.approx(0.0, abs=0.01)


def test_own_percentile_needs_a_real_history():
    assert valuation.own_percentile(pd.Series([1.0, 2.0])) is None
    assert valuation.own_percentile(pd.Series(dtype=float)) is None
```

Append to `tests/test_prove_xbrl.py`:

```python
def test_report_marks_a_failing_pair_as_not_passed():
    frame = prove.summarize({
        ("AAPL", "trailingPE"): (0.002, True),
        ("NET", "evEbitda"): (0.51, False),
    })
    assert set(frame.columns) == {"ticker", "metric", "median_error", "passed"}
    assert not frame.set_index(["ticker", "metric"]).loc[
        ("NET", "evEbitda"), "passed"]
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_valuation.py -k series -v`
Expected: FAIL with `AttributeError: module 'valuation' has no attribute 'multiple_series'`

- [ ] **Step 3: Add the series builder to `valuation.py`**

```python
# Which aggregates each multiple needs. Every one is a dollar total, so none
# carries a share basis a split could corrupt.
METRIC_SPECS: dict[str, tuple[str, ...]] = {
    "trailingPE": ("netIncome",),
    "ps":         ("revenue",),
    "evEbitda":   ("opIncome", "dep", "debtLT", "debtST", "cash"),
    "fcfYield":   ("cfo", "capex"),
}

# Below this many observations an "own range" is not a range. Roughly one
# fiscal year of trading days.
MIN_HISTORY = 250


def _ttm(facts_by_tag: dict, name: str, dates) -> pd.Series:
    return ttm_series(facts_by_tag.get(name, []), dates)


def latest_series(facts, dates) -> pd.Series:
    """The newest reported level as of each date, forward-filled.

    For balance-sheet items only. Cash is a level on a date, not a flow, so
    summing four quarters of it would count the same money four times -- and
    running it through quarterly() first would drop it entirely, since an
    instant fact has no duration to match.
    """
    index = pd.DatetimeIndex(dates)
    if not len(facts):
        return pd.Series(index=index, dtype=float)
    known = {}
    for f in sorted(facts, key=lambda f: (f.filed, f.period_end)):
        known[f.filed] = float(f.value)
    stepped = pd.Series(known)
    stepped.index = pd.DatetimeIndex(stepped.index)
    stepped = stepped.sort_index()
    return stepped.reindex(stepped.index.union(index)).ffill().reindex(index)


def _level(facts_by_tag: dict, name: str, dates) -> pd.Series:
    return latest_series(facts_by_tag.get(name, []), dates)


def multiple_series(ticker: str, facts_by_tag: dict,
                    closes_raw: pd.Series) -> pd.DataFrame:
    """Daily trailingPE, ps, evEbitda and fcfYield for one ticker.

    Undefined ratios are NaN rather than negative, the same rule the peer frame
    applies: a negative denominator makes the multiple absent, not cheap.
    """
    dates = pd.DatetimeIndex(closes_raw.index)
    shares = shares_series(facts_by_tag.get("shares", []), dates)
    cap = closes_raw.astype(float) * shares

    out = pd.DataFrame(index=dates)

    earnings = _ttm(facts_by_tag, "netIncome", dates)
    out["trailingPE"] = (cap / earnings).where(earnings > 0)

    revenue = _ttm(facts_by_tag, "revenue", dates)
    out["ps"] = (cap / revenue).where(revenue > 0)

    ebitda = (_ttm(facts_by_tag, "opIncome", dates)
              + _ttm(facts_by_tag, "dep", dates))
    debt = (_level(facts_by_tag, "debtLT", dates).fillna(0.0)
            + _level(facts_by_tag, "debtST", dates).fillna(0.0))
    cash = _level(facts_by_tag, "cash", dates).fillna(0.0)
    out["evEbitda"] = ((cap + debt - cash) / ebitda).where(ebitda > 0)

    fcf = (_ttm(facts_by_tag, "cfo", dates)
           - _ttm(facts_by_tag, "capex", dates))
    out["fcfYield"] = (fcf / cap * 100).where(cap > 0)

    return out


def own_percentile(series: pd.Series) -> float | None:
    """Where the newest value sits in its own history, 0 = cheapest ever."""
    clean = pd.Series(series).dropna()
    if len(clean) < MIN_HISTORY:
        return None
    return float((clean < clean.iloc[-1]).mean())


def build_all(conn, tickers) -> dict:
    """Every ticker's series, from the store. ~1s for 153 x 1,270 x 4."""
    prices = pd.read_sql_query(
        "SELECT ticker, as_of, value FROM metrics "
        "WHERE period_type='daily' AND metric='closeRaw'", conn)
    facts = history.reported(conn)
    out = {}
    for ticker in tickers:
        px = prices[prices.ticker == ticker]
        if px.empty:
            continue
        series = pd.Series(px.value.values,
                           index=pd.DatetimeIndex(px.as_of)).sort_index()
        tf = facts[facts.ticker == ticker]
        by_tag = {name: _facts_for(tf, chain)
                  for name, chain in _CHAINS.items()}
        out[ticker] = multiple_series(ticker, by_tag, series)
    return out
```

`_CHAINS` and `_facts_for` map the stored `concept` tags back to the logical
names in `METRIC_SPECS`:

```python
import xbrl

_CHAINS = xbrl.CONCEPTS


def _facts_for(ticker_facts: pd.DataFrame, chain) -> list:
    """Rows for whichever chain member this ticker actually used."""
    present = ticker_facts[ticker_facts.concept.isin(chain)]
    if present.empty:
        return []
    tag = present.concept.value_counts().idxmax()
    rows = present[present.concept == tag]
    return [xbrl.Fact(r.ticker, r.concept, r.period_start, r.period_end,
                      None if pd.isna(r.fy) else int(r.fy), r.fp, r.form,
                      r.filed, float(r.value))
            for r in rows.itertuples()]
```

- [ ] **Step 4: Add `summarize` and `report` to `tools/prove_xbrl.py`**

```python
def summarize(results: dict) -> pd.DataFrame:
    """(ticker, metric) -> (median_error, passed), as a frame."""
    return pd.DataFrame(
        [{"ticker": t, "metric": m, "median_error": e, "passed": ok}
         for (t, m), (e, ok) in sorted(results.items())])


def report(conn) -> pd.DataFrame:
    """Prove every (ticker, metric) against Yahoo's own published value."""
    import valuation
    snap = pd.read_sql_query(
        "SELECT ticker, as_of, metric, value FROM metrics "
        "WHERE period_type='snapshot' AND metric IN "
        "('trailingPE','ps','evEbitda','fcfYield')", conn)
    tickers = sorted(snap.ticker.unique())
    built = valuation.build_all(conn, tickers)
    results = {}
    for ticker, frame in built.items():
        ref_all = snap[snap.ticker == ticker]
        for metric in frame.columns:
            ref = ref_all[ref_all.metric == metric]
            if ref.empty:
                continue
            reference = pd.Series(ref.value.values,
                                  index=pd.DatetimeIndex(ref.as_of))
            results[(ticker, metric)] = proof(frame[metric], reference)
    return summarize(results)
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/ -q`
Expected: 277 passed

- [ ] **Step 6: Run the proof against real data — this is the gate**

```bash
python - <<'EOF'
import sys; sys.path.insert(0, "tools")
import history, prove_xbrl as p
r = p.report(history.connect())
print(r.groupby("metric").agg(
    n=("passed", "size"), passed=("passed", "sum"),
    median_err=("median_error", "median")).round(4).to_string())
rate = r.groupby("metric").passed.mean()
print("\npass rate:\n", (rate * 100).round(1).to_string())
for m, v in rate.items():
    if v < 0.90:
        print(f"WARNING {m}: {v:.0%} — normalization is wrong, not the data")
EOF
```

A metric under 90% means the concept chain or the EBITDA definition is wrong.
Fix it before continuing — every later task renders what this produces.

- [ ] **Step 7: Commit**

```bash
git add valuation.py tools/prove_xbrl.py tests/test_valuation.py tests/test_prove_xbrl.py
git commit -m "feat: daily multiple series, proved against the source

Market cap is the unadjusted close times split-adjusted shares; every numerator
is a dollar aggregate. An undefined ratio is NaN rather than negative, the same
rule the peer frame now applies -- a negative denominator makes a multiple
absent, not cheap.

The proof report is the gate: a metric passing on under 90% of names means the
concept chain or the EBITDA definition is wrong, not that the data is bad."
```

---

## Task 11.5: An honest TTM, and the right share basis for market cap

Inserted after Task 11's proof gate failed on all four metrics. The brief is at
`.superpowers/sdd/2026-09-05-valuation-history/task-11.5-brief.md`; the
diagnosis is the "Task 11 GATE" entry in that directory's `progress.md`.

The gate's single failure number was hiding two different results. `trailingPE`
and `ps` are sound -- a quarter of names reconstruct to within 0.31%, two
thirds within 2% -- and fail only because `PASS_TOLERANCE` is 1% while their
median error is ~1%, so about half fail by construction. Market cap alone
carries 0.91% of that, because the store used
`WeightedAverageNumberOfDilutedSharesOutstanding`: the right concept for EPS,
the wrong one for market cap. Switching to `dei:EntityCommonStockSharesOut-
standing` makes six of seven probed tickers exact (MCD 100.00% -> 0.00%).

`fcfYield` is genuinely broken, structurally: cash-flow statements are
cumulative, `quarterly()` only reconstructs the annual case, and `ttm_at` never
checked that its four quarters span a year -- so it summed four quarters spread
across three years. That guard matters more than the metric that revealed it.

`evEbitda` is broken for two reasons, one of them unfixable here: `debtLT` is
absent for 42 of 153 tickers, and Yahoo's `enterpriseToEbitda` uses a non-GAAP
adjusted EBITDA that cannot be reconstructed from GAAP tags at all. Left to the
proof harness to suppress, per the design's own rule that a name ends up with
no EV/EBITDA history rather than a wrong one.

**Gate:** market cap median error against Yahoo below 0.2%, from 0.91%.

---

## Task 12: The frame select and the own-history frame

**Files:**
- Modify: `render.py` (`TOOLBAR`, `JS_TMPL`, `render_sector`, `render_html`)
- Modify: `build_dashboard.py` (pass the series in)
- Modify: `tests/test_render.py`

**Interfaces:**
- Consumes: `valuation.build_all`, `valuation.own_percentile`, `prove_xbrl.report`
- Produces: cells carrying `data-oh`; `render.render_html(..., own=None)` where `own` is `dict[(ticker, metric)] -> float`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_render.py`:

```python
def _one_band(**extra):
    df = pd.DataFrame({
        "ticker": ["ORCL", "MSFT", "FTNT"],
        "name": ["Oracle", "Microsoft", "Fortinet"],
        "subindustry": ["Infra"] * 3,
        "trailingPE": [19.18, 19.77, 42.84],
    }).set_index("ticker", drop=False)
    return df


def test_a_cell_carries_its_own_history_percentile():
    html = render.render_sector(
        "TMT", _one_band(), {}, [("trailingPE", "Trail P/E", "val", "x", False)],
        {"TMT": ["Infra"]}, {"val": "Valuation"}, {},
        own={("ORCL", "trailingPE"): 0.12},
    )
    row = html[html.index('data-tk="ORCL"'):]
    assert "data-oh='0.1200'" in row[:row.index("</tr>")]


def test_a_pair_that_failed_the_proof_carries_no_own_history():
    html = render.render_sector(
        "TMT", _one_band(), {}, [("trailingPE", "Trail P/E", "val", "x", False)],
        {"TMT": ["Infra"]}, {"val": "Valuation"}, {}, own={},
    )
    assert "data-oh" not in html


def test_the_toolbar_offers_every_frame():
    for value in ("peers", "own", "c1w", "c1m"):
        assert f'value="{value}"' in render.TOOLBAR
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_render.py -k frame -v`
Expected: FAIL — `render_sector() got an unexpected keyword argument 'own'`

- [ ] **Step 3: Add the select to `TOOLBAR`**

Immediately before the existing `Tint vs` block:

```html
  <span class="tlab">Frame</span>
  <select id="frame">
    <option value="peers">vs peers</option>
    <option value="own">vs own history</option>
    <option value="c1w">change 1w</option>
    <option value="c1m">change 1m</option>
  </select>
```

- [ ] **Step 4: Emit `data-oh` in `render_sector`**

`render_sector` and `render_html` take `own: dict | None = None`. Inside the
cell loop, after the `data-sc` block:

```python
                oh = (own or {}).get((tk, key))
                if oh is not None:
                    attrs += f" data-oh='{oh:.4f}'"
```

- [ ] **Step 5: Teach the JS to paint from the selected frame**

In `JS_TMPL`, extend the state and `applyTint`:

```javascript
var st={tint:'ss',frame:'peers',flat:false,q:'',
        groups:{val:1,grow:1,prof:1,bal:1}};

// An own-history percentile is 0..1 where 0 is the cheapest the name has been.
// The peer scale is -1..1 with +1 favorable, so a cheap multiple maps to +1.
function frameScore(td){
  if(st.frame==='peers'){
    return st.tint==='off'?null
      :td.getAttribute(st.tint==='ss'?'data-ss':'data-sc');
  }
  if(st.frame==='own'){
    var p=td.getAttribute('data-oh');
    return p===null?null:String(1-2*parseFloat(p));
  }
  return td.getAttribute(st.frame==='c1w'?'data-c1w':'data-c1m');
}
function applyTint(){
  $$('td.num').forEach(function(td){
    var v=frameScore(td);
    td.style.background=(v===null)?'':tintOf(parseFloat(v));
  });
}
$('#frame').addEventListener('change',function(e){
  st.frame=e.target.value;
  $('#tint').disabled=(st.frame!=='peers');
  applyTint();
});
```

- [ ] **Step 6: Wire the data through `build_dashboard.py`**

```python
def own_history(conn, df) -> dict:
    """(ticker, metric) -> own-range percentile, for pairs that passed the proof.

    A pair that failed gets no entry, so the frame renders blank rather than
    wrong. Non-fatal for the same reason record_history() is: no valuation
    history must ever cost you the dashboard.
    """
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import prove_xbrl
        import valuation
        passed = prove_xbrl.report(conn)
        ok = {(r.ticker, r.metric) for r in passed.itertuples() if r.passed}
        series = valuation.build_all(conn, sorted(df.ticker))
        out = {}
        for ticker, frame in series.items():
            for metric in frame.columns:
                if (ticker, metric) not in ok:
                    continue
                pct = valuation.own_percentile(frame[metric])
                if pct is not None:
                    out[(ticker, metric)] = pct
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"  own-history frame unavailable: {exc}", file=sys.stderr)
        return {}
```

and pass `own=own_history(conn, df)` into `render.render_html`.

- [ ] **Step 7: Run the tests and look at the page**

```bash
python -m pytest tests/ -q          # expect 280 passed
python build_dashboard.py --no-fetch
python - <<'EOF'
import re
h = open("dashboard.html").read()
print("cells with an own-history percentile:", h.count("data-oh="))
print("frame select present:", 'id="frame"' in h)
EOF
open dashboard.html   # switch Frame to "vs own history"
```

- [ ] **Step 8: Commit**

```bash
git add render.py build_dashboard.py tests/test_render.py
git commit -m "feat: add the own-history frame to the dashboard

A Frame select beside the existing Tint control, re-painting the same table
rather than adding columns -- the page already carries 17 and a second number
per cell would cost it the thing it is best at. An own-history percentile of 0
is the cheapest the name has been, mapped onto the existing -1..1 blue/red
scale so one legend covers both frames.

A (ticker, metric) pair that failed the proof gets no percentile, so the frame
renders blank rather than wrong, and the whole path is non-fatal: no valuation
history must ever cost you the dashboard."
```

---

## Task 13: The drilldown

**Files:**
- Modify: `render.py` (`CSS`, `JS_TMPL`, `render_html`)
- Modify: `build_dashboard.py`
- Modify: `tests/test_render.py`

**Interfaces:**
- Consumes: `valuation.build_all` from Task 11
- Produces: `render.encode_series(frame, every=5) -> dict`, an embedded `__SERIES__` payload, a `#drill` panel

- [ ] **Step 1: Write the failing test**

```python
def test_encode_series_downsamples_to_weekly():
    idx = pd.date_range("2021-09-01", periods=1270, freq="B")
    frame = pd.DataFrame({"trailingPE": range(1270)}, index=idx, dtype=float)
    out = render.encode_series(frame, every=5)
    assert len(out["trailingPE"]["v"]) == pytest.approx(254, abs=2)
    assert out["trailingPE"]["t0"] == "2021-09-01"


def test_encode_series_rounds_to_four_significant_figures():
    idx = pd.date_range("2021-09-01", periods=10, freq="B")
    frame = pd.DataFrame({"ps": [1.23456789] * 10}, index=idx)
    assert out_v(render.encode_series(frame, every=1)["ps"]) == 1.235


def out_v(entry):
    return entry["v"][0]


def test_encode_series_keeps_gaps_as_null_not_zero():
    idx = pd.date_range("2021-09-01", periods=5, freq="B")
    frame = pd.DataFrame({"ps": [1.0, float("nan"), 3.0, 4.0, 5.0]}, index=idx)
    assert render.encode_series(frame, every=1)["ps"]["v"][1] is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_render.py -k encode -v`
Expected: FAIL — `module 'render' has no attribute 'encode_series'`

- [ ] **Step 3: Implement `encode_series`**

```python
# Weekly is plenty for a five-year range chart and costs a fifth of the bytes.
# Percentiles are computed from the *daily* series server-side, so downsampling
# only ever affects the picture, never a number the reader is shown.
SERIES_EVERY = 5


def encode_series(frame, every: int = SERIES_EVERY) -> dict:
    """One ticker's multiple history, compactly, for the drilldown chart."""
    out = {}
    for column in frame.columns:
        s = frame[column].iloc[::every]
        if s.dropna().empty:
            continue
        out[column] = {
            "t0": s.index[0].strftime("%Y-%m-%d"),
            "step": every,
            "v": [None if pd.isna(v) else round(float(v), 4) for v in s],
        }
    return out
```

- [ ] **Step 4: Embed the payload and add the panel**

In `render_html`, add `<script>var SERIES=__SERIES__;</script>` via the same
`.replace` idiom `build_js` already uses, and a panel before the footer:

```html
<div id="drill" hidden>
  <div class="drill-card">
    <button class="drill-x" aria-label="Close">x</button>
    <h3 id="drill-title"></h3>
    <svg id="drill-chart" viewBox="0 0 640 200" width="100%" height="200"></svg>
    <p id="drill-note" class="na"></p>
  </div>
</div>
```

CSS:

```css
#drill{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;
       align-items:center;justify-content:center;z-index:50;}
#drill[hidden]{display:none;}
.drill-card{background:var(--page);border:1px solid var(--line);border-radius:8px;
            padding:18px;max-width:min(720px,92vw);}
.drill-x{float:right;background:none;border:0;color:var(--muted);cursor:pointer;
         font-size:16px;}
```

JS — a click handler on valuation cells, drawing a polyline the same way
`sparkline` does server-side:

```javascript
// KEYS is the metric order build_js() embeds. The first cell of a row is the
// sticky name column, so a cell's metric is its position less one.
function colOf(td){
  return Array.prototype.indexOf.call(td.parentNode.children, td) - 1;
}
function drawDrill(tk,key){
  var s=(SERIES[tk]||{})[key];
  if(!s){return;}
  var pts=s.v, n=pts.length, lo=Infinity, hi=-Infinity;
  pts.forEach(function(v){if(v!==null){lo=Math.min(lo,v);hi=Math.max(hi,v);}});
  if(!(hi>lo)){return;}
  var d='', seen=false;
  pts.forEach(function(v,i){
    if(v===null){seen=false;return;}
    var x=i/(n-1)*640, y=200-(v-lo)/(hi-lo)*190-5;
    d+=(seen?'L':'M')+x.toFixed(1)+' '+y.toFixed(1)+' ';
    seen=true;
  });
  var last=null;
  for(var i=pts.length-1;i>=0;i--){if(pts[i]!==null){last=pts[i];break;}}
  var below=pts.filter(function(v){return v!==null&&v<last;}).length;
  var valid=pts.filter(function(v){return v!==null;}).length;
  $('#drill-chart').innerHTML=
    "<path d='"+d+"' fill='none' stroke='currentColor' stroke-width='1.5'/>";
  $('#drill-title').textContent=tk+' — '+key;
  $('#drill-note').textContent=
    'now '+last.toFixed(1)+'  ·  range '+lo.toFixed(1)+'–'+hi.toFixed(1)+
    '  ·  '+Math.round(below/valid*100)+'th percentile of its own history';
  $('#drill').hidden=false;
}
document.addEventListener('click',function(e){
  var td=e.target.closest?e.target.closest('td.num.g-val'):null;
  if(td&&td.getAttribute('data-oh')!==null){
    drawDrill(td.closest('tr').dataset.tk, KEYS[colOf(td)]);
  }
  if(e.target.closest&&e.target.closest('.drill-x')){$('#drill').hidden=true;}
});
```

- [ ] **Step 5: Measure the payload — this is the decision point**

```bash
python build_dashboard.py --no-fetch
ls -l dashboard.html | awk '{printf "dashboard.html: %.2f MB\n", $5/1048576}'
```

Expected roughly 1.2 MB, up from ~0.48 MB. **If it exceeds 2 MB**, change
`SERIES_EVERY` from 5 to 21 (monthly), which costs nothing visually at
five-year scale, and re-measure.

- [ ] **Step 6: Run the tests and commit**

```bash
python -m pytest tests/ -q     # expect 283 passed
git add render.py build_dashboard.py tests/test_render.py
git commit -m "feat: drill into a multiple's own five-year history

Clicking a valuation cell that has an own-history percentile opens its own
chart, its range, and where today sits in it. The series is downsampled to
weekly for the picture only -- every percentile the reader is shown is computed
from the full daily series server-side, so downsampling can never change a
number, only the resolution of a line."
```

---

## Task 14: Change frames

**Files:**
- Modify: `valuation.py`, `render.py`, `build_dashboard.py`
- Modify: `tests/test_valuation.py`, `tests/test_render.py`

**Interfaces:**
- Consumes: `valuation.build_all`, the `frame` select from Task 12
- Produces: `valuation.change(series, sessions) -> float | None`, `valuation.changes(conn, tickers) -> dict[(ticker, metric, window)] -> float`; cells carrying `data-c1w` / `data-c1m`

**Why:** spec §7. For the four valuation metrics these come from the derived
daily series and are available immediately at full depth. For margins, growth,
ROE and forward P/E they come from `snapshot`, which held 16 days on
2026-09-05 — so 1w works and 1m renders an em dash until the store accrues.

- [ ] **Step 1: Write the failing test**

```python
def test_change_is_a_log_ratio_over_the_window():
    import math
    s = pd.Series([100.0] * 20 + [110.0])
    assert valuation.change(s, 5) == pytest.approx(math.log(1.10), rel=1e-6)


def test_change_returns_nothing_when_the_window_exceeds_the_history():
    assert valuation.change(pd.Series([1.0, 2.0, 3.0]), 22) is None


def test_change_ignores_gaps_rather_than_treating_them_as_zero():
    s = pd.Series([100.0, float("nan"), float("nan"), float("nan"), 110.0])
    assert valuation.change(s, 4) is not None


def test_a_flat_series_changes_by_zero_not_by_nothing():
    assert valuation.change(pd.Series([50.0] * 30), 5) == pytest.approx(0.0)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_valuation.py -k change -v`
Expected: FAIL — `module 'valuation' has no attribute 'change'`

- [ ] **Step 3: Implement**

```python
import math

# Trading sessions, matching the convention already used for ret1m/ret6m.
WINDOWS = {"c1w": 5, "c1m": 22}


def change(series: pd.Series, sessions: int) -> float | None:
    """Log change over `sessions`, or None if the history is too short.

    A log ratio because it is the quantity that decomposes:
    dln(multiple) = dln(price) - dln(fundamental), which is what separates a
    market move from a source revision in the next task.
    """
    clean = pd.Series(series).dropna()
    if len(clean) < sessions + 1:
        return None
    now, then = float(clean.iloc[-1]), float(clean.iloc[-1 - sessions])
    if now <= 0 or then <= 0:
        return None
    return math.log(now / then)


def changes(conn, tickers) -> dict:
    out = {}
    for ticker, frame in build_all(conn, tickers).items():
        for metric in frame.columns:
            for name, sessions in WINDOWS.items():
                value = change(frame[metric], sessions)
                if value is not None:
                    out[(ticker, metric, name)] = value
    return out
```

- [ ] **Step 4: Emit the attributes and scale them for tinting**

In `render_sector`, after the `data-oh` block:

```python
                for window in ("c1w", "c1m"):
                    delta = (changes or {}).get((tk, key, window))
                    if delta is not None:
                        # Scaled so a 20% move saturates the tint, and signed so
                        # a falling multiple reads favorable on a lower-is-better
                        # metric, matching the peer frame's convention.
                        sign = -1.0 if _hb is False else 1.0
                        attrs += (f" data-{window}='{delta:.5f}'"
                                  f" data-{window}s='{max(-1.0, min(1.0, sign * delta / 0.20)):.4f}'")
```

The JS `frameScore` reads `data-c1ws` / `data-c1ms` for the tint and the raw
`data-c1w` for the arrow.

- [ ] **Step 5: Add the always-on direction arrow**

```javascript
// 0.5%: below this a move is rounding, not direction. The same constant marks
// a dln F data event in the restatement flag.
var DEADBAND=0.005;
function arrows(){
  $$('td.num').forEach(function(td){
    var old=td.querySelector('.arw'); if(old){old.remove();}
    var c=td.getAttribute('data-c1w'); if(c===null){return;}
    var v=parseFloat(c); if(Math.abs(v)<DEADBAND){return;}
    var s=document.createElement('span');
    s.className='arw'; s.textContent=v>0?'▲':'▼';
    td.appendChild(s);
  });
}
```

CSS: `.arw{font-size:8px;margin-left:3px;color:var(--muted);}`

- [ ] **Step 6: Run the tests, render, commit**

```bash
python -m pytest tests/ -q     # expect 287 passed
python build_dashboard.py --no-fetch
git add valuation.py render.py build_dashboard.py tests/
git commit -m "feat: change frames over 1 week and 1 month

For the four valuation metrics these come from the derived daily series, so they
are available immediately at full depth rather than waiting for the snapshot
table to accrue -- which is why the history was worth building first. A log
change, because it is the quantity that decomposes into price and fundamental.

The always-on arrow uses a 0.5% deadband so it shows direction rather than
rounding."
```

---

## Task 15: Separate a real report from a source revision

**Files:**
- Modify: `valuation.py`, `render.py`
- Modify: `tests/test_valuation.py`

**Interfaces:**
- Consumes: `history.reported` (filing dates), the `snapshot` metrics
- Produces: `valuation.data_events(prices, multiples, filings, deadband=0.005) -> list[tuple[str, str]]` returning `(date, kind)` where kind is `"report"` or `"revision"`; cells carrying `data-rs`

**Why:** spec §2.3-§2.4, §7. `dln M = dln P - dln F`. Fundamentals do not move
daily, so any daily `dln F` is a data event. EV/EBITDA's implied fundamental
moves more than 2% on **38.6%** of observations. AVGO moved 22% on 2026-09-04
with its last filing on 2026-06-09 — a source revision, not news.

- [ ] **Step 1: Write the failing test**

```python
def test_a_move_with_a_filing_behind_it_is_a_report():
    prices = pd.Series([100.0, 100.2],
                       index=pd.to_datetime(["2026-07-30", "2026-07-31"]))
    mult = pd.Series([20.0, 16.0],
                     index=pd.to_datetime(["2026-07-30", "2026-07-31"]))
    events = valuation.data_events(prices, mult, ["2026-07-31"])
    assert events == [("2026-07-31", "report")]


def test_avgo_2026_09_04_is_a_source_revision():
    # Price flat, EV/EBITDA 42.6 -> 33.4, implied EBITDA up 27%, and AVGO's
    # last periodic filing was 2026-06-09.
    prices = pd.Series([357.2, 357.9],
                       index=pd.to_datetime(["2026-09-03", "2026-09-04"]))
    mult = pd.Series([42.6, 33.4],
                     index=pd.to_datetime(["2026-09-03", "2026-09-04"]))
    events = valuation.data_events(prices, mult, ["2026-06-09"])
    assert events == [("2026-09-04", "revision")]


def test_an_ordinary_price_move_is_not_an_event():
    prices = pd.Series([100.0, 108.0],
                       index=pd.to_datetime(["2026-09-03", "2026-09-04"]))
    mult = pd.Series([20.0, 21.6],
                     index=pd.to_datetime(["2026-09-03", "2026-09-04"]))
    assert valuation.data_events(prices, mult, []) == []


def test_a_move_inside_the_deadband_is_not_an_event():
    prices = pd.Series([100.0, 100.0],
                       index=pd.to_datetime(["2026-09-03", "2026-09-04"]))
    mult = pd.Series([20.0, 20.05],
                     index=pd.to_datetime(["2026-09-03", "2026-09-04"]))
    assert valuation.data_events(prices, mult, []) == []
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/test_valuation.py -k events -v`
Expected: FAIL — `module 'valuation' has no attribute 'data_events'`

- [ ] **Step 3: Implement**

```python
# How close a data event must fall to a filing to be explained by it. A 10-Q
# reaches a data vendor within a few days, not the same afternoon.
FILING_WINDOW_DAYS = 5


def data_events(prices: pd.Series, multiples: pd.Series, filings,
                deadband: float = 0.005) -> list:
    """Days where the implied fundamental moved, and whether a filing explains it.

    F = P/M. A fundamental does not change daily, so any daily change in F is a
    data event rather than a market event. Cross-referencing filing dates splits
    those in two, and the difference matters: one is information, the other is
    the source changing its mind about the past.
    """
    pair = pd.concat([prices.rename("p"), multiples.rename("m")],
                     axis=1, join="inner").dropna()
    pair = pair[(pair.p > 0) & (pair.m > 0)]
    if len(pair) < 2:
        return []
    implied = pair.p / pair.m
    step = (implied / implied.shift(1)).apply(
        lambda r: math.log(r) if r and r > 0 else float("nan")).abs()
    filed = pd.DatetimeIndex(sorted(filings)) if len(filings) else None

    out = []
    for when, size in step.items():
        if pd.isna(size) or size < deadband:
            continue
        kind = "revision"
        if filed is not None and len(filed):
            gap = (when - filed).days
            recent = [g for g in gap if 0 <= g <= FILING_WINDOW_DAYS]
            if recent:
                kind = "report"
        out.append((when.strftime("%Y-%m-%d"), kind))
    return out
```

- [ ] **Step 4: Mark the cell**

In `render_sector`, alongside the change attributes:

```python
                mark = (events or {}).get((tk, key))
                if mark:
                    attrs += f" data-rs='{mark}'"
```

and in the JS arrow pass, append `*` for `report` and `!` for `revision`:

```javascript
    var rs=td.getAttribute('data-rs');
    if(rs){
      var m=document.createElement('span');
      m.className='rs rs-'+rs;
      m.textContent=(rs==='report')?'*':'!';
      m.title=(rs==='report')
        ? 'Moved on a new filing — real information'
        : 'Moved with no filing behind it — the source revised itself';
      td.appendChild(m);
    }
```

CSS: `.rs{font-size:9px;margin-left:2px;cursor:help;} .rs-revision{color:#e34948;font-weight:600;}`

- [ ] **Step 5: Verify against the case that motivated it**

```bash
python -m pytest tests/ -q     # expect 291 passed
python build_dashboard.py --no-fetch
python - <<'EOF'
import re
h = open("dashboard.html").read()
row = re.search(r'data-tk="AVGO".*?</tr>', h, re.S)
print("AVGO carries a revision mark:", "rs-revision" in row.group(0))
print("total revision marks on the page:", h.count("rs-revision"))
print("total report marks on the page:", h.count("rs-report"))
EOF
```

- [ ] **Step 6: Commit**

```bash
git add valuation.py render.py tests/test_valuation.py
git commit -m "feat: separate a real report from a source revision

F = P/M, and a fundamental does not change daily, so any daily move in the
implied fundamental is a data event rather than a market one. EV/EBITDA's moves
more than 2% on 38.6% of stored observations, which EBITDA plainly does not do.

Filing dates split those events in two. AVGO fell 22% on EV/EBITDA on
2026-09-04 with its last periodic filing on 2026-06-09: no filing behind it, so
it is marked ! rather than *, and a reader is told the source changed its mind
rather than being shown a 22% valuation move as though it were news."
```

---

## Verification, once every task is done

```bash
python -m pytest tests/ -q                    # 291 passed
python tools/backfill_xbrl.py                 # "Nothing due."
python build_dashboard.py --no-fetch
ls -l dashboard.html
```

Then, on the page itself:

- **Frame → vs peers.** NET's EV/EBITDA is greyed and italic, not deep blue. BA's ND/EBITDA is greyed. ORCL and MSFT are visibly more blue on EV/EBITDA than before Task 2.
- **Frame → vs own history.** Valuation cells re-tint; margins and growth go blank, since only the four valuation metrics have a proved history.
- **Click any tinted valuation cell.** Its own five-year chart opens with the range and percentile.
- **Frame → change 1w.** Arrows and tints agree in direction. AVGO carries `!` on EV/EBITDA and P/S.
- **Frame → change 1m.** The four valuation metrics populate; the rest render em dashes until the snapshot table reaches 22 sessions, around mid-October 2026.
