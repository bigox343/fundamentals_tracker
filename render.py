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

import pandas as pd


def fmt(val, kind: str) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "<span class='na'>–</span>"
    try:
        if kind == "usd":
            return f"${val:,.2f}"
        if kind == "bigusd":
            a = abs(val)
            if a >= 1e12:
                return f"${val/1e12:.2f}T"
            if a >= 1e9:
                return f"${val/1e9:.1f}B"
            if a >= 1e6:
                return f"${val/1e6:.0f}M"
            return f"${val:,.0f}"
        if kind == "x":
            return f"{val:.1f}x"
        if kind == "pct":
            return f"{val:.1f}%"
    except (ValueError, TypeError):
        return "<span class='na'>–</span>"
    return str(val)


def parse_spark(raw) -> list[float]:
    if not isinstance(raw, str) or not raw.strip():
        return []
    out = []
    for tok in raw.split(";"):
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def sparkline(raw, ret: float | None, label: str = "", w: int = 62, h: int = 17) -> str:
    """Inline SVG 6M price sparkline: line + first-value baseline + end dot.

    Direction rides on the blue/red diverging pair the heatmap already uses, but
    color is redundant here — the line's shape and the signed % beside it both
    carry it, so the mark still reads under CVD, print, and forced-colors.
    """
    pts = parse_spark(raw)
    if len(pts) < 3:
        return "<span class='na'>–</span>"
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    pad = 2.0                       # keeps the 2px end dot inside the viewBox
    inner = h - 2 * pad

    def y(v):
        return pad + inner * (1 - (v - lo) / span)

    dx = w / (len(pts) - 1)
    coords = " ".join(f"{i*dx:.1f},{y(v):.1f}" for i, v in enumerate(pts))
    # direction comes from the drawn endpoints, not the quoted return: for a name
    # too young to have a 6M figure the line is still the honest answer
    cls = "up" if pts[-1] >= pts[0] else "dn"
    # a None return means the series is younger than the window, so the tooltip
    # must not claim six months of it
    tip = (f"{label + ' — ' if label else ''}"
           f"{'6M' if ret is not None else 'available history'}: "
           f"{pts[0]:,.2f} → {pts[-1]:,.2f}"
           f"{f' ({ret:+.1f}%)' if ret is not None else ''} · "
           f"low {lo:,.2f} / high {hi:,.2f}")
    return (
        f"<svg class='spk {cls}' viewBox='0 0 {w} {h}' width='{w}' height='{h}' "
        f"role='img' aria-label=\"{tip}\"><title>{tip}</title>"
        f"<line class='base' x1='0' y1='{y(pts[0]):.1f}' x2='{w}' y2='{y(pts[0]):.1f}'/>"
        f"<polyline points='{coords}'/>"
        f"<circle cx='{w - 0.6:.1f}' cy='{y(pts[-1]):.1f}' r='2'/></svg>"
    )


def spark_cell(row) -> str:
    """Stacked sparkline + signed 6M return, sized to fit the existing row height."""
    ret = row.get("ret6m")
    if isinstance(ret, float) and math.isnan(ret):
        ret = None
    svg = sparkline(row.get("spark"), ret, str(row.get("ticker", "")))
    if svg.startswith("<span"):          # no usable series — one dash, not two
        return svg
    val = f"{ret:+.1f}%" if ret is not None else "<span class='na'>–</span>"
    return f"<span class='spk-wrap'>{svg}<span class='spk-val'>{val}</span></span>"


# Weekly is plenty for a five-year range chart and costs a fifth of the
# bytes. Percentiles are computed from the *daily* series server-side (see
# valuation.own_percentile) and travel to the page as data-oh -- downsampling
# here only ever affects the picture, never a number the reader is shown.
# The click handler must read data-oh rather than re-deriving a percentile
# from these weekly points; see the JS_TMPL comment beside drawDrill.
SERIES_EVERY = 5


def _round4sig(v: float) -> float:
    """Round to four *significant figures*, not four decimal places.

    round(1.23456789, 4) is 1.2346 -- four decimal places, a different
    quantity once a multiple's value clears 1. A P/S of 1.23456789 should
    read as 1.235. Guards against the naive round(v, 4) that satisfies this
    module's docstring but not its own test.
    """
    if v == 0:
        return 0.0
    return round(v, 3 - int(math.floor(math.log10(abs(v)))))


def encode_series(frame: pd.DataFrame, every: int = SERIES_EVERY) -> dict:
    """One ticker's multiple history, compactly, for the drilldown chart.

    Only shapes the payload -- which (ticker, metric) pairs are worth
    encoding at all is build_dashboard.own_history's call, since that is
    where the proof-pass and MIN_HISTORY gates already live. A column with
    no data survives dropna().empty rather than embedding an empty chart.
    """
    out = {}
    for column in frame.columns:
        s = frame[column].iloc[::every]
        if s.dropna().empty:
            continue
        out[column] = {
            "t0": s.index[0].strftime("%Y-%m-%d"),
            "step": every,
            "v": [None if pd.isna(v) else _round4sig(float(v)) for v in s],
        }
    return out


# How large a move fills the tint, per change kind and window. Measured on the
# live store rather than chosen: these are the p90 of the absolute change, so
# roughly the top decile of real moves saturates and the rest stay legible
# against each other.
#
#   log ratio (multiples, from the daily series)   c1w p90 0.090   c1m p90 0.177
#   percentage points (snapshot fundamentals)      c1w p90 3.30    c1m no depth
#
# The two windows need different spans because a month moves about twice as far
# as a week; one shared constant would leave weekly moves almost untinted. The
# points figures are conditional on a move having happened at all -- only 19%
# of snapshot fundamentals change in a given week, because they are quarterly.
CHANGE_SATURATION = {
    ("log", "c1w"): 0.09, ("log", "c1m"): 0.18,
    ("points", "c1w"): 3.0, ("points", "c1m"): 6.0,
}


