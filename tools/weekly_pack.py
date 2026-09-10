#!/usr/bin/env python3
"""Evidence pack for the weekly note.

The dashboard answers "what are the numbers". This answers the questions that
need two sources put next to each other -- where analysts are moving against
price, where a cheap multiple has falling estimates behind it, where insiders
and managers are positioned, and which of the week's moves were data rather
than news.

Division of labour, and it matters: this script produces facts, and the model
that writes the note may only interpret them. Every figure in the outgoing
email must appear here first. The reason is on the record -- the first
hand-written version of that note carried eight wrong revenue-growth figures,
one of which (Deere at +2.4% against a real -11.1%) inverted its own
conclusion. Interpretation is what a model is for; arithmetic is not.

Reads:  data/history.db  and  dashboard.html
Writes: the pack to stdout, and to reports/pack_YYYYMMDD.txt

Usage:  python tools/weekly_pack.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history  # noqa: E402
from build_dashboard import UNIVERSE  # noqa: E402
from weekly_note import _ordinal, parse_dashboard  # noqa: E402

SECTOR = {t: s.split(" ")[0] for s, subs in UNIVERSE.items()
          for sub in subs.values() for t in sub}
BAND = {t: sub for s, subs in UNIVERSE.items()
        for sub, tks in subs.items() for t in tks}

# Bands smaller than this are one or two names wearing a sector label; their
# medians move on a single print and are not worth a sentence.
MIN_BAND = 4

# Insider windows: a quarter is long enough that a single scheduled sale does
# not define the picture, short enough to still be about now.
INSIDER_DAYS = 90


def _pct(n, d):
    return float("nan") if not d else 100.0 * n / d


def estimate_revisions(conn) -> tuple[pd.DataFrame, pd.DataFrame, str, pd.DataFrame]:
    """Where analysts are moving numbers, by sector and by band.

    The single most useful thing in the store that the dashboard does not
    show: the dashboard is a level, this is a direction, and they disagree
    often enough to be the point of the note.
    """
    latest = conn.execute(
        "SELECT MAX(as_of) FROM metrics WHERE period_type='estimate'").fetchone()[0]
    raw = pd.read_sql_query(
        "SELECT ticker, metric, value FROM metrics "
        "WHERE period_type='estimate' AND as_of=?", conn, params=[latest])
    w = raw.pivot_table(index="ticker", columns="metric", values="value")
    for col in ("epsRevUp30", "epsRevDown30", "epsRevUp7", "epsRevDown7"):
        if col not in w:
            w[col] = np.nan
    w["sector"] = [SECTOR.get(t) for t in w.index]
    w["band"] = [BAND.get(t) for t in w.index]
    w["net30"] = w.epsRevUp30 - w.epsRevDown30
    w["net7"] = w.epsRevUp7 - w.epsRevDown7
    if {"epsEstHigh", "epsEstLow", "epsEstAvg"} <= set(w.columns):
        w["spread"] = ((w.epsEstHigh - w.epsEstLow)
                       / w.epsEstAvg.abs().clip(lower=1e-9))

    def agg(key):
        g = w.dropna(subset=[key]).groupby(key)
        out = g.agg(names=("net30", "size"), up=("epsRevUp30", "sum"),
                    down=("epsRevDown30", "sum"), net=("net30", "median"))
        out["ratio"] = (out.up / out.down.clip(lower=1)).round(2)
        out["rising"] = g.net30.apply(lambda s: int((s > 0).sum()))
        out["falling"] = g.net30.apply(lambda s: int((s < 0).sum()))
        return out

    return agg("sector"), agg("band").query(f"names >= {MIN_BAND}"), latest, w


def insider_flow(conn) -> tuple[pd.DataFrame, pd.Series, dict]:
    """Open-market buying and selling, which is the only kind that means much.

    Grants, gifts and option exercises are compensation, not a view, so they
    are counted separately rather than netted in.
    """
    cutoff = (date.today() - timedelta(days=INSIDER_DAYS)).isoformat()
    txn = pd.read_sql_query(
        "SELECT ticker, as_of, txn_type, value FROM insider_txns WHERE as_of >= ?",
        conn, params=[cutoff])
    if txn.empty:
        return pd.DataFrame(), pd.Series(dtype=float), {}
    txn["sector"] = txn.ticker.map(SECTOR)
    buy = txn.txn_type.str.contains("Purchase", case=False, na=False)
    sell = txn.txn_type.str.contains("Sale", case=False, na=False)
    txn["kind"] = np.where(buy, "buy", np.where(sell, "sell", "compensation"))
    counts = txn.kind.value_counts().to_dict()

    flow = (txn[txn.kind != "compensation"]
            .groupby(["sector", "kind"]).value.sum().unstack(fill_value=0))
    for col in ("buy", "sell"):
        if col not in flow:
            flow[col] = 0.0
    flow = (flow[["buy", "sell"]] / 1e6).round(1)
    flow["net"] = (flow.buy - flow.sell).round(1)
    buys = (txn[txn.kind == "buy"].groupby("ticker").value.sum()
            .sort_values(ascending=False) / 1e6).round(2)
    return flow, buys[buys > 0], counts


def manager_crowding(conn) -> tuple[pd.DataFrame, str, int]:
    """How many tracked managers hold each name, newest filed quarter.

    Crowding is only interesting beside valuation: a name every manager owns
    at the top of its own range is a different proposition from one they all
    own at the bottom.
    """
    quarters = history.thirteenf_quarters(conn)
    if not quarters:
        return pd.DataFrame(), "", 0
    quarter = quarters[0]
    held = pd.read_sql_query(
        "SELECT ticker, fund, value FROM thirteenf WHERE quarter=?",
        conn, params=[quarter])
    if held.empty:
        return pd.DataFrame(), quarter, 0
    out = held.groupby("ticker").agg(funds=("fund", "nunique"),
                                     value=("value", "sum"))
    out["value_m"] = (out.value / 1e6).round(0)
    return (out.sort_values("funds", ascending=False)
            .drop(columns="value")), quarter, held.fund.nunique()


def dashboard_view(page: Path) -> tuple[pd.DataFrame, dict, str]:
    data = parse_dashboard(page)
    df = pd.DataFrame(data["rows"])
    num = [c for c in df.columns if "." in c]
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce") \
            if not c.endswith(".rs") else df[c]
    return df, data["index"], data["asof"]


def write_pack(conn, page: Path) -> str:
    df, index, asof = dashboard_view(page)
    sec_rev, band_rev, est_asof, w_est = estimate_revisions(conn)
    flow, buys, kinds = insider_flow(conn)
    crowd, quarter, n_funds = manager_crowding(conn)

    MARKED = [c[:-3] for c in df.columns if c.endswith(".rs")]
    mark = df[[f"{m}.rs" for m in MARKED]]
    df["revisions"] = (mark == "revision").sum(axis=1)
    df["reports"] = (mark == "report").sum(axis=1)

    out: list[str] = []
    w = out.append
    w(f"EVIDENCE PACK — dashboard {asof}, estimates {est_asof}")
    w(f"{len(df)} names. Every figure below is measured; none is estimated.")
    w("")

    w("== MARKET ==")
    for k in ("S&P 500", "1-Day", "1-Month", "YTD", "1-Year", "Trail P/E"):
        if k in index:
            w(f"  {k:<12s} {index[k]}")
    w("")

    w("== SECTOR: LEVEL vs DIRECTION ==")
    w("  Own 5y = median percentile of today's P/S in each name's own five-year")
    w("  range. Est ratio = EPS estimates raised vs cut over 30 days.")
    w(f"  {'sector':<12s} {'n':>3s} {'P/E':>7s} {'P/S':>6s} {'rev gr':>8s}"
      f" {'own 5y':>7s} {'est up:dn':>10s} {'rising':>7s} {'falling':>8s}")
    for sec, d in df.groupby("sector"):
        r = sec_rev.loc[sec] if sec in sec_rev.index else None
        oh = d["ps.oh"].median()
        w(f"  {sec:<12s} {len(d):3d} {d['trailingPE.v'].median():6.1f}x"
          f" {d['ps.v'].median():5.1f}x {d['revGrowth.v'].median():+7.1f}%"
          f" {(_ordinal(round(100*oh)) if pd.notna(oh) else '—'):>7s}"
          f" {(f'{r.ratio:.2f}' if r is not None else '—'):>10s}"
          f" {(f'{int(r.rising)}/{int(r.names)}' if r is not None else '—'):>7s}"
          f" {(f'{int(r.falling)}/{int(r.names)}' if r is not None else '—'):>8s}")
    w("")

    w("== BAND: WHERE ESTIMATES ARE MOVING ==")
    w(f"  {'band':<30s} {'n':>3s} {'est up:dn':>10s} {'net':>7s} {'own 5y':>7s}")
    own_by_band = df.groupby("band")["ps.oh"].median()
    for band, r in band_rev.sort_values("ratio", ascending=False).iterrows():
        oh = own_by_band.get(band, float("nan"))
        w(f"  {band[:30]:<30s} {int(r.names):3d} {r.ratio:10.2f} {r.net:+7.2f}"
          f" {(_ordinal(round(100*oh)) if pd.notna(oh) else '—'):>7s}")
    w("")

    w("== DIVERGENCE: PRICE AGAINST ESTIMATES ==")
    w("  A level tells you what something costs; a revision tells you which way")
    w("  the number behind it is going. Where those disagree is where the week's")
    w("  reading is either an opportunity or a warning, and the pack cannot tell")
    w("  you which -- only that the two sources do not agree.")
    ret = pd.read_sql_query(
        "SELECT ticker, value FROM metrics WHERE period_type='snapshot' "
        "AND metric='ret1m' AND as_of=(SELECT MAX(as_of) FROM metrics "
        "WHERE period_type='snapshot')", conn).set_index("ticker").value
    joined = pd.DataFrame({"ret1m": ret}).join(
        w_est[["sector", "band", "net30"]], how="inner").dropna()
    w("")
    w(f"  {'sector':<12s} {'names':>5s} {'median 1m ret':>14s}"
      f" {'median net rev':>15s} {'reading':>28s}")
    for sec, d in joined.groupby("sector"):
        r, n = d.ret1m.median(), d.net30.median()
        if r < 0 and n > 0:
            read = "sold off, estimates rising"
        elif r > 0 and n < 0:
            read = "bid up, estimates falling"
        elif r < 0 and n < 0:
            read = "sold off, estimates falling"
        else:
            read = "bid up, estimates rising"
        w(f"  {sec:<12s} {len(d):5d} {r:+13.1f}% {n:+15.2f} {read:>28s}")
    w("")
    w("  Individual names where the two disagree most (1m return vs 30d net")
    w("  EPS revisions), which is where a single position decision might live:")
    cheap_up = joined[(joined.ret1m < -5) & (joined.net30 > 0)].nlargest(6, "net30")
    rich_down = joined[(joined.ret1m > 5) & (joined.net30 < 0)].nsmallest(6, "net30")
    names = df.set_index("ticker")["name"]
    for lab, block in (("  sold off while estimates rose:", cheap_up),
                       ("  bid up while estimates fell:", rich_down)):
        w(lab)
        if block.empty:
            w("     none this week")
            continue
        for tk, r in block.iterrows():
            w(f"     {tk:<6s} {str(names.get(tk, ''))[:24]:<24s}"
              f" 1m {r.ret1m:+6.1f}%   net rev {r.net30:+6.1f}")
    w("")

    w("== DATA EVENTS THIS WEEK ==")
    w(f"  {int(df.reports.sum())} metrics moved on a filing (*), across "
      f"{int((df.reports > 0).sum())} names")
    w(f"  {int(df.revisions.sum())} moved with no filing (!), across "
      f"{int((df.revisions > 0).sum())} names")
    for lab, col in (("!", "revisions"), ("*", "reports")):
        top = df.nlargest(5, col)
        top = top[top[col] > 0]
        if top.empty:
            continue
        w(f"  {lab} most affected: " + ", ".join(
            f"{r.ticker} ({int(r[col])})" for _, r in top.iterrows()))
    by_sec = df.groupby("sector")[["reports", "revisions"]].sum()
    w("  by sector: " + "; ".join(
        f"{s} {int(r.reports)}* {int(r.revisions)}!" for s, r in by_sec.iterrows()))
    w("")

    w("== LARGEST 1-WEEK RE-RATINGS, WITH PROVENANCE ==")
    d = df.dropna(subset=["trailingPE.c1w"]).copy()
    d["pct"] = (np.exp(d["trailingPE.c1w"]) - 1) * 100
    for _, r in d.reindex(d.pct.abs().sort_values(ascending=False).index).head(8).iterrows():
        # The mark on THIS cell, not the name's aggregate. A name can carry a
        # revision on one metric and a filing on another; labelling the P/E
        # move from the name-level count mislabels it, and the first note
        # written off this pack correctly refused to trust either figure
        # because the two sections disagreed.
        cell = r.get("trailingPE.rs")
        prov = ("! source revision" if cell == "revision" else
                "* new filing" if cell == "report" else "  price only")
        w(f"  {r.ticker:<6s} {r['name'][:24]:<24s} {r['trailingPE.v']:7.1f}x"
          f" {r.pct:+7.1f}%   {prov}")
    w("")

    for label, asc in (("== CHEAPEST vs OWN 5 YEARS (P/S) ==", True),
                       ("== RICHEST vs OWN 5 YEARS (P/S) ==", False)):
        w(label)
        d = df.dropna(subset=["ps.oh"]).sort_values("ps.oh", ascending=asc).head(8)
        for _, r in d.iterrows():
            w(f"  {r.ticker:<6s} {r['name'][:24]:<24s} {r['ps.v']:6.1f}x"
              f" {_ordinal(round(100*r['ps.oh'])):>5s} pct  rev gr {r['revGrowth.v']:+6.1f}%"
              f"  net mgn {r['netMargin.v']:5.1f}%")
        w("")

    w("== INSIDERS (open market only, last 90 days) ==")
    if kinds:
        w(f"  {kinds.get('sell', 0)} sales, {kinds.get('buy', 0)} purchases, "
          f"{kinds.get('compensation', 0)} grants/gifts/exercises (excluded)")
        w(f"  {'sector':<12s} {'bought $m':>10s} {'sold $m':>9s} {'net $m':>9s}")
        for sec, r in flow.iterrows():
            w(f"  {sec:<12s} {r.buy:10.1f} {r.sell:9.1f} {r.net:9.1f}")
        if len(buys):
            w("  largest open-market purchases: " + ", ".join(
                f"{t} ${v:.1f}m" for t, v in buys.head(6).items()))
    else:
        w("  no insider data in window")
    w("")

    w(f"== MANAGER CROWDING (13F, {quarter}, {n_funds} funds) ==")
    if not crowd.empty:
        own = df.set_index("ticker")
        w(f"  {'ticker':<7s} {'funds':>5s} {'$m held':>9s} {'own 5y P/S':>11s}"
          f" {'own 5y P/E':>11s}")
        for tk, r in crowd.head(10).iterrows():
            ps = own["ps.oh"].get(tk, float("nan"))
            pe = own["trailingPE.oh"].get(tk, float("nan"))
            w(f"  {tk:<7s} {int(r.funds):5d} {r.value_m:9.0f}"
              f" {(_ordinal(round(100*ps)) if pd.notna(ps) else '—'):>11s}"
              f" {(_ordinal(round(100*pe)) if pd.notna(pe) else '—'):>11s}")
    w("")

    n_ps = int(df["ps.oh"].notna().sum())
    n_pe = int(df["trailingPE.oh"].notna().sum())
    w("== COVERAGE CAVEATS (state these if you cite the percentiles) ==")
    w(f"  Own-history percentiles: {n_ps} names on P/S, {n_pe} on trailing P/E.")
    w("  Only pairs that reconstructed to within 1% of the vendor's published")
    w("  figure from SEC filings qualify. EV/EBITDA and FCF yield have none --")
    w("  neither reconstructed accurately enough to publish, so they are absent")
    w("  rather than wrong. A blank is never 'no change'.")
    w("  Estimate revision counts are analyst actions, not magnitudes.")
    w("  Two different return series appear above, do not mix them:")
    w("    - MARKET's 1-Month/YTD are index and proxy-ETF price returns.")
    w("    - DIVERGENCE's median 1m ret is the median of the tracked NAMES'")
    w("      own ret1m, computed per name. Those magnitudes are sound.")
    w("  Estimate up:dn is a count of analyst actions; net is the median of")
    w("  (raised - cut) per name. A band can show a ratio above 1 with a")
    w("  negative net, or the reverse -- they answer different questions and")
    w("  disagreeing is not a contradiction. Read both as flat.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", default=str(ROOT / "dashboard.html"))
    ap.add_argument("--out", default=str(ROOT / "reports"))
    args = ap.parse_args(argv)

    page = Path(args.dashboard)
    if not page.exists():
        print(f"no dashboard at {page}", file=sys.stderr)
        return 1

    conn = history.connect()
    try:
        pack = write_pack(conn, page)
    finally:
        conn.close()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"pack_{date.today():%Y%m%d}.txt"
    dest.write_text(pack + "\n", encoding="utf-8")
    print(pack)
    print(f"\n[written to {dest}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
