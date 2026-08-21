"""Render the history store as a self-contained HTML page.

Reads data/history.db and writes history.html — no server, no external assets,
same idiom as dashboard.html. The store's distinctive content is the perishable
one: forward consensus observed over time. Everything here is built to show that
against price, because their ratio is the decomposition the project is aiming at:

    ln(P1/P0) = ln(fPE1/fPE0) + ln(fEPS1/fEPS0)
       return  =   re-rating   +    revision

Usage:  python3 visualize.py [--out history.html]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import history

ROOT = Path(__file__).resolve().parent

# Categorical slots 1-2 and the diverging poles from the reference palette,
# validated in both modes: worst adjacent CVD dE 24.7 light / 26.8 dark against
# a >=8 target, normal-vision 33.6 / 31.8 against a >=15 floor, every mark >=3:1
# on its surface. Do not substitute by eye.
PALETTE = {
    "light": {"s1": "#2a78d6", "s2": "#eb6834", "pos": "#2a78d6", "neg": "#e34948"},
    "dark": {"s1": "#3987e5", "s2": "#d95926", "pos": "#3987e5", "neg": "#e66767"},
}


# How many filing lines the page carries per name. The store keeps everything;
# this only bounds the embedded payload, and the tail is what a reader scans.
INSIDER_SHOWN = 15


def _detail(conn) -> tuple[dict, dict, dict, dict]:
    """Per-company panels: snapshot metrics with peer rank, estimates, ownership."""
    snap = pd.read_sql_query(
        "SELECT ticker, metric, value FROM metrics WHERE period_type = 'snapshot' "
        "AND as_of = (SELECT MAX(as_of) FROM metrics WHERE period_type = 'snapshot')",
        conn,
    )
    peers = pd.read_sql_query(
        "SELECT ticker, subindustry FROM companies", conn
    ).set_index("ticker").subindustry.to_dict()
    snap["peer"] = snap.ticker.map(peers)
    # Percentile within the sub-industry, which is the comparison the dashboard
    # already scores on -- semis against semis, not against staples.
    snap["pct"] = snap.groupby(["metric", "peer"]).value.rank(pct=True)

    metrics: dict[str, list] = {}
    for t, g in snap.groupby("ticker"):
        metrics[t] = [{"metric": r.metric, "value": float(r.value),
                       "pct": None if pd.isna(r.pct) else round(float(r.pct), 3)}
                      for r in g.itertuples()]

    est = pd.read_sql_query(
        "SELECT ticker, metric, ref_period, value FROM metrics "
        "WHERE period_type = 'estimate' AND metric != 'epsEst' "
        "AND as_of = (SELECT MAX(as_of) FROM metrics WHERE period_type = 'estimate')",
        conn,
    )
    estimates: dict[str, list] = {}
    for t, g in est.groupby("ticker"):
        estimates[t] = [{"metric": r.metric, "ref": r.ref_period,
                         "value": float(r.value)} for r in g.itertuples()]

    try:
        hold = pd.read_sql_query(
            "SELECT ticker, as_of, kind, holder, shares, value, pct_held, pct_change "
            "FROM holdings ORDER BY pct_held DESC", conn)
    except Exception:
        hold = pd.DataFrame()
    holders: dict[str, list] = {}
    if len(hold):
        for t, g in hold.groupby("ticker"):
            holders[t] = [{"asOf": r.as_of, "kind": r.kind, "holder": r.holder,
                           "shares": None if pd.isna(r.shares) else float(r.shares),
                           "value": None if pd.isna(r.value) else float(r.value),
                           "pctHeld": None if pd.isna(r.pct_held) else float(r.pct_held),
                           "pctChange": None if pd.isna(r.pct_change) else float(r.pct_change)}
                          for r in g.itertuples()]

    try:
        ins = pd.read_sql_query(
            'SELECT ticker, as_of, insider, position, txn_type AS "transaction", '
            "shares, value, ownership FROM insider_txns ORDER BY as_of DESC", conn)
    except Exception:
        ins = pd.DataFrame()
    insiders: dict[str, list] = {}
    if len(ins):
        for t, g in ins.groupby("ticker"):
            insiders[t] = [{"asOf": r.as_of, "insider": r.insider,
                            "position": r.position, "txn": getattr(r, "transaction"),
                            "shares": None if pd.isna(r.shares) else float(r.shares),
                            "ownership": r.ownership}
                           for r in g.head(INSIDER_SHOWN).itertuples()]
    return metrics, estimates, holders, insiders


def _thirteenf(conn) -> tuple[dict, dict]:
    """13F positions per company, and the fund-first view of the same rows.

    Returns (per_ticker, fundview). Both are built from one pass over the two
    newest ingested quarters, because every number a reader wants here is a
    comparison between them.
    """
    try:
        quarters = history.thirteenf_quarters(conn)
    except Exception:  # noqa: BLE001 - a store predating this table
        return {}, {}
    if not quarters:
        return {}, {}
    quarter = quarters[0]
    prev = quarters[1] if len(quarters) > 1 else None

    try:
        changes = history.thirteenf_changes(conn, quarter, prev)
    except Exception:  # noqa: BLE001
        return {}, {}
    if not len(changes):
        return {}, {}

    meta = pd.read_sql_query(
        "SELECT cik, fund, cohort, book_value, n_positions, n_universe "
        "FROM thirteenf_filings WHERE quarter = ? AND status = 'ok'",
        conn, params=[quarter]).set_index("cik")

    def _f(value):
        return None if value is None or pd.isna(value) else float(value)

    per_ticker: dict[str, list] = {}
    for ticker, group in changes.groupby("ticker"):
        rows = []
        for r in group.itertuples():
            info = meta.loc[r.cik] if r.cik in meta.index else None
            rows.append({
                "fund": r.fund,
                "cohort": (info["cohort"] if info is not None else "") or "",
                "shares": _f(r.shares), "value": _f(r.value),
                "prevShares": _f(r.prev_shares),
                "deltaShares": _f(r.delta_shares), "deltaPct": _f(r.delta_pct),
                "pctOfBook": _f(r.pct_of_book), "action": r.action,
            })
        rows.sort(key=lambda x: (x["value"] or 0), reverse=True)
        per_ticker[ticker] = rows

    funds: list[dict] = []
    for cik, group in changes.groupby("cik"):
        info = meta.loc[cik] if cik in meta.index else None
        if info is None:
            continue
        held = group[group["shares"] > 0]
        positions = []
        for r in group.sort_values("value", ascending=False).itertuples():
            positions.append({
                "ticker": r.ticker, "shares": _f(r.shares), "value": _f(r.value),
                "deltaShares": _f(r.delta_shares), "deltaPct": _f(r.delta_pct),
                "pctOfBook": _f(r.pct_of_book), "action": r.action,
            })
        funds.append({
            "fund": info["fund"], "cohort": info["cohort"] or "",
            "bookValue": _f(info["book_value"]),
            "nFiled": int(info["n_positions"] or 0),
            "nUniverse": int(len(held)),
            "universeValue": float(held["value"].sum()),
            "positions": positions,
        })
    funds.sort(key=lambda f: f["universeValue"], reverse=True)

    return per_ticker, {"quarter": quarter, "prevQuarter": prev, "funds": funds}


def load(conn) -> dict:
    """Assemble the payload: one record per ticker, plus store-level totals."""
    companies = pd.read_sql_query(
        "SELECT ticker, name, sector, subindustry FROM companies", conn
    ).set_index("ticker")
    det_metrics, det_est, det_hold, det_ins = _detail(conn)
    det_13f, fundview = _thirteenf(conn)

    est = pd.read_sql_query(
        "SELECT ticker, ref_period, as_of, value FROM metrics "
        "WHERE metric = 'epsEst' AND ref_period LIKE 'FY%' ORDER BY ticker, as_of",
        conn,
    )
    # FY0 is the nearer fiscal year, FY1 the next; ref_period sorts naturally
    # within a ticker because the FY prefix is followed by an ISO date.
    order = est.groupby("ticker").ref_period.agg(["min", "max"])

    closes = pd.read_sql_query(
        "SELECT ticker, as_of, value FROM metrics "
        "WHERE period_type = 'daily' AND as_of >= '2026-04-01' ORDER BY ticker, as_of",
        conn,
    )
    dates = sorted(est.as_of.unique())

    records = []
    for ticker, grp in est.groupby("ticker"):
        fy0_ref, fy1_ref = order.loc[ticker, "min"], order.loc[ticker, "max"]
        fy0 = grp[grp.ref_period == fy0_ref].sort_values("as_of")
        fy1 = grp[grp.ref_period == fy1_ref].sort_values("as_of")

        px = closes[closes.ticker == ticker]
        # price on the last trading day at or before each estimate observation
        px_at = []
        for d in dates:
            prior = px[px.as_of <= d]
            px_at.append(float(prior.value.iloc[-1]) if len(prior) else None)

        # A percentage change is only meaningful when both ends are positive.
        # Loss-making names flip sign and would render as a huge false move.
        rev = None
        if len(fy0) >= 2:
            a, b = float(fy0.value.iloc[0]), float(fy0.value.iloc[-1])
            if a > 0 and b > 0:
                rev = (b / a - 1) * 100
        ret = None
        if px_at[0] and px_at[-1]:
            ret = (px_at[-1] / px_at[0] - 1) * 100

        info = companies.loc[ticker] if ticker in companies.index else None
        records.append({
            "ticker": ticker,
            "name": (info["name"] if info is not None else ticker) or ticker,
            "sector": (info["sector"] if info is not None else "") or "",
            "subindustry": (info["subindustry"] if info is not None else "") or "",
            "metrics": det_metrics.get(ticker, []),
            "estimates": det_est.get(ticker, []),
            "holders": det_hold.get(ticker, []),
            "insiders": det_ins.get(ticker, []),
            "funds13f": det_13f.get(ticker, []),
            "fy0Ref": fy0_ref, "fy1Ref": fy1_ref,
            "fy0": [{"d": r.as_of, "v": round(float(r.value), 5)}
                    for r in fy0.itertuples()],
            "fy1": [{"d": r.as_of, "v": round(float(r.value), 5)}
                    for r in fy1.itertuples()],
            "px": [None if p is None else round(p, 4) for p in px_at],
            "revision": None if rev is None else round(rev, 2),
            "priceReturn": None if ret is None else round(ret, 2),
        })

    cov = history.coverage(conn)
    totals = {
        "rows": int(cov.observations.sum()),
        "tickers": int(cov.tickers.max()),
        "estimateRows": int(cov[cov.period_type == "estimate"].observations.sum()),
        "priceRows": int(cov[cov.period_type == "daily"].observations.sum()),
        "priceFirst": str(cov[cov.period_type == "daily"].first_as_of.min()),
        "priceLast": str(cov[cov.period_type == "daily"].last_as_of.max()),
    }
    return {"dates": dates, "records": records, "totals": totals,
            "fundview": fundview, "palette": PALETTE}


def render(payload: dict) -> str:
    data = json.dumps(payload, separators=(",", ":"))
    return TEMPLATE.replace("__DATA__", data)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fundamentals History</title>
<style>
  :root {
    color-scheme: light;
    --plane:#f9f9f7; --surface:#fcfcfb;
    --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
    --s1:#2a78d6; --s2:#eb6834; --pos:#2a78d6; --neg:#e34948;
    --deemph:#c3c2b7;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --plane:#0d0d0d; --surface:#1a1a19;
      --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
      --s1:#3987e5; --s2:#d95926; --pos:#3987e5; --neg:#e66767;
      --deemph:#52514e;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --s1:#3987e5; --s2:#d95926; --pos:#3987e5; --neg:#e66767;
    --deemph:#52514e;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--plane); color:var(--ink);
    font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  }
  .wrap { max-width:1180px; margin:0 auto; padding:32px 20px 64px; }
  header h1 { font-size:22px; font-weight:600; margin:0 0 4px; letter-spacing:-0.01em; }
  header p { margin:0; color:var(--ink-2); font-size:13px; }

  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
          gap:12px; margin:24px 0 20px; }
  .tile { background:var(--surface); border:1px solid var(--border); border-radius:10px;
          padding:14px 16px; }
  .tile .label { color:var(--ink-2); font-size:12px; }
  .tile .value { font-size:26px; font-weight:600; margin-top:2px; letter-spacing:-0.02em; }
  .tile .sub { color:var(--muted); font-size:11px; margin-top:2px; }

  .filters { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
             margin:0 0 20px; padding:12px 14px; background:var(--surface);
             border:1px solid var(--border); border-radius:10px; }
  .filters label { color:var(--ink-2); font-size:12px; }
  select, button {
    font:inherit; font-size:13px; color:var(--ink); background:var(--surface);
    border:1px solid var(--axis); border-radius:7px; padding:5px 9px; cursor:pointer;
  }
  button[aria-pressed="true"] { border-color:var(--s1); color:var(--s1); }

  .card { background:var(--surface); border:1px solid var(--border); border-radius:10px;
          padding:18px 20px 20px; margin-bottom:18px; }
  .card h2 { font-size:15px; font-weight:600; margin:0 0 2px; }
  .card .desc { color:var(--ink-2); font-size:12.5px; margin:0 0 14px; max-width:70ch; }
  .card-head { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }

  .legend { display:flex; gap:16px; flex-wrap:wrap; margin:0 0 10px; }
  .legend span { display:inline-flex; align-items:center; gap:6px;
                 color:var(--ink-2); font-size:12px; }
  .key-line { width:14px; height:2px; border-radius:1px; display:inline-block; }
  .key-rect { width:11px; height:11px; border-radius:2px; display:inline-block; }

  .plot { width:100%; overflow-x:auto; }
  svg { display:block; }
  .tick { fill:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
  .dlabel { fill:var(--ink-2); font-size:11px; font-variant-numeric:tabular-nums; }
  .glabel { fill:var(--ink); font-size:11.5px; }
  .gridline { stroke:var(--grid); stroke-width:1; }
  .axisline { stroke:var(--axis); stroke-width:1; }

  table { border-collapse:collapse; width:100%; font-size:12.5px;
          font-variant-numeric:tabular-nums; }
  th, td { text-align:right; padding:5px 10px; border-bottom:1px solid var(--grid); }
  th:first-child, td:first-child { text-align:left; font-variant-numeric:normal; }
  th { color:var(--ink-2); font-weight:600; }
  .tblwrap { max-height:340px; overflow:auto; margin-top:6px; }
  [hidden] { display:none !important; }

  .tip { position:fixed; pointer-events:none; z-index:20; background:var(--surface);
         border:1px solid var(--border); border-radius:8px; padding:8px 10px;
         font-size:12px; box-shadow:0 4px 16px rgba(0,0,0,0.14); max-width:260px; }
  .tip .tt { font-weight:600; margin-bottom:4px; }
  .tip .row { display:flex; align-items:center; gap:7px; justify-content:space-between; }
  .tip .row b { font-variant-numeric:tabular-nums; }
  .tip .nm { color:var(--ink-2); display:inline-flex; align-items:center; gap:6px; }
  .note { color:var(--muted); font-size:11.5px; margin-top:10px; }
  #dbody .tblwrap { max-height:560px; }
  .tabs { display:flex; gap:6px; flex-wrap:wrap; margin:2px 0 16px;
          border-bottom:1px solid var(--grid); padding-bottom:10px; }
  .tabs button { border-color:transparent; background:transparent; color:var(--ink-2); }
  .tabs button[aria-selected="true"] { border-color:var(--axis);
          background:var(--plane); color:var(--ink); font-weight:600; }
  .mgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
           gap:10px; }
  .mcell { border:1px solid var(--border); border-radius:8px; padding:9px 11px; }
  .mcell .mk { color:var(--ink-2); font-size:11.5px; }
  .mcell .mv { font-size:17px; font-weight:600; margin-top:1px;
               font-variant-numeric:tabular-nums; }
  .meter { height:4px; border-radius:2px; background:var(--grid); margin-top:7px;
           overflow:hidden; }
  .meter i { display:block; height:100%; background:var(--s1); border-radius:2px; }
  .mcell .mp { color:var(--muted); font-size:10.5px; margin-top:4px; }
  .up { color:var(--pos); } .dn { color:var(--neg); }
  .swatch { width:9px; height:9px; border-radius:2px; display:inline-block;
            margin-right:6px; vertical-align:middle; }
  .hit { fill:transparent; cursor:pointer; }
  :focus-visible { outline:2px solid var(--s1); outline-offset:2px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Fundamentals history</h1>
    <p id="sub"></p>
  </header>

  <div class="kpis" id="kpis"></div>

  <div class="filters">
    <label for="tk">Highlight company</label>
    <select id="tk"></select>
    <span style="color:var(--muted);font-size:12px">
      scopes every chart below — the selection is emphasised, never filtered away
    </span>
  </div>

  <section class="card">
    <div class="card-head">
      <div>
        <h2>90-day EPS revision, current fiscal year</h2>
        <p class="desc">How far consensus for this fiscal year moved over the window
          the store covers. The 20 largest moves in either direction.</p>
      </div>
      <button id="t1" aria-pressed="false">Table</button>
    </div>
    <div class="legend">
      <span><i class="key-rect" style="background:var(--pos)"></i>raised</span>
      <span><i class="key-rect" style="background:var(--neg)"></i>cut</span>
    </div>
    <div class="plot" id="c1"></div>
    <div class="tblwrap" id="tb1" hidden></div>
    <p class="note" id="n1"></p>
  </section>

  <section class="card">
    <div class="card-head">
      <div>
        <h2>Revision against price, same 90 days</h2>
        <p class="desc">The two halves of the decomposition this store exists to enable.
          A point far right that has not moved up has been re-rated down; one on the
          diagonal was carried by earnings alone.</p>
      </div>
      <button id="t2" aria-pressed="false">Table</button>
    </div>
    <div class="plot" id="c2"></div>
    <div class="tblwrap" id="tb2" hidden></div>
    <p class="note">Single series — one company per point. Companies with a
      loss at either end of the window are omitted: a percentage change across
      zero is not meaningful.</p>
  </section>

  <section class="card">
    <div class="card-head">
      <div>
        <h2 id="h3">Price and consensus, indexed</h2>
        <p class="desc">Both series indexed to 100 at the start of the window, on one
          axis — never two scales. Where price outruns consensus the gap is re-rating;
          where they track, the move was earnings.</p>
      </div>
      <button id="t3" aria-pressed="false">Table</button>
    </div>
    <div class="legend" id="lg3"></div>
    <div class="plot" id="c3"></div>
    <div class="tblwrap" id="tb3" hidden></div>
    <p class="note" id="n3"></p>
  </section>

  <section class="card" id="drill">
    <div class="card-head">
      <div>
        <h2 id="dtitle"></h2>
        <p class="desc" id="dsub"></p>
      </div>
    </div>
    <div class="tabs" id="dtabs"></div>
    <div id="dbody"></div>
    <p class="note" id="dnote"></p>
  </section>

  <section class="card" id="fundcard">
    <div class="chead">
      <div>
        <h2>Hedge fund book</h2>
        <p class="desc" id="fdesc"></p>
      </div>
      <select id="fsel"></select>
    </div>
    <div class="kpis" id="fkpis"></div>
    <div id="fbody"></div>
    <p class="note" id="fnote"></p>
  </section>
</div>

<script>
const DATA = __DATA__;
const R = DATA.records, DATES = DATA.dates;
const byTicker = Object.fromEntries(R.map(r => [r.ticker, r]));
let sel = byTicker.GOOGL ? "GOOGL" : R[0].ticker;

const NS = "http://www.w3.org/2000/svg";
const el = (n, a = {}) => { const e = document.createElementNS(NS, n);
  for (const k in a) e.setAttribute(k, a[k]); return e; };
const fmtPct = v => (v >= 0 ? "+" : "") + v.toFixed(1) + "%";
/* Round tick values a reader can hold in their head — 0/10/20, never 0/33.9/67.8. */
function niceTicks(lo, hi, target = 5) {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / target, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  const step = mag * (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10);
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step)
    out.push(Math.abs(t) < 1e-9 ? 0 : t);
  return out;
}
const fmtNum = v => v.toLocaleString(undefined, {maximumFractionDigits: 2});
const shortDate = d => new Date(d + "T00:00:00").toLocaleDateString(undefined,
  {month: "short", day: "numeric"});

/* ---------- tooltip ---------- */
const tip = document.createElement("div");
tip.className = "tip"; tip.hidden = true; document.body.appendChild(tip);
function showTip(x, y, title, rows) {
  tip.replaceChildren();
  const t = document.createElement("div"); t.className = "tt";
  t.textContent = title; tip.appendChild(t);
  for (const r of rows) {
    const line = document.createElement("div"); line.className = "row";
    const nm = document.createElement("span"); nm.className = "nm";
    if (r.color) { const k = document.createElement("i"); k.className = "key-line";
      k.style.background = r.color; nm.appendChild(k); }
    nm.appendChild(document.createTextNode(r.label));
    const v = document.createElement("b"); v.textContent = r.value;
    line.append(nm, v); tip.appendChild(line);
  }
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(x + 14, innerWidth - w - 8) + "px";
  tip.style.top = Math.max(8, Math.min(y - h - 12, innerHeight - h - 8)) + "px";
}
const hideTip = () => { tip.hidden = true; };
addEventListener("scroll", hideTip, true);

/* ---------- KPIs ---------- */
const T = DATA.totals;
document.getElementById("sub").textContent =
  `${T.tickers} companies · ${T.rows.toLocaleString()} observations · generated from data/history.db`;
const kpis = [
  ["Observations", T.rows.toLocaleString(), "across three period types"],
  ["Estimate points", T.estimateRows.toLocaleString(), `${DATES[0]} → ${DATES[DATES.length-1]}`],
  ["Daily closes", T.priceRows.toLocaleString(), `${T.priceFirst} → ${T.priceLast}`],
  ["Median revision", (() => { const v = R.map(r=>r.revision).filter(v=>v!=null).sort((a,b)=>a-b);
      return fmtPct(v[Math.floor(v.length/2)]); })(), "current fiscal year, 90 days"],
];
document.getElementById("kpis").replaceChildren(...kpis.map(([l, v, s]) => {
  const d = document.createElement("div"); d.className = "tile";
  const a = document.createElement("div"); a.className = "label"; a.textContent = l;
  const b = document.createElement("div"); b.className = "value"; b.textContent = v;
  const c = document.createElement("div"); c.className = "sub"; c.textContent = s;
  d.append(a, b, c); return d;
}));

/* ---------- ticker select ---------- */
const sl = document.getElementById("tk");
sl.replaceChildren(...[...R].sort((a,b)=>a.ticker<b.ticker?-1:1).map(r => {
  const o = document.createElement("option"); o.value = r.ticker;
  o.textContent = `${r.ticker} — ${r.name}`; return o;
}));
sl.value = sel;
sl.addEventListener("change", () => { sel = sl.value; drawAll(); });

/* ---------- chart 1: diverging bars ---------- */
function chart1() {
  const host = document.getElementById("c1"); host.replaceChildren();
  const withRev = R.filter(r => r.revision != null);
  const top = [...withRev].sort((a,b) => Math.abs(b.revision) - Math.abs(a.revision)).slice(0, 20);
  top.sort((a,b) => b.revision - a.revision);
  document.getElementById("n1").textContent =
    `${withRev.length} of ${R.length} companies have a meaningful percentage revision; `
    + `${R.length - withRev.length} are omitted for a loss at one end of the window.`;

  const rowH = 22, padT = 8, padB = 30, left = 78, right = 62;
  const W = Math.max(560, host.clientWidth || 900), H = padT + top.length * rowH + padB;
  const max = Math.max(...top.map(r => Math.abs(r.revision))) * 1.12;
  const cx = left + (W - left - right) / 2;
  const scale = v => cx + (v / max) * ((W - left - right) / 2);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, width: W, height: H,
    role: "img", "aria-label": "Largest 90-day EPS revisions"});

  for (const t of niceTicks(-max, max, 6)) {
    const x = scale(t);
    svg.appendChild(el("line", {x1:x, x2:x, y1:padT, y2:H-padB,
      class: t === 0 ? "axisline" : "gridline"}));
    const lb = el("text", {x, y: H-padB+16, class:"tick", "text-anchor":"middle"});
    lb.textContent = (t>0?"+":"") + t.toFixed(0) + "%"; svg.appendChild(lb);
  }
  top.forEach((r, i) => {
    const y = padT + i * rowH, bh = Math.min(14, rowH - 8);
    const pos = r.revision >= 0, x0 = scale(0), x1 = scale(r.revision);
    const w = Math.abs(x1 - x0), x = pos ? x0 : x1;
    const dim = r.ticker !== sel && sel != null;
    const g = el("g");
    // square at the baseline, 4px round at the data end
    const rad = 4, d = pos
      ? `M${x0} ${y} H${x1-rad} a${rad} ${rad} 0 0 1 ${rad} ${rad} V${y+bh-rad} a${rad} ${rad} 0 0 1 -${rad} ${rad} H${x0} Z`
      : `M${x0} ${y} H${x1+rad} a${rad} ${rad} 0 0 0 -${rad} ${rad} V${y+bh-rad} a${rad} ${rad} 0 0 0 ${rad} ${rad} H${x0} Z`;
    g.appendChild(el("path", {d: w > rad ? d : `M${x} ${y} h${w} v${bh} h-${w} Z`,
      fill: pos ? "var(--pos)" : "var(--neg)", opacity: dim ? 0.85 : 1}));
    const nm = el("text", {x: left-10, y: y+bh-2, class:"glabel", "text-anchor":"end",
      "font-weight": r.ticker === sel ? 700 : 400});
    nm.textContent = r.ticker; g.appendChild(nm);
    const vl = el("text", {x: pos ? x1+7 : x1-7, y: y+bh-2, class:"dlabel",
      "text-anchor": pos ? "start" : "end"});
    vl.textContent = fmtPct(r.revision); g.appendChild(vl);
    const hit = el("rect", {x:left, y, width:W-left-right, height:rowH, class:"hit",
      tabindex:0, role:"button", "aria-label": `${r.ticker} ${fmtPct(r.revision)}`});
    const show = ev => { const b = hit.getBoundingClientRect();
      showTip(ev.clientX ?? b.right, ev.clientY ?? b.top, `${r.ticker} — ${r.name}`,
        [{label:"90-day revision", value:fmtPct(r.revision), color: pos?"var(--pos)":"var(--neg)"},
         {label:"fiscal year", value:r.fy0Ref.slice(2)},
         {label:"price, same window", value:r.priceReturn==null?"—":fmtPct(r.priceReturn)}]); };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("blur", hideTip);
    hit.addEventListener("click", () => { sel = r.ticker; sl.value = sel; drawAll(); });
    g.appendChild(hit); svg.appendChild(g);
  });
  host.appendChild(svg);
  table("tb1", ["Company", "Sector", "Fiscal year", "Revision %", "Price %"],
    top.map(r => [r.ticker, r.sector, r.fy0Ref.slice(2), fmtPct(r.revision),
      r.priceReturn==null?"—":fmtPct(r.priceReturn)]));
}

/* ---------- chart 2: scatter ---------- */
function chart2() {
  const host = document.getElementById("c2"); host.replaceChildren();
  const pts = R.filter(r => r.revision != null && r.priceReturn != null);
  const W = Math.max(560, host.clientWidth || 900), H = 420;
  const left = 54, right = 20, padT = 14, padB = 42;
  const xs = pts.map(p=>p.revision), ys = pts.map(p=>p.priceReturn);
  const pad = (a) => { const lo=Math.min(...a), hi=Math.max(...a), m=(hi-lo)*0.08||1;
    return [lo-m, hi+m]; };
  const [x0,x1] = pad(xs), [y0,y1] = pad(ys);
  const sx = v => left + (v-x0)/(x1-x0) * (W-left-right);
  const sy = v => H-padB - (v-y0)/(y1-y0) * (H-padB-padT);
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, width:W, height:H, role:"img",
    "aria-label":"90-day EPS revision against 90-day price return"});
  for (const t of niceTicks(y0, y1, 5)) { const y = sy(t);
    svg.appendChild(el("line", {x1:left, x2:W-right, y1:y, y2:y, class:"gridline"}));
    const lb = el("text", {x:left-9, y:y+4, class:"tick", "text-anchor":"end"});
    lb.textContent = t.toFixed(0)+"%"; svg.appendChild(lb); }
  for (const t of niceTicks(x0, x1, 6)) { const x = sx(t);
    svg.appendChild(el("line", {x1:x, x2:x, y1:padT, y2:H-padB, class:"gridline"}));
    const lb = el("text", {x, y:H-padB+17, class:"tick", "text-anchor":"middle"});
    lb.textContent = t.toFixed(0)+"%"; svg.appendChild(lb); }
  if (x0 < 0 && x1 > 0) svg.appendChild(el("line",
    {x1:sx(0), x2:sx(0), y1:padT, y2:H-padB, class:"axisline"}));
  if (y0 < 0 && y1 > 0) svg.appendChild(el("line",
    {x1:left, x2:W-right, y1:sy(0), y2:sy(0), class:"axisline"}));
  const ax = el("text", {x:(left+W-right)/2, y:H-8, class:"tick", "text-anchor":"middle"});
  ax.textContent = "90-day EPS revision"; svg.appendChild(ax);
  const ay = el("text", {x:14, y:(padT+H-padB)/2, class:"tick", "text-anchor":"middle",
    transform:`rotate(-90 14 ${(padT+H-padB)/2})`});
  ay.textContent = "90-day price return"; svg.appendChild(ay);

  for (const p of pts) {
    const isSel = p.ticker === sel;
    svg.appendChild(el("circle", {cx:sx(p.revision), cy:sy(p.priceReturn), r:isSel?6:4.5,
      fill: isSel ? "var(--s1)" : "var(--s1)", opacity: isSel ? 1 : 0.6,
      stroke:"var(--surface)", "stroke-width":2}));
  }
  const s = byTicker[sel];
  if (s && s.revision != null && s.priceReturn != null) {
    const lb = el("text", {x:sx(s.revision)+10, y:sy(s.priceReturn)-8, class:"dlabel",
      "font-weight":700}); lb.textContent = s.ticker; svg.appendChild(lb);
  }
  // nearest-point layer: the pointer only has to be closest, not dead-centre
  const overlay = el("rect", {x:left, y:padT, width:W-left-right, height:H-padB-padT,
    class:"hit"});
  overlay.addEventListener("pointermove", ev => {
    const b = svg.getBoundingClientRect(), k = W / b.width;
    const mx = (ev.clientX-b.left)*k, my = (ev.clientY-b.top)*k;
    let best=null, bd=Infinity;
    for (const p of pts) { const d = (sx(p.revision)-mx)**2 + (sy(p.priceReturn)-my)**2;
      if (d < bd) { bd = d; best = p; } }
    if (best && bd < 60**2) showTip(ev.clientX, ev.clientY, `${best.ticker} — ${best.name}`,
      [{label:"EPS revision", value:fmtPct(best.revision), color:"var(--s1)"},
       {label:"price return", value:fmtPct(best.priceReturn)}]);
    else hideTip();
  });
  overlay.addEventListener("pointerleave", hideTip);
  overlay.addEventListener("click", ev => {
    const b = svg.getBoundingClientRect(), k = W / b.width;
    const mx=(ev.clientX-b.left)*k, my=(ev.clientY-b.top)*k;
    let best=null,bd=Infinity;
    for (const p of pts) { const d=(sx(p.revision)-mx)**2+(sy(p.priceReturn)-my)**2;
      if (d<bd){bd=d;best=p;} }
    if (best && bd < 60**2) { sel = best.ticker; sl.value = sel; drawAll(); }
  });
  svg.appendChild(overlay);
  host.appendChild(svg);
  table("tb2", ["Company", "Sector", "EPS revision %", "Price return %"],
    [...pts].sort((a,b)=>b.revision-a.revision).map(p =>
      [p.ticker, p.sector, fmtPct(p.revision), fmtPct(p.priceReturn)]));
}

/* ---------- chart 3: indexed lines ---------- */
function chart3() {
  const host = document.getElementById("c3"); host.replaceChildren();
  const r = byTicker[sel];
  document.getElementById("h3").textContent = `${r.ticker} — price and consensus, indexed`;
  const base = { px: r.px[0], eps: r.fy0.length ? r.fy0[0].v : null };
  const series = [
    {key:"Price", color:"var(--s1)", pts: DATES.map((d,i) =>
      ({d, v: (r.px[i]!=null && base.px) ? r.px[i]/base.px*100 : null}))},
    {key:`EPS consensus ${r.fy0Ref.slice(2)}`, color:"var(--s2)", pts: DATES.map(d => {
      const m = r.fy0.find(p => p.d === d);
      return {d, v: (m && base.eps) ? m.v/base.eps*100 : null}; })},
  ];
  const lg = document.getElementById("lg3");
  lg.replaceChildren(...series.map(s => { const sp = document.createElement("span");
    const i = document.createElement("i"); i.className="key-line"; i.style.background=s.color;
    sp.append(i, document.createTextNode(s.key)); return sp; }));

  const W = Math.max(560, host.clientWidth || 900), H = 340;
  const left = 52, right = 96, padT = 16, padB = 40;
  const vals = series.flatMap(s => s.pts.map(p=>p.v)).filter(v=>v!=null);
  if (!vals.length) { host.appendChild(document.createTextNode("No data")); return; }
  let lo = Math.min(...vals, 100), hi = Math.max(...vals, 100);
  const m = (hi-lo)*0.14 || 4; lo -= m; hi += m;
  const sx = i => left + i/(DATES.length-1) * (W-left-right);
  const sy = v => H-padB - (v-lo)/(hi-lo) * (H-padB-padT);
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, width:W, height:H, role:"img",
    "aria-label":`${r.ticker} price and consensus indexed to 100`});
  const step = Math.max(1, Math.round((hi-lo)/5));
  for (let t = Math.ceil(lo/step)*step; t <= hi; t += step) {
    const y = sy(t);
    svg.appendChild(el("line",{x1:left,x2:W-right,y1:y,y2:y,
      class: t===100 ? "axisline" : "gridline"}));
    const lb = el("text",{x:left-9,y:y+4,class:"tick","text-anchor":"end"});
    lb.textContent = String(t); svg.appendChild(lb);
  }
  DATES.forEach((d,i) => { const lb = el("text",{x:sx(i),y:H-padB+17,class:"tick",
    "text-anchor":"middle"}); lb.textContent = shortDate(d); svg.appendChild(lb); });

  for (const s of series) {
    const pts = s.pts.map((p,i)=>({x:sx(i), y:p.v==null?null:sy(p.v), v:p.v}))
                     .filter(p=>p.y!=null);
    if (pts.length > 1) svg.appendChild(el("path",
      {d: pts.map((p,i)=>(i?"L":"M")+p.x+" "+p.y).join(" "), fill:"none",
       stroke:s.color, "stroke-width":2, "stroke-linejoin":"round", "stroke-linecap":"round"}));
    for (const p of pts) svg.appendChild(el("circle",
      {cx:p.x, cy:p.y, r:4, fill:s.color, stroke:"var(--surface)", "stroke-width":2}));
    const last = pts[pts.length-1];
    if (last) { const lb = el("text",{x:last.x+10, y:last.y+4, class:"dlabel"});
      lb.textContent = last.v.toFixed(1); svg.appendChild(lb); }
  }
  // crosshair: readers aim at a date, never at a 2px line
  const cross = el("line",{x1:0,x2:0,y1:padT,y2:H-padB,class:"gridline"});
  cross.style.visibility="hidden"; svg.appendChild(cross);
  const over = el("rect",{x:left-14,y:padT,width:W-left-right+28,height:H-padB-padT,class:"hit"});
  const at = (ev) => {
    const b = svg.getBoundingClientRect(), k = W/b.width;
    const mx = (ev.clientX-b.left)*k;
    let i = Math.round((mx-left)/((W-left-right)/(DATES.length-1)));
    i = Math.max(0, Math.min(DATES.length-1, i));
    cross.setAttribute("x1", sx(i)); cross.setAttribute("x2", sx(i));
    cross.style.visibility = "visible";
    showTip(ev.clientX, ev.clientY, DATES[i], series.map(s => ({
      label: s.key, color: s.color,
      value: s.pts[i].v == null ? "—" : s.pts[i].v.toFixed(1)})));
  };
  over.addEventListener("pointermove", at);
  over.addEventListener("pointerleave", () => { cross.style.visibility="hidden"; hideTip(); });
  svg.appendChild(over);
  host.appendChild(svg);

  document.getElementById("n3").textContent =
    `Indexed to 100 at ${DATES[0]}. Consensus is for the fiscal year ending `
    + `${r.fy0Ref.slice(2)}; the four backfilled points come from eps_trend and are `
    + `accurate to a few days, so read the shape rather than any single step.`;
  table("tb3", ["Date", "Price", "Price idx", `EPS ${r.fy0Ref.slice(2)}`, "EPS idx"],
    DATES.map((d,i) => { const e = r.fy0.find(p=>p.d===d);
      return [d, r.px[i]==null?"—":fmtNum(r.px[i]),
        series[0].pts[i].v==null?"—":series[0].pts[i].v.toFixed(1),
        e?fmtNum(e.v):"—", series[1].pts[i].v==null?"—":series[1].pts[i].v.toFixed(1)]; }));
}

/* ---------- table twins ---------- */
function table(id, head, rows) {
  const host = document.getElementById(id); host.replaceChildren();
  const t = document.createElement("table");
  const thead = document.createElement("thead"), tr = document.createElement("tr");
  for (const h of head) { const th = document.createElement("th"); th.textContent = h;
    tr.appendChild(th); }
  thead.appendChild(tr); t.appendChild(thead);
  const tb = document.createElement("tbody");
  for (const r of rows) { const row = document.createElement("tr");
    for (const c of r) { const td = document.createElement("td"); td.textContent = c;
      row.appendChild(td); } tb.appendChild(row); }
  t.appendChild(tb); host.appendChild(t);
}
for (const [b, p] of [["t1","tb1"],["t2","tb2"],["t3","tb3"]]) {
  document.getElementById(b).addEventListener("click", e => {
    const on = e.target.getAttribute("aria-pressed") === "true";
    e.target.setAttribute("aria-pressed", String(!on));
    document.getElementById(p).hidden = on;
  });
}

/* ---------- drilldown ---------- */
const METRIC_LABEL = {
  price:"Price", marketCap:"Market cap", forwardPE:"Forward P/E",
  trailingPE:"Trailing P/E", evEbitda:"EV/EBITDA", ps:"P/S", fcfYield:"FCF yield",
  revGrowth:"Revenue growth", epsGrowth:"EPS growth", grossMargin:"Gross margin",
  opMargin:"Operating margin", netMargin:"Net margin", roe:"ROE",
  netDebtEbitda:"Net debt / EBITDA", fcf:"Free cash flow", cash:"Cash",
  ret1m:"1-month return", ret6m:"6-month return", retYtd:"YTD return",
};
const MONEY = new Set(["marketCap","fcf","cash"]);
const PCTM  = new Set(["fcfYield","revGrowth","epsGrowth","grossMargin","opMargin",
                       "netMargin","roe","ret1m","ret6m","retYtd"]);
const bigUSD = v => { const a = Math.abs(v);
  if (a >= 1e12) return "$" + (v/1e12).toFixed(2) + "T";
  if (a >= 1e9)  return "$" + (v/1e9).toFixed(1) + "B";
  if (a >= 1e6)  return "$" + (v/1e6).toFixed(1) + "M";
  return "$" + fmtNum(v); };
const bigNum = v => { const a = Math.abs(v);
  if (a >= 1e9) return (v/1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v/1e6).toFixed(1) + "M";
  if (a >= 1e3) return (v/1e3).toFixed(1) + "K";
  return fmtNum(v); };
const ordinal = n => { const t = n % 100, u = n % 10;
  if (t >= 11 && t <= 13) return n + "th";
  return n + (u === 1 ? "st" : u === 2 ? "nd" : u === 3 ? "rd" : "th"); };
function metricValue(m, v) {
  if (MONEY.has(m)) return bigUSD(v);
  if (PCTM.has(m)) return v.toFixed(1) + "%";
  if (m === "price") return "$" + v.toFixed(2);
  return v.toFixed(2) + "x";
}

let dtab = "details";
const TABS = [["details","Company details"], ["hedge","Hedge funds (13F)"],
              ["holders","Top holdings"], ["changes","Holding changes"],
              ["estimates","Estimates"], ["insiders","Insider filings"]];

function drill() {
  const r = byTicker[sel];
  document.getElementById("dtitle").textContent = `${r.ticker} — ${r.name}`;
  document.getElementById("dsub").textContent =
    [r.sector, r.subindustry].filter(Boolean).join(" · ");

  const tabs = document.getElementById("dtabs");
  tabs.replaceChildren(...TABS.map(([k, lbl]) => {
    const b = document.createElement("button");
    b.textContent = lbl; b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(k === dtab));
    b.addEventListener("click", () => { dtab = k; drill(); });
    return b;
  }));

  const body = document.getElementById("dbody");
  const note = document.getElementById("dnote");
  body.replaceChildren(); note.textContent = "";

  if (dtab === "details") {
    const grid = document.createElement("div"); grid.className = "mgrid";
    for (const m of r.metrics) {
      const c = document.createElement("div"); c.className = "mcell";
      const k = document.createElement("div"); k.className = "mk";
      k.textContent = METRIC_LABEL[m.metric] || m.metric;
      const v = document.createElement("div"); v.className = "mv";
      v.textContent = metricValue(m.metric, m.value);
      c.append(k, v);
      if (m.pct != null) {
        const track = document.createElement("div"); track.className = "meter";
        const fill = document.createElement("i");
        fill.style.width = Math.round(m.pct * 100) + "%";
        track.appendChild(fill);
        const p = document.createElement("div"); p.className = "mp";
        p.textContent = `${ordinal(Math.round(m.pct*100))} percentile in ${r.subindustry||"peers"}`;
        c.append(track, p);
      }
      grid.appendChild(c);
    }
    body.appendChild(grid);
    note.textContent = "Percentile is the rank within the company's sub-industry on "
      + "the latest snapshot — the same peer set the dashboard scores against. "
      + "High is not always good: for P/E or net debt a low rank is the favourable end.";
    return;
  }

  if (dtab === "hedge") {
    const fv = DATA.fundview || {};
    if (!r.funds13f || !r.funds13f.length) {
      body.textContent = fv.quarter
        ? `None of the ${(fv.funds||[]).length} funds tracked reported a position in `
          + `${r.ticker} for ${fv.quarter}.`
        : "No 13F filings ingested yet.";
      return;
    }
    const held = r.funds13f.filter(f => f.shares > 0);
    const net = r.funds13f.reduce((a, f) => a + (f.deltaShares || 0), 0);

    const sum = document.createElement("div"); sum.className = "mgrid";
    const stat = (k, v, cls) => {
      const c = document.createElement("div"); c.className = "mcell";
      const kk = document.createElement("div"); kk.className = "mk"; kk.textContent = k;
      const vv = document.createElement("div"); vv.className = "mv " + (cls || "");
      vv.textContent = v; c.append(kk, vv); return c;
    };
    sum.append(
      stat("Funds holding", `${held.length} of ${(fv.funds||[]).length}`),
      stat("Combined value", bigUSD(held.reduce((a,f)=>a+(f.value||0),0))),
      stat("Net QoQ shares", (net >= 0 ? "+" : "") + bigNum(net),
           net > 0 ? "up" : net < 0 ? "dn" : ""),
      stat("Quarter", fv.quarter || "—"));
    body.appendChild(sum);

    const wrap = document.createElement("div"); wrap.className = "tblwrap";
    const t = document.createElement("table");
    const head = ["Fund", "Cohort", "Shares", "Value", "% of book",
                  "Δ Shares", "Δ %", ""];
    const thead = document.createElement("thead"), htr = document.createElement("tr");
    for (const h of head) { const th = document.createElement("th");
      th.textContent = h; htr.appendChild(th); }
    thead.appendChild(htr); t.appendChild(thead);
    const tb = document.createElement("tbody");
    for (const f of r.funds13f) {
      const tr = document.createElement("tr");
      if (f.action === "EXIT") tr.className = "exited";
      const tds = [
        [f.fund, ""],
        [f.cohort, "dim"],
        [f.shares > 0 ? bigNum(f.shares) : "—", "num"],
        [f.value > 0 ? bigUSD(f.value) : "—", "num"],
        [f.pctOfBook == null ? "—" : (f.pctOfBook*100).toFixed(2) + "%", "num"],
        [f.deltaShares == null ? "—"
          : (f.deltaShares >= 0 ? "+" : "") + bigNum(f.deltaShares),
         "num " + (f.deltaShares > 0 ? "up" : f.deltaShares < 0 ? "dn" : "")],
        [f.deltaPct == null ? "—" : fmtPct(f.deltaPct*100),
         "num " + (f.deltaPct > 0 ? "up" : f.deltaPct < 0 ? "dn" : "")],
        [null, ""],
      ];
      tds.forEach(([txt, cls], i) => {
        const td = document.createElement("td");
        if (i === 7) {
          if (f.action) { const b = document.createElement("span");
            b.className = "badge b" + f.action; b.textContent = f.action;
            td.appendChild(b); }
        } else { td.textContent = txt; td.className = cls; }
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    }
    t.appendChild(tb); wrap.appendChild(t); body.appendChild(wrap);
    note.textContent =
      "Parsed from each manager's own 13F-HR filing on SEC EDGAR, not from an "
      + "aggregate holder list — so a fund appears here at any position size. "
      + "Δ is against " + (fv.prevQuarter || "the prior quarter") + ". "
      + "“% of book” is the position against that manager's entire "
      + "reported equity book, which is the column that separates conviction "
      + "from flow: a top position at a concentrated fund runs several percent, "
      + "while a multi-strategy book of thousands of names rarely clears 0.5% "
      + "on anything. 13F covers long US equity only — no shorts, no swaps — "
      + "and is filed 45 days after quarter end, so it is a lagged picture.";
    return;
  }

  if (dtab === "holders" || dtab === "changes") {
    if (!r.holders.length) {
      body.textContent = "No holder data captured for this company yet.";
      return;
    }
    let rows = r.holders.slice();
    if (dtab === "changes") {
      rows = rows.filter(h => h.pctChange != null)
                 .sort((a,b) => Math.abs(b.pctChange) - Math.abs(a.pctChange));
    }
    const wrap = document.createElement("div"); wrap.className = "tblwrap";
    const t = document.createElement("table");
    const head = ["Holder", "Type", "Reported", "% held", "Shares", "Value", "QoQ change"];
    const thead = document.createElement("thead"), htr = document.createElement("tr");
    for (const h of head) { const th = document.createElement("th");
      th.textContent = h; htr.appendChild(th); }
    thead.appendChild(htr); t.appendChild(thead);
    const tb = document.createElement("tbody");
    for (const h of rows) {
      const tr = document.createElement("tr");
      const cells = [
        h.holder,
        h.kind === "institution" ? "13F" : "Fund",
        h.asOf,
        h.pctHeld == null ? "—" : (h.pctHeld*100).toFixed(2) + "%",
        h.shares == null ? "—" : bigNum(h.shares),
        h.value == null ? "—" : bigUSD(h.value),
        null,
      ];
      cells.forEach((c, i) => {
        const td = document.createElement("td");
        if (i === 6) {
          if (h.pctChange == null) td.textContent = "—";
          else { td.textContent = fmtPct(h.pctChange*100);
                 td.className = h.pctChange >= 0 ? "up" : "dn"; }
        } else td.textContent = c;
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    }
    t.appendChild(tb); wrap.appendChild(t); body.appendChild(wrap);
    const dates = [...new Set(r.holders.map(h => h.asOf))].sort();
    note.textContent =
      "These are the ten largest institutional holders and the ten largest fund "
      + "holders that the data source exposes — not the whole 13F universe, which "
      + "runs to thousands of filers. A fund that tripled a small position will not "
      + "appear here, so read this as changes among the largest holders rather than "
      + "the largest changes. Report dates in this list: " + dates.join(", ")
      + " — the source mixes quarters, so each row is dated by its own filing.";
    return;
  }

  if (dtab === "estimates") {
    if (!r.estimates.length) { body.textContent = "No estimates captured."; return; }
    const refs = [...new Set(r.estimates.map(e => e.ref))].sort((a,b) =>
      (a[1] === b[1] ? 0 : a[1] === "Q" ? -1 : 1) || (a < b ? -1 : 1));
    const byRef = {};
    for (const e of r.estimates) (byRef[e.ref] ||= {})[e.metric] = e.value;
    const spec = [
      ["epsEstAvg", "EPS consensus", v => v.toFixed(2)],
      ["epsEstLow", "EPS low", v => v.toFixed(2)],
      ["epsEstHigh", "EPS high", v => v.toFixed(2)],
      ["epsEstAnalysts", "EPS analysts", v => String(v)],
      ["epsEstGrowth", "EPS growth", v => fmtPct(v*100)],
      ["revEstAvg", "Revenue consensus", bigUSD],
      ["revEstGrowth", "Revenue growth", v => fmtPct(v*100)],
      ["revEstAnalysts", "Revenue analysts", v => String(v)],
      ["epsRevUp7", "Raised, last 7d", v => String(v)],
      ["epsRevDown7", "Cut, last 7d", v => String(v)],
      ["epsRevUp30", "Raised, last 30d", v => String(v)],
      ["epsRevDown30", "Cut, last 30d", v => String(v)],
    ];
    const wrap = document.createElement("div"); wrap.className = "tblwrap";
    const t = document.createElement("table");
    const thead = document.createElement("thead"), htr = document.createElement("tr");
    for (const h of ["", ...refs]) { const th = document.createElement("th");
      th.textContent = h; htr.appendChild(th); }
    thead.appendChild(htr); t.appendChild(thead);
    const tb = document.createElement("tbody");
    for (const [key, label, fmt] of spec) {
      if (!refs.some(rf => byRef[rf] && byRef[rf][key] != null)) continue;
      const tr = document.createElement("tr");
      const th = document.createElement("td"); th.textContent = label; tr.appendChild(th);
      for (const rf of refs) {
        const td = document.createElement("td");
        const v = byRef[rf] ? byRef[rf][key] : null;
        td.textContent = v == null ? "—" : fmt(v);
        tr.appendChild(td);
      }
      tb.appendChild(tr);
    }
    t.appendChild(tb); wrap.appendChild(t); body.appendChild(wrap);
    note.textContent = "FQ is a fiscal quarter, FY a fiscal year, each labelled by "
      + "the period it ends. Both are captured because a company's fiscal year and "
      + "one of its quarters can end on the same day.";
    return;
  }

  if (dtab === "insiders") {
    if (!r.insiders.length) {
      body.textContent = "No insider filings captured for this company.";
      return;
    }
    const wrap = document.createElement("div"); wrap.className = "tblwrap";
    const t = document.createElement("table");
    const thead = document.createElement("thead"), htr = document.createElement("tr");
    for (const h of ["Date","Insider","Position","Transaction","Shares","Held"]) {
      const th = document.createElement("th"); th.textContent = h; htr.appendChild(th); }
    thead.appendChild(htr); t.appendChild(thead);
    const tb = document.createElement("tbody");
    for (const x of r.insiders) {
      const tr = document.createElement("tr");
      for (const c of [x.asOf, x.insider, x.position, x.txn,
                       x.shares == null ? "—" : bigNum(x.shares),
                       x.ownership === "D" ? "Direct" : x.ownership === "I" ? "Indirect" : x.ownership]) {
        const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
      tb.appendChild(tr);
    }
    t.appendChild(tb); wrap.appendChild(t); body.appendChild(wrap);
    note.textContent = `The ${r.insiders.length} most recent filings captured. `
      + "Grants and gifts are not open-market buying — read the transaction column, "
      + "not just the share count.";
  }
}

let fsel = null;

function fundView() {
  const fv = DATA.fundview || {};
  const funds = fv.funds || [];
  const card = document.getElementById("fundcard");
  if (!funds.length) { card.style.display = "none"; return; }
  if (fsel === null) fsel = funds[0].fund;
  const f = funds.find(x => x.fund === fsel) || funds[0];

  const sel2 = document.getElementById("fsel");
  if (!sel2.options.length) {
    for (const x of funds) {
      const o = document.createElement("option");
      o.value = x.fund;
      o.textContent = `${x.fund} — ${x.nUniverse} of ${R.length}`;
      sel2.appendChild(o);
    }
    sel2.addEventListener("change", () => { fsel = sel2.value; fundView(); });
  }
  sel2.value = f.fund;

  document.getElementById("fdesc").textContent =
    `${f.cohort} · ${fv.quarter} · filed ${bigNum(f.nFiled)} equity positions in total`;

  const kpis = document.getElementById("fkpis");
  kpis.replaceChildren();
  const held = f.positions.filter(p => p.shares > 0);
  const adds = held.filter(p => p.action === "ADD" || p.action === "NEW").length;
  const cuts = f.positions.filter(p => p.action === "TRIM" || p.action === "EXIT").length;
  for (const [k, v] of [
    ["Names held here", `${f.nUniverse}`],
    ["Value in these names", bigUSD(f.universeValue)],
    ["Whole reported book", bigUSD(f.bookValue)],
    ["Added / trimmed", `${adds} / ${cuts}`],
  ]) {
    const c = document.createElement("div"); c.className = "kpi";
    const kk = document.createElement("div"); kk.className = "k"; kk.textContent = k;
    const vv = document.createElement("div"); vv.className = "v"; vv.textContent = v;
    c.append(kk, vv); kpis.appendChild(c);
  }

  const body = document.getElementById("fbody");
  body.replaceChildren();
  const wrap = document.createElement("div"); wrap.className = "tblwrap";
  const t = document.createElement("table");
  const thead = document.createElement("thead"), htr = document.createElement("tr");
  for (const h of ["Ticker", "Name", "Shares", "Value", "% of book",
                   "Δ Shares", "Δ %", ""]) {
    const th = document.createElement("th"); th.textContent = h; htr.appendChild(th);
  }
  thead.appendChild(htr); t.appendChild(thead);
  const tb = document.createElement("tbody");
  for (const p of f.positions) {
    const tr = document.createElement("tr");
    if (p.action === "EXIT") tr.className = "exited";
    const rec = byTicker[p.ticker];
    const cells = [
      [p.ticker, "lnk"],
      [rec ? rec.name : "", "dim"],
      [p.shares > 0 ? bigNum(p.shares) : "—", "num"],
      [p.value > 0 ? bigUSD(p.value) : "—", "num"],
      [p.pctOfBook == null ? "—" : (p.pctOfBook * 100).toFixed(2) + "%", "num"],
      [p.deltaShares == null ? "—"
        : (p.deltaShares >= 0 ? "+" : "") + bigNum(p.deltaShares),
       "num " + (p.deltaShares > 0 ? "up" : p.deltaShares < 0 ? "dn" : "")],
      [p.deltaPct == null ? "—" : fmtPct(p.deltaPct * 100),
       "num " + (p.deltaPct > 0 ? "up" : p.deltaPct < 0 ? "dn" : "")],
      [null, ""],
    ];
    cells.forEach(([txt, cls], i) => {
      const td = document.createElement("td");
      if (i === 7) {
        if (p.action) { const b = document.createElement("span");
          b.className = "badge b" + p.action; b.textContent = p.action;
          td.appendChild(b); }
      } else if (i === 0 && rec) {
        const a = document.createElement("a");
        a.href = "#"; a.textContent = txt;
        a.addEventListener("click", ev => {
          ev.preventDefault(); sel = p.ticker; sl.value = sel;
          dtab = "hedge"; drawAll();
          document.getElementById("drill").scrollIntoView({behavior: "smooth"});
        });
        td.appendChild(a);
      } else { td.textContent = txt; td.className = cls; }
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  }
  t.appendChild(tb); wrap.appendChild(t); body.appendChild(wrap);

  document.getElementById("fnote").textContent =
    `${f.fund}'s positions in the ${R.length} names this page tracks, for `
    + `${fv.quarter}, against ${fv.prevQuarter || "the prior quarter"}. `
    + `The whole reported book is ${bigUSD(f.bookValue)} across `
    + `${bigNum(f.nFiled)} equity lines, so what is shown here is the overlap `
    + `with this universe rather than the fund. Share counts are adjusted for `
    + `splits before differencing, so a 10-for-1 does not read as a position `
    + `ten times larger. 13F is long US equity only, filed 45 days after `
    + `quarter end.`;
}

function drawAll() { chart1(); chart2(); chart3(); drill(); fundView(); }
drawAll();
let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawAll, 150); });
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="history.html")
    ap.add_argument("--db", default=str(history.DB_PATH))
    args = ap.parse_args()

    conn = history.connect(args.db)
    try:
        payload = load(conn)
    finally:
        conn.close()

    out = ROOT / args.out
    out.write_text(render(payload), encoding="utf-8")
    n = len(payload["records"])
    print(f"Wrote {out}  ({n} companies, {payload['totals']['rows']:,} observations)")


if __name__ == "__main__":
    main()