def cell_style(score: float | None) -> str:
    """Diverging blue<->red tint. score in [-1,1]; +1 favorable (blue), -1 red."""
    if score is None:
        return ""
    a = min(0.34, abs(score) * 0.34)
    rgb = "37,106,191" if score > 0 else "208,59,59"  # seq-blue-500 / status-critical
    return f" style=\"background:rgba({rgb},{a:.3f})\""


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


def band_perf(band: str, proxies: dict) -> str:
    """Benchmark badge + sparkline + returns, inlined into a sub-industry header.

    Sits immediately after the band name rather than floated right: the header
    cell spans the whole (scrollable) table, so anything right-aligned would
    park off-screen until the reader scrolled.
    """
    p = proxies.get(band) or {}
    if not p:
        return ""
    name = p.get("proxy")
    badge = (f"<span class='pxy'>{name}</span>" if name
             else "<span class='pxy peers'>peer med</span>")
    spk = sparkline(p.get("spark"), p.get("ret6m"), name or band, w=52, h=15) if name else ""
    if spk.startswith("<span"):      # no usable series -> drop the mark, keep numbers
        spk = ""
    nums = ""
    for lab, key in (("6M", "ret6m"), ("1M", "ret1m"), ("YTD", "retYtd")):
        v = p.get(key)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        nums += f"<span class='pv'>{v:+.1f}%<span class='pw'>{lab}</span></span>"
    if not nums and not spk:
        return ""
    return f"<span class='bandperf'>{badge}{spk}{nums}</span>"


def render_sector(sector: str, df: pd.DataFrame, proxies: dict,
                   metrics: list, universe: dict, group_labels: dict,
                   domains: dict, own: dict | None = None,
                   changes: dict | None = None) -> str:
    gcount = {}
    for _k, _l, g, _f, _hb in metrics:
        gcount[g] = gcount.get(g, 0) + 1

    # group header
    ghead = "<tr class='ghead'><th class='sticky'></th>"
    seen = []
    for _k, _l, g, _f, _hb in metrics:
        if g not in seen:
            seen.append(g)
            lbl = group_labels[g]
            cls = f" class='grp g-{g}'" if lbl else ""
            ghead += f"<th colspan='{gcount[g]}'{cls}>{lbl}</th>"
    ghead += "</tr>"

    colhead = ("<tr class='colhead'><th class='sticky left sortable' data-key='__name'>"
               "Company<span class='ind'></span></th>")
    for key, lab, g, _f, _hb in metrics:
        colhead += f"<th class='g-{g} sortable' data-key='{key}'>{lab}<span class='ind'></span></th>"
    colhead += "</tr>"

    # ticker-indexed so sub-industry and sector scores can be looked up side by
    # side; the JS toggle swaps which basis paints the cell
    tdf = df.set_index("ticker", drop=False)
    sec_scores = {
        key: relative_scores(tdf, key, hb, domains.get(key))
        for key, _l, _g, _f, hb in metrics if hb is not None
    }

    ncols = len(metrics) + 1
    body = ""
    # one band per sub-industry; peer scoring happens *within* the band so a
    # semi is never tinted against a telco
    for sub in universe[sector]:
        sdf = tdf[tdf.subindustry == sub]
        if sdf.empty:
            continue
        sdf = sdf.sort_values("marketCap", ascending=False, na_position="last")
        sub_scores = {
            key: relative_scores(sdf, key, hb, domains.get(key))
            for key, _l, _g, _f, hb in metrics if hb is not None
        }
        # evaluated per-band, not per-cell, so it lines up with sub_scores'
        # own frame (sdf) rather than the whole sector
        sub_domain = {
            key: domains[key](sdf) for key, _l, _g, _f, _hb in metrics
            if key in domains
        }
        thin = " thin" if len(sdf) < 3 else ""
        note = " <span class='thin-note'>(thin peer set — no tint)</span>" if thin else ""
        body += (f"<tr class='subhead{thin}' data-sub=\"{sub}\">"
                 f"<td class='sticky left' colspan='{ncols}'>"
                 f"<span class='caret'>▾</span>{sub} "
                 f"<span class='cnt'>{len(sdf)}</span>{note}"
                 f"{band_perf(sub, proxies)}</td></tr>")
        for tk, r in sdf.iterrows():
            body += f"<tr data-sub=\"{sub}\" data-tk=\"{tk}\">"
            body += (f"<td class='sticky left name'><span class='tk'>{r['ticker']}</span>"
                     f"<span class='nm'>{r.get('name','')}</span></td>")
            for key, _lab, g, f, _hb in metrics:
                # the sparkline is drawn from the price series but sorts and
                # exports on its 6M return, so data-v carries the number
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
                # A pair the proof gate rejected gets no percentile here, so
                # the own-history frame renders blank for it rather than a
                # number nobody has verified -- see prove_xbrl.report().
                oh = (own or {}).get((tk, key))
                if oh is not None:
                    attrs += f" data-oh='{oh:.4f}'"
                # The raw change drives the arrow; the scaled one drives the
                # tint. Signed so a falling multiple reads favorable on a
                # lower-is-better metric, matching the peer frame's rule that
                # blue means good rather than merely up.
                for window in ("c1w", "c1m"):
                    delta = (changes or {}).get((tk, key, window))
                    if delta is None:
                        continue
                    sign = -1.0 if _hb is False else 1.0
                    span = CHANGE_SATURATION[
                        ("points" if f == "pct" else "log", window)]
                    scaled = max(-1.0, min(1.0, sign * delta / span))
                    attrs += (f" data-{window}='{delta:.5f}'"
                              f" data-{window}s='{scaled:.4f}'")
                # A missing score is ambiguous on its own: test_domains.py's
                # 3-name band (-5.0, -3.0, 12.0) excludes the two negatives,
                # leaving one valid value under relative_scores' floor of
                # three, so *all three* come back with ss=None -- including
                # 12.0, which is perfectly well-defined. Checking the domain
                # predicate on this row directly (sub_domain), rather than
                # inferring "out of domain" from the absent score, is what
                # keeps 12.0 unmarked while -5.0 and -3.0 still get flagged.
                cls = f"num g-{g}"
                dom_row = sub_domain.get(key)
                if (ss is None and dom_row is not None
                        and isinstance(v, (int, float))
                        and not (isinstance(v, float) and math.isnan(v))
                        and not bool(dom_row.get(tk, True))):
                    cls += " undef"
                    attrs += (" title=\"Denominator is zero or negative — "
                              "this ratio is undefined and is excluded from "
                              "peer scoring\"")
                cell = spark_cell(r) if f == "spark" else fmt(v, f)
                body += f"<td class='{cls}'{attrs}{cell_style(ss)}>{cell}</td>"
            body += "</tr>"

    return f"""
    <section class="sector">
      <h2>{sector}</h2>
      <div class="scroll">
        <table>
          <thead>{ghead}{colhead}</thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    </section>"""


