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


def load(conn) -> dict:
    """Assemble the payload: one record per ticker, plus store-level totals."""
    companies = pd.read_sql_query(
        "SELECT ticker, name, sector FROM companies", conn
    ).set_index("ticker")

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
            "palette": PALETTE}


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

function drawAll() { chart1(); chart2(); chart3(); }
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