def render_spx(spx: dict, df: pd.DataFrame, proxies: dict, universe: dict) -> str:
    def tile(label, val, kind="raw", delta=False):
        """One stat tile, or nothing at all when the source has no value.

        Yahoo publishes no forwardPE for ETFs or indices (SPY, IVV, VOO, SPLG
        and ^GSPC all return None), so the Fwd P/E tile could never populate and
        sat permanently blank. A tile is an independent grid item, so dropping
        it just reflows the row — unlike a table cell, where a dash has to stay
        to hold the column. Table cells keep rendering "–" for that reason.
        """
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return ""
        if kind == "level":
            disp = f"{val:,.0f}"
            cls = ""
        elif kind == "pct":
            disp = f"{val:+.1f}%" if delta else f"{val:.1f}%"
            cls = ("up" if val >= 0 else "down") if delta else ""
        elif kind == "x":
            disp = f"{val:.1f}x"
            cls = ""
        else:
            disp = f"{val}"
            cls = ""
        return (f"<div class='tile'><div class='tl'>{label}</div>"
                f"<div class='tv {cls}'>{disp}</div></div>")

    tiles = "".join([
        tile("S&P 500", spx.get("level"), "level"),
        tile("1-Day", spx.get("ret_1d"), "pct", delta=True),
        tile("1-Month", spx.get("ret_1m"), "pct", delta=True),
        tile("YTD", spx.get("ret_ytd"), "pct", delta=True),
        tile("1-Year", spx.get("ret_1y"), "pct", delta=True),
        tile("Fwd P/E", spx.get("fwd_pe"), "x"),
        tile("Trail P/E", spx.get("trail_pe"), "x"),
    ])
    # every tile suppressed means the whole ^GSPC/SPY pull failed, not that one
    # field is unpublished — say so rather than leaving an empty grid
    tiles_html = (f"<div class='tiles'>{tiles}</div>" if tiles else
                  "<div class='nores'>Index snapshot unavailable — the Yahoo pull "
                  "returned no data for ^GSPC or SPY on this run.</div>")

    # bottom-up medians, sub-industry level (sector total shown as a band row)
    def med_cells(sdf):
        def med(k):
            return pd.to_numeric(sdf[k], errors="coerce").median()
        return (
            f"<td class='num'>{fmt(med('forwardPE'),'x')}</td>"
            f"<td class='num'>{fmt(med('evEbitda'),'x')}</td>"
            f"<td class='num'>{fmt(med('revGrowth'),'pct')}</td>"
            f"<td class='num'>{fmt(med('grossMargin'),'pct')}</td>"
            f"<td class='num'>{fmt(med('netMargin'),'pct')}</td>"
            f"<td class='num'>{fmt(med('roe'),'pct')}</td>"
        )

    # benchmark return block: proxy ETF where one exists, peer median otherwise
    def perf_cells(band):
        p = proxies.get(band) or {}
        name = p.get("proxy")
        badge = (f"<span class='pxy'>{name}</span>" if name
                 else ("<span class='pxy peers'>peer med</span>" if p
                       else "<span class='na'>–</span>"))
        out = f"<td class='num pxy-td'>{badge}</td>"
        for key in ("ret1m", "ret6m", "retYtd"):
            v = p.get(key)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                out += "<td class='num'><span class='na'>–</span></td>"
            else:
                out += f"<td class='num'>{v:+.1f}%</td>"
        return out

    rows = ""
    for sector in universe:
        secdf = df[df.sector == sector]
        if secdf.empty:
            continue
        rows += (f"<tr class='secrow'><td class='left'>{sector}</td>"
                 f"{med_cells(secdf)}{perf_cells(sector)}</tr>")
        for sub in universe[sector]:
            sdf = secdf[secdf.subindustry == sub]
            if sdf.empty:
                continue
            rows += (f"<tr><td class='left sub-td'>{sub} "
                     f"<span class='cnt'>{len(sdf)}</span></td>"
                     f"{med_cells(sdf)}{perf_cells(sub)}</tr>")

    return f"""
    <section class="spx">
      <h2>S&amp;P 500 — Index Snapshot</h2>
      {tiles_html}
      <h3>Bottom-up medians &amp; benchmark returns by sub-industry
        <span class="sub">(medians from tracked names; returns from the proxy ETF)</span></h3>
      <div class="scroll">
        <table class="medians">
          <thead>
          <tr class='ghead'><th></th><th colspan='6' class='grp g-med'>Medians — tracked names</th>
          <th colspan='4' class='grp g-perf'>Benchmark price return</th></tr>
          <tr><th class='left'>Sector / Sub-industry</th><th class='num'>Fwd P/E</th>
          <th class='num'>EV/EBITDA</th><th class='num'>Rev Gr</th>
          <th class='num'>Gross %</th>
          <th class='num'>Net %</th><th class='num'>ROE</th>
          <th class='num'>Proxy</th><th class='num'>1M</th>
          <th class='num'>6M</th><th class='num'>YTD</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div class='legend'><span class='na'>Proxy ETFs stand in for each band
      (SMH semis, IGV software, ITA aero/defense, XRT retail …). Bands with no clean
      single-ETF proxy show <b>peer med</b> — the median return of the tracked names
      in that band. Returns are price/adjusted-close, not total return.</span></div>
    </section>"""


CSS = """
:root{color-scheme:light dark;
  --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
  --grid:#e1e0d9;--line:#c3c2b7;--up:#006300;--down:#c0392b;--ring:rgba(11,11,11,.10);
  --val:#256abf;--grow:#1baf7a;--prof:#4a3aa7;--bal:#eb6834;
  /* sparkline direction — the heatmap's diverging pair, not up/down green:
     green-vs-red measures ΔE 2.9 under protanopia (target 8), blue-vs-red 20.4 */
  --spk-up:#256abf;--spk-dn:#d03b3b;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
  --grid:#2c2c2a;--line:#383835;--up:#0ca30c;--down:#e66767;--ring:rgba(255,255,255,.10);
  --spk-up:#4a8fe0;--spk-dn:#de5f5f;}}
:root[data-theme="dark"]{
  --page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
  --grid:#2c2c2a;--line:#383835;--up:#0ca30c;--down:#e66767;--ring:rgba(255,255,255,.10);
  --spk-up:#4a8fe0;--spk-dn:#de5f5f;}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1280px;margin:0 auto;padding:32px 24px 64px;}
header{border-bottom:2px solid var(--line);padding-bottom:16px;margin-bottom:24px;}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em;}
.asof{color:var(--muted);font-size:13px;}
h2{font-size:18px;margin:36px 0 12px;letter-spacing:-.01em;}
h3{font-size:14px;margin:22px 0 8px;color:var(--ink2);font-weight:600;}
h3 .sub{color:var(--muted);font-weight:400;}
section{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
  padding:18px 18px 8px;margin-bottom:20px;}
section h2{margin-top:4px;}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table{border-collapse:separate;border-spacing:0;width:100%;font-variant-numeric:tabular-nums;}
th,td{padding:7px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--grid);}
.num{text-align:right;}.left{text-align:left;}
thead .ghead th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--surface);text-align:center;border-bottom:2px solid var(--surface);
  padding:4px 10px;border-radius:6px 6px 0 0;}
.ghead .grp.g-val{background:var(--val)}.ghead .grp.g-grow{background:var(--grow)}
.ghead .grp.g-prof{background:var(--prof)}.ghead .grp.g-bal{background:var(--bal)}
.ghead .grp.g-med{background:var(--ink2)}.ghead .grp.g-perf{background:var(--val)}
/* offset by the sticky toolbar's real height (JS sets --tbh) so column
   headers park below it instead of sliding underneath */
.colhead th{font-size:11px;color:var(--ink2);font-weight:600;border-bottom:2px solid var(--line);
  position:sticky;top:var(--tbh,52px);background:var(--surface);}
tbody tr:hover td{background:rgba(127,127,127,.06);}
tr.subhead td{background:var(--page);color:var(--ink);font-weight:700;font-size:12px;
  letter-spacing:.03em;text-align:left;padding:10px 10px 8px;
  border-top:2px solid var(--line);border-bottom:1px solid var(--line);}
tbody tr.subhead:hover td{background:var(--page);}
tr.subhead .cnt{color:var(--muted);font-weight:500;margin-left:6px;}
tr.subhead .thin-note{color:var(--muted);font-weight:400;font-size:11px;letter-spacing:0;}
.medians tr.secrow td{font-weight:700;border-top:2px solid var(--line);}
.medians td.sub-td{padding-left:24px;color:var(--ink2);}
.medians .cnt{color:var(--muted);font-size:11px;}
.sticky{position:sticky;left:0;background:var(--surface);z-index:2;}
th.sticky{z-index:3;}
td.name{text-align:left;min-width:150px;}
.tk{font-weight:700;display:block;}
.nm{color:var(--muted);font-size:11px;display:block;overflow:hidden;text-overflow:ellipsis;max-width:150px;}
.na{color:var(--muted);}
td.undef{color:var(--muted);font-style:italic;opacity:.65;cursor:help;}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:12px 0 8px;}
.tile{background:var(--page);border:1px solid var(--ring);border-radius:10px;padding:12px 14px;}
.tl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}
.tv{font-size:22px;font-weight:650;margin-top:4px;letter-spacing:-.02em;}
.tv.up{color:var(--up)}.tv.down{color:var(--down)}
.medians td.left,.medians th.left{font-weight:500;}
.legend{display:flex;gap:16px;align-items:center;color:var(--ink2);font-size:12px;margin:8px 2px 16px;flex-wrap:wrap;}
.chip{display:inline-block;width:26px;height:12px;border-radius:3px;vertical-align:middle;margin-right:5px;}
footer{color:var(--muted);font-size:12px;margin-top:32px;border-top:1px solid var(--grid);padding-top:14px;}
footer a{color:var(--ink2);}

/* ---- sparklines ---- */
/* stacked so the cell keeps the two-line rhythm the Company cell already sets */
.spk-wrap{display:inline-flex;flex-direction:column;align-items:flex-end;gap:1px;
  line-height:1;vertical-align:middle;}
.spk{display:block;overflow:visible;}
/* fill stays off the polyline — a filled sparkline reads as an area chart and
   exaggerates a window that does not start at zero */
.spk polyline{fill:none;stroke-width:1.4;stroke-linejoin:round;stroke-linecap:round;}
.spk circle{stroke:none;}
.spk.up polyline{stroke:var(--spk-up);}.spk.up circle{fill:var(--spk-up);}
.spk.dn polyline{stroke:var(--spk-dn);}.spk.dn circle{fill:var(--spk-dn);}
/* start-of-window reference, deliberately recessive — it frames the line, it is
   not a series of its own */
.spk .base{stroke:var(--muted);stroke-width:1;stroke-dasharray:2 3;opacity:.45;}
/* the value stays in ink, never the mark's hue: the sparkline beside it is what
   carries direction, and the sign carries it again for CVD/print */
.spk-val{font-size:10px;color:var(--ink2);letter-spacing:-.01em;}
td.num:has(.spk-wrap){padding-top:5px;padding-bottom:5px;}
@media (forced-colors:active){.spk polyline{stroke:CanvasText;}.spk circle{fill:CanvasText;}}

/* ---- sub-industry benchmark strip ---- */
.bandperf{display:inline-flex;align-items:center;gap:7px;margin-left:12px;
  vertical-align:middle;font-weight:500;}
.pxy{font-size:10px;font-weight:700;letter-spacing:.04em;color:var(--val);
  border:1px solid var(--val);border-radius:4px;padding:1px 5px;}
.pxy.peers{color:var(--muted);border-color:var(--line);font-weight:600;
  letter-spacing:.02em;text-transform:none;}
.bandperf .spk{vertical-align:middle;}
.pv{font-size:11px;color:var(--ink2);font-weight:600;}
.pw{font-size:9px;color:var(--muted);margin-left:2px;letter-spacing:.04em;font-weight:500;}
.medians td.pxy-td .pxy{font-size:10px;}

/* ---- interactive controls ---- */
.toolbar{position:sticky;top:0;z-index:20;display:flex;gap:10px;align-items:center;
  flex-wrap:wrap;padding:10px 12px;margin:0 0 16px;background:var(--surface);
  border:1px solid var(--ring);border-radius:12px;
  box-shadow:0 2px 10px rgba(0,0,0,.06);}
.toolbar input[type=search]{flex:1 1 200px;min-width:150px;padding:7px 10px;font:inherit;
  font-size:13px;color:var(--ink);background:var(--page);border:1px solid var(--line);
  border-radius:8px;}
.toolbar input[type=search]:focus{outline:2px solid var(--val);outline-offset:-1px;}
.btn{font:inherit;font-size:12px;padding:6px 11px;border-radius:8px;cursor:pointer;
  color:var(--ink2);background:var(--page);border:1px solid var(--line);}
.btn:hover{color:var(--ink);border-color:var(--ink2);}
.btn.on{color:#fff;border-color:transparent;}
.btn.on[data-grp=val]{background:var(--val)}.btn.on[data-grp=grow]{background:var(--grow)}
.btn.on[data-grp=prof]{background:var(--prof)}.btn.on[data-grp=bal]{background:var(--bal)}
/* scoped so it can't fight the coloured column chips above */
.btn:not([data-grp])[aria-pressed=true]{background:var(--ink);color:var(--page);border-color:var(--ink);}
.tsep{width:1px;height:22px;background:var(--line);}
.tlab{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}
.toolbar select{font:inherit;font-size:12px;padding:6px 8px;border-radius:8px;color:var(--ink);
  background:var(--page);border:1px solid var(--line);}
#count{margin-left:auto;font-size:12px;color:var(--muted);white-space:nowrap;}
th.sortable{cursor:pointer;user-select:none;}
th.sortable:hover{color:var(--ink);}
th .ind{opacity:0;margin-left:3px;font-size:9px;}
th.sorted .ind{opacity:1;}
th.sorted{color:var(--ink);}
tr.subhead td{cursor:pointer;}
.caret{display:inline-block;width:12px;color:var(--muted);transition:transform .12s;}
tr.subhead.closed .caret{transform:rotate(-90deg);}
tr.hidden,tr.filtered{display:none;}
.flat tr.subhead{display:none;}
.hidecol{display:none;}
.nores{padding:14px 4px;color:var(--muted);font-size:13px;}
@media print{.toolbar{display:none;}}

/* ---- drilldown ---- */
/* cursor only -- no width/height/padding, so the affordance cannot move a
   column or change row height */
td.num.g-val[data-oh]{cursor:pointer}
/* inline-block with no height of its own and a font smaller than the row's
   line-height, so it cannot grow a cell or wrap a line */
.arw{font-size:8px;line-height:1;margin-left:3px;color:var(--muted);display:inline-block;vertical-align:middle;}
#drill{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;
       align-items:center;justify-content:center;z-index:50;}
#drill[hidden]{display:none;}
.drill-card{background:var(--page);border:1px solid var(--line);border-radius:8px;
            padding:18px;max-width:min(720px,92vw);}
.drill-x{float:right;background:none;border:0;color:var(--muted);cursor:pointer;
         font-size:16px;}
#drill-chart{color:var(--val);}
"""


TOOLBAR = """
<div class="toolbar">
  <input id="q" type="search" placeholder="Filter ticker or name…" autocomplete="off">
  <span class="tlab">Columns</span>
  <button class="btn on" data-grp="val" aria-pressed="true">Valuation</button>
  <button class="btn on" data-grp="grow" aria-pressed="true">Growth</button>
  <button class="btn on" data-grp="prof" aria-pressed="true">Profitability</button>
  <button class="btn on" data-grp="bal" aria-pressed="true">Balance</button>
  <span class="tsep"></span>
  <span class="tlab">Frame</span>
  <select id="frame">
    <option value="peers">vs peers</option>
    <option value="own">vs own history</option>
    <option value="c1w">change 1w</option>
    <option value="c1m">change 1m</option>
  </select>
  <span class="tsep"></span>
  <span class="tlab">Tint vs</span>
  <select id="tint">
    <option value="ss">sub-industry</option>
    <option value="sc">whole sector</option>
    <option value="off">off</option>
  </select>
  <span class="tsep"></span>
  <button class="btn" id="flat" aria-pressed="false">Ungroup</button>
  <button class="btn" id="collapse" aria-pressed="false">Collapse all</button>
  <button class="btn" id="csv">Export CSV</button>
  <button class="btn" id="theme">Theme: Auto</button>
  <span id="count"></span>
</div>"""


# Interactivity is layered on top of the server-rendered table: the Python pass
# still paints the sub-industry tint, so the page is fully readable with JS off.
JS_TMPL = """
(function(){
'use strict';
var KEYS=__KEYS__, LABELS=__LABELS__, STAMP=__STAMP__, SERIES=__SERIES__;
var $=function(s){return document.querySelector(s);};
var $$=function(s){return Array.prototype.slice.call(document.querySelectorAll(s));};
// 0.5%: below this a move is rounding, not direction.
var DEADBAND=0.005;
var st={tint:'ss',frame:'peers',flat:false,q:'',groups:{val:1,grow:1,prof:1,bal:1}};

// snapshot the band structure once; all later ordering works off this model
var secs=$$('section.sector').map(function(sec){
  var tb=sec.querySelector('tbody'),bands=[],cur=null;
  Array.prototype.slice.call(tb.children).forEach(function(tr){
    if(tr.classList.contains('subhead')){cur={head:tr,rows:[]};bands.push(cur);}
    else if(cur){cur.rows.push(tr);}
  });
  return {el:sec,tb:tb,bands:bands,sort:null};
});

function num(td){var v=td?td.getAttribute('data-v'):null;return v===null?NaN:parseFloat(v);}
function cmp(a,b,key,dir){
  if(key==='__name'){return dir*String(a.dataset.tk).localeCompare(String(b.dataset.tk));}
  var i=KEYS.indexOf(key)+1,av=num(a.children[i]),bv=num(b.children[i]);
  var an=isNaN(av),bn=isNaN(bv);
  if(an&&bn){return 0;} if(an){return 1;} if(bn){return -1;}  // blanks sink
  return dir*(av-bv);
}
function ordered(rows,s){
  if(!s){return rows;}
  return rows.slice().sort(function(a,b){return cmp(a,b,s.key,s.dir);});
}

function render(){
  secs.forEach(function(sc){
    sc.el.classList.toggle('flat',st.flat);
    var frag=document.createDocumentFragment();
    if(st.flat){
      var all=[];
      sc.bands.forEach(function(b){all=all.concat(b.rows);});
      ordered(all,sc.sort).forEach(function(r){frag.appendChild(r);});
      sc.bands.forEach(function(b){frag.appendChild(b.head);});  // parked, CSS hides
    }else{
      sc.bands.forEach(function(b){
        frag.appendChild(b.head);
        ordered(b.rows,sc.sort).forEach(function(r){frag.appendChild(r);});
      });
    }
    sc.tb.appendChild(frag);
  });
  applyFilter();
}

function applyFilter(){
  var q=st.q.trim().toLowerCase(),shown=0,total=0;
  secs.forEach(function(sc){
    var secVis=0;
    sc.bands.forEach(function(b){
      var vis=0,closed=b.head.classList.contains('closed');
      b.rows.forEach(function(r){
        total++;
        var nm=r.querySelector('.nm');
        var hay=(r.dataset.tk+' '+(nm?nm.textContent:'')).toLowerCase();
        var ok=!q||hay.indexOf(q)>-1;
        r.classList.toggle('filtered',!ok);
        r.classList.toggle('hidden',closed&&!st.flat);
        if(ok){vis++;shown++;}
      });
      secVis+=vis;
      b.head.classList.toggle('hidden',st.flat||vis===0);
    });
    sc.el.style.display=secVis?'':'none';
  });
  $('#count').textContent=q?(shown+' of '+total+' shown'):(total+' companies');
}

function tintOf(s){
  if(isNaN(s)){return '';}
  var a=Math.min(0.34,Math.abs(s)*0.34);
  return 'rgba('+(s>0?'37,106,191':'208,59,59')+','+a.toFixed(3)+')';
}
// An own-history percentile is 0..1 where 0 is the cheapest the name has
// ever been. The peer scale is -1..1 with +1 favorable, so a cheap multiple
// (low percentile) must map to +1 -- hence 1-2*p rather than p itself.
// c1w/c1m read from data attributes that do not exist until Task 14; their
// <option>s are disabled in the markup so this branch is unreachable through
// the UI, but frameScore still returns null for them rather than throwing.
function frameScore(td){
  if(st.frame==='peers'){
    return st.tint==='off'?null
      :td.getAttribute(st.tint==='ss'?'data-ss':'data-sc');
  }
  if(st.frame==='own'){
    var p=td.getAttribute('data-oh');
    return p===null?null:String(1-2*parseFloat(p));
  }
  // the SCALED attribute, not the raw one: a raw log change of 0.09 is a
  // large weekly move but would tint at 9% of full if fed straight to tintOf.
  // data-c1ws / data-c1ms carry the saturation already applied server-side.
  return td.getAttribute(st.frame==='c1w'?'data-c1ws':'data-c1ms');
}

// The arrow is always on, in every frame, because direction is the one thing
// a reader wants without having to change a control. It reads the RAW change,
// so it says which way the number moved rather than whether that was good --
// the tint already carries good-or-bad, and doubling it up would lose the
// distinction between "fell" and "improved by falling".
function arrows(){
  $$('td.num').forEach(function(td){
    var old=td.querySelector('.arw');
    if(old){old.remove();}
    var c=td.getAttribute('data-c1w');
    if(c===null){return;}
    var v=parseFloat(c);
    if(!(Math.abs(v)>=DEADBAND)){return;}
    var s=document.createElement('span');
    s.className='arw';
    s.textContent=v>0?'\u25b2':'\u25bc';
    s.setAttribute('aria-hidden','true');
    td.appendChild(s);
  });
}
function applyTint(){
  $$('td.num').forEach(function(td){
    var v=frameScore(td);
    td.style.background=(v===null)?'':tintOf(parseFloat(v));
  });
}

// KEYS is the metric order build_js() embeds. The first cell of a row is the
// sticky name column, so a cell's metric is its position less one.
function colOf(td){
  return Array.prototype.indexOf.call(td.parentNode.children, td) - 1;
}
// pct is the percentile already painted on the cell (data-oh), computed
// server-side from the full daily series -- NOT re-derived here from
// SERIES, which is downsampled to weekly for the picture only. Recomputing
// it from these weekly points is exactly the bug this function must not
// have: it would let the cell's tint and this panel's own number come from
// two different series and disagree on screen at the same moment.
function drawDrill(tk,key,pct){
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
  if(last===null){return;}
  $('#drill-chart').innerHTML=
    "<path d='"+d+"' fill='none' stroke='currentColor' stroke-width='1.5'/>";
  $('#drill-title').textContent=tk+' — '+key;
  $('#drill-note').textContent=
    'now '+last.toFixed(1)+'  ·  range '+lo.toFixed(1)+'–'+hi.toFixed(1)+
    '  ·  '+Math.round(pct*100)+'th percentile of its own history';
  $('#drill').hidden=false;
}

function applyGroups(){
  Object.keys(st.groups).forEach(function(g){
    var on=!!st.groups[g];
    $$('.g-'+g).forEach(function(el){el.classList.toggle('hidecol',!on);});
    var btn=$('.btn[data-grp="'+g+'"]');
    if(btn){btn.classList.toggle('on',on);btn.setAttribute('aria-pressed',String(on));}
  });
}

function exportCSV(){
  var vis=KEYS.map(function(k){
    var th=$('.colhead th[data-key="'+k+'"]');
    return th&&!th.classList.contains('hidecol');
  });
  var head=['Sector','Sub-industry','Ticker','Name'];
  LABELS.forEach(function(l,i){if(vis[i]){head.push(l);}});
  var out=[head];
  secs.forEach(function(sc){
    var sector=sc.el.querySelector('h2').textContent.trim();
    sc.bands.forEach(function(b){
      b.rows.forEach(function(r){
        if(r.classList.contains('filtered')){return;}
        var nm=r.querySelector('.nm');
        var line=[sector,r.dataset.sub,r.dataset.tk,nm?nm.textContent:''];
        KEYS.forEach(function(k,i){
          if(!vis[i]){return;}
          var v=r.children[i+1].getAttribute('data-v');
          line.push(v===null?'':v);
        });
        out.push(line);
      });
    });
  });
  var body=out.map(function(r){
    return r.map(function(c){
      c=String(c);
      return /[",\\n]/.test(c)?'"'+c.replace(/"/g,'""')+'"':c;
    }).join(',');
  }).join('\\n');
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([body],{type:'text/csv;charset=utf-8'}));
  a.download='fundamentals_view_'+STAMP+'.csv';
  document.body.appendChild(a);a.click();document.body.removeChild(a);
  setTimeout(function(){URL.revokeObjectURL(a.href);},1000);
}

var root=document.documentElement;
function setTheme(t){
  if(t==='auto'){root.removeAttribute('data-theme');}else{root.setAttribute('data-theme',t);}
  $('#theme').textContent='Theme: '+t.charAt(0).toUpperCase()+t.slice(1);
  try{localStorage.setItem('ft-theme',t);}catch(e){}
}

// ---- wiring ----
$('#q').addEventListener('input',function(e){st.q=e.target.value;applyFilter();});
$('#tint').addEventListener('change',function(e){st.tint=e.target.value;applyTint();});
$('#frame').addEventListener('change',function(e){
  st.frame=e.target.value;
  // the basis only means something for the peer frame -- greyed out rather
  // than hidden, so it is visibly "not applicable here" and not "broken"
  $('#tint').disabled=(st.frame!=='peers');
  applyTint();
});
$$('.btn[data-grp]').forEach(function(b){
  b.addEventListener('click',function(){
    var g=b.dataset.grp;st.groups[g]=!st.groups[g];applyGroups();
  });
});
$('#flat').addEventListener('click',function(){
  st.flat=!st.flat;
  $('#flat').setAttribute('aria-pressed',String(st.flat));
  $('#flat').textContent=st.flat?'Group':'Ungroup';
  render();
});
$('#collapse').addEventListener('click',function(){
  var anyOpen=$$('tr.subhead').some(function(t){return !t.classList.contains('closed');});
  $$('tr.subhead').forEach(function(t){t.classList.toggle('closed',anyOpen);});
  $('#collapse').textContent=anyOpen?'Expand all':'Collapse all';
  $('#collapse').setAttribute('aria-pressed',String(anyOpen));
  applyFilter();
});
$('#csv').addEventListener('click',exportCSV);
$('#theme').addEventListener('click',function(){
  var cur=root.getAttribute('data-theme')||'auto';
  setTheme(cur==='auto'?'light':(cur==='light'?'dark':'auto'));
});
$$('tr.subhead').forEach(function(tr){
  tr.addEventListener('click',function(){tr.classList.toggle('closed');applyFilter();});
});
document.addEventListener('click',function(e){
  var td=e.target.closest?e.target.closest('td.num.g-val'):null;
  if(td){
    var oh=td.getAttribute('data-oh');
    if(oh!==null){
      drawDrill(td.closest('tr').dataset.tk, KEYS[colOf(td)], parseFloat(oh));
    }
  }
  if(e.target.closest&&e.target.closest('.drill-x')){$('#drill').hidden=true;}
});
$$('th.sortable').forEach(function(th){
  th.addEventListener('click',function(){
    var sc=null;
    secs.forEach(function(s){if(s.el.contains(th)){sc=s;}});
    if(!sc){return;}
    var key=th.dataset.key;
    if(sc.sort&&sc.sort.key===key){
      sc.sort=sc.sort.dir===-1?{key:key,dir:1}:null;   // desc -> asc -> original
    }else{
      sc.sort={key:key,dir:key==='__name'?1:-1};
    }
    sc.el.querySelectorAll('th.sortable').forEach(function(o){
      o.classList.remove('sorted');o.querySelector('.ind').textContent='';
    });
    if(sc.sort){
      th.classList.add('sorted');
      th.querySelector('.ind').textContent=sc.sort.dir===1?'\\u25B2':'\\u25BC';
    }
    render();
  });
});

// keep the sticky column headers parked just under the toolbar, whatever
// height it wraps to at this viewport width
function measure(){
  var tb=document.querySelector('.toolbar');
  if(tb){root.style.setProperty('--tbh',Math.round(tb.getBoundingClientRect().height)+'px');}
}
window.addEventListener('resize',measure);

try{var saved=localStorage.getItem('ft-theme');if(saved){setTheme(saved);}}catch(e){}
measure();applyGroups();applyTint();arrows();applyFilter();
})();
"""


def build_js(metrics: list, series: dict | None = None) -> str:
    keys = [k for k, _l, _g, _f, _hb in metrics]
    labels = [l for _k, l, _g, _f, _hb in metrics]
    return (JS_TMPL
            .replace("__KEYS__", json.dumps(keys))
            .replace("__LABELS__", json.dumps(labels))
            .replace("__STAMP__", json.dumps(datetime.now().strftime("%Y%m%d")))
            .replace("__SERIES__", json.dumps(series or {})))


def render_html(df: pd.DataFrame, spx: dict, proxies: dict,
                 metrics: list, universe: dict, group_labels: dict,
                 domains: dict, own: dict | None = None,
                 series: dict | None = None,
                 changes: dict | None = None) -> str:
    asof = spx.get("asof", datetime.now(timezone.utc))
    asof_s = asof.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    sectors = "".join(
        render_sector(sec, df[df.sector == sec], proxies, metrics, universe,
                      group_labels, domains, own=own, changes=changes)
        for sec in universe
    )
    spx_html = render_spx(spx, df, proxies, universe)
    legend = (
        "<div class='legend'>"
        "<span><span class='chip' style='background:rgba(37,106,191,.34)'></span>"
        "more favorable vs sector peers</span>"
        "<span><span class='chip' style='background:rgba(208,59,59,.34)'></span>"
        "less favorable</span>"
        "<span class='na'>Tint is relative within each <b>sub-industry</b>, per column — "
        "semis are scored against semis, infra software against infra software. "
        "<b>6M Trend</b> plots ~6 months of adjusted closes (dashed line = the window's "
        "starting price) and sorts/exports on the 6M % change.</span>"
        "<span class='na'><b>Frame</b> repaints the same table: "
        "<b>vs own history</b> asks where a multiple sits in its own five-year "
        "range, and only appears for the metrics that passed the source proof. "
        "<b>change 1w/1m</b> tint by how far a number moved; the arrow shows "
        "the 1-week direction in every frame, with moves under 0.5% left "
        "unmarked as rounding rather than direction. Valuation multiples carry "
        "both windows from their own daily series; the remaining metrics come "
        "from the snapshot table, which has accrued enough for 1 week but not "
        "yet a month, so those cells stay untinted on <b>change 1m</b> rather "
        "than reading as no change.</span>"
        "</div>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fundamentals Tracker — {asof.strftime('%Y-%m-%d')}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <h1>Company Fundamentals Tracker</h1>
  <div class="asof">TMT · Industrials · Consumer &nbsp;|&nbsp; Data as of {asof_s} &nbsp;·&nbsp; source: Yahoo Finance</div>
</header>
{spx_html}
{TOOLBAR}
{legend}
{sectors}
<div id="drill" hidden>
  <div class="drill-card">
    <button class="drill-x" aria-label="Close">x</button>
    <h3 id="drill-title"></h3>
    <svg id="drill-chart" viewBox="0 0 640 200" width="100%" height="200"></svg>
    <p id="drill-note" class="na"></p>
  </div>
</div>
<footer>
  Generated by <code>build_dashboard.py</code>. Fundamentals from Yahoo Finance via yfinance —
  figures are consensus/TTM as reported by the source and may lag or contain gaps.
  Sparklines and all returns use split/dividend-adjusted daily closes over the trailing
  ~6 months (1M = 22 trading days, 6M = 126, YTD from Jan 1); proxy ETFs are stand-ins
  for each band, not the band itself. Valuation multiples move with price; treat as a
  directional screen, not investment advice.
</footer>
</div>
<script>{build_js(metrics, series)}</script>
</body></html>"""
