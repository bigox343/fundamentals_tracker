#!/usr/bin/env python3
"""Weekly fundamentals note, extracted from the built dashboard.

Every figure here is read out of dashboard.html, never restated from memory.
That is the whole point of the script existing: the first draft of this note
was written by hand and eight of its revenue-growth figures were wrong --
Deere's was quoted as +2.4% when the real number is -11.1%, which inverts the
story rather than shading it. A model asked to summarise numbers will
occasionally supply them instead. Code cannot.

Reads:  dashboard.html  (built by build_dashboard.py)
Writes: the note to stdout, and to reports/weekly_YYYYMMDD.txt

Usage:  python tools/weekly_note.py [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Metrics that carry a provenance mark. Mirrors build_dashboard's
# RATIO_EVENT_METRICS | DIRECT_EVENT_METRICS -- imported rather than retyped
# so the two cannot drift.
from build_dashboard import DIRECT_EVENT_METRICS, RATIO_EVENT_METRICS  # noqa: E402

MARKED = sorted(RATIO_EVENT_METRICS | DIRECT_EVENT_METRICS)


def _ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 21st -- not 21th."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def parse_dashboard(path: Path) -> dict:
    """Everything the note needs, in one pass over the page."""
    h = path.read_text(encoding="utf-8")
    keys = json.loads(re.search(r"var KEYS=(\[.*?\])", h).group(1))

    asof = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", h)

    index = {}
    spx = h[h.index("S&amp;P 500"):][:900]
    for label, value in re.findall(
            r"<[^>]*>([A-Za-z0-9/&; -]+?)</[^>]*>\s*<[^>]*>([-+0-9.,%x]+)<", spx):
        lab = label.replace("&amp;", "&").strip()
        if lab and value:
            index[lab] = value

    rows, sector = [], None
    for chunk in re.split(r"(<h2[^>]*>.*?</h2>)", h, flags=re.S):
        head = re.match(r"<h2[^>]*>(.*?)</h2>", chunk, re.S)
        if head:
            sector = _strip(head.group(1)).replace("&amp;", "&")
            continue
        if sector is None:
            continue
        band = None
        for piece in re.split(r"(<tr class='subhead[^>]*>.*?</tr>)", chunk, flags=re.S):
            sub = re.match(r"<tr class='subhead[^>]*data-sub=\"([^\"]+)\"", piece)
            if sub:
                band = sub.group(1)
                continue
            for tk, body in re.findall(
                    r"<tr[^>]*data-tk=\"([^\"]+)\"[^>]*>(.*?)</tr>", piece, re.S):
                name = re.search(r"class='nm'>([^<]*)", body)
                rec = {"ticker": tk, "name": name.group(1) if name else "",
                       "sector": sector.split(" ")[0], "band": band}
                cells = re.findall(r"<td class='num[^']*'([^>]*)>", body)
                for i, attrs in enumerate(cells):
                    if i >= len(keys):
                        break
                    for attr, dest in (("data-v", "v"), ("data-oh", "oh"),
                                       ("data-c1w", "c1w"), ("data-rs", "rs")):
                        m = re.search(rf"{attr}='([^']*)'", attrs)
                        if m:
                            rec[f"{keys[i]}.{dest}"] = m.group(1)
                rows.append(rec)
    return {"asof": asof.group(1) if asof else "?", "index": index, "rows": rows}


def _num(rows, key):
    import pandas as pd
    return pd.to_numeric(pd.DataFrame(rows).get(key), errors="coerce")


def build_note(data: dict) -> str:
    import numpy as np
    import pandas as pd

    df = pd.DataFrame(data["rows"])
    num = lambda c: pd.to_numeric(df.get(c), errors="coerce")   # noqa: E731
    mark = pd.DataFrame({m: df.get(f"{m}.rs") for m in MARKED})
    rev = (mark == "revision").sum(axis=1)
    rep = (mark == "report").sum(axis=1)

    out = []
    w = out.append
    w(f"FUNDAMENTALS NOTE — {data['asof'][:10]}")
    w(f"{len(df)} names across {', '.join(sorted(df.sector.unique()))}")
    w("")

    w("MARKET")
    for label in ("S&P 500", "1-Month", "YTD", "1-Year", "Trail P/E"):
        if label in data["index"]:
            w(f"  {label:<12s} {data['index'][label]:>9s}")
    w("")

    w("SECTOR")
    g = df.assign(pe=num("trailingPE.v"), ps=num("ps.v"), nm=num("netMargin.v"),
                  rg=num("revGrowth.v"), oh=num("ps.oh"), rev=rev, rep=rep)
    w(f"  {'':<12s} {'names':>6s} {'P/E':>7s} {'P/S':>6s} {'net':>7s}"
      f" {'rev gr':>8s} {'own 5y':>8s} {'*':>4s} {'!':>4s}")
    for sec, d in g.groupby("sector"):
        oh = d.oh.median()
        w(f"  {sec:<12s} {len(d):6d} {d.pe.median():6.1f}x {d.ps.median():5.1f}x"
          f" {d.nm.median():6.1f}% {d.rg.median():+7.1f}%"
          f" {_ordinal(round(100 * oh)) if pd.notna(oh) else '—':>8s}"
          f" {int(d.rep.sum()):4d} {int(d.rev.sum()):4d}")
    w("")

    w("DATA EVENTS THIS WEEK")
    w(f"  {int(rep.sum())} metrics moved on a filing (*), across "
      f"{int((rep > 0).sum())} names")
    w(f"  {int(rev.sum())} moved with no filing behind them (!), across "
      f"{int((rev > 0).sum())} names")
    for label, series in (("!  revised", rev), ("*  filed", rep)):
        top = df.assign(n=series).nlargest(5, "n")
        top = top[top.n > 0]
        if top.empty:
            continue
        w(f"  {label}:")
        for _, r in top.iterrows():
            w(f"     {r.ticker:<6s} {r['name'][:28]:<28s} {int(r.n):2d} metrics")
    w("")

    w("LARGEST 1-WEEK RE-RATINGS (trailing P/E)")
    d = df.assign(chg=num("trailingPE.c1w"), val=num("trailingPE.v"),
                  rev=rev, rep=rep).dropna(subset=["chg"])
    d["pct"] = (np.exp(d.chg) - 1) * 100
    for _, r in d.reindex(d.pct.abs().sort_values(ascending=False).index).head(6).iterrows():
        prov = "! revised" if r.rev else ("* filed" if r.rep else "  price only")
        w(f"  {r.ticker:<6s} {r['name'][:26]:<26s} {r.val:7.1f}x {r.pct:+7.1f}%  {prov}")
    w("")

    for label, asc in (("CHEAPEST AGAINST THEIR OWN 5 YEARS (P/S)", True),
                       ("RICHEST", False)):
        w(label)
        d = df.assign(oh=num("ps.oh"), ps=num("ps.v"),
                      rg=num("revGrowth.v")).dropna(subset=["oh"])
        d = d.sort_values("oh", ascending=asc).head(6)
        for _, r in d.iterrows():
            w(f"  {r.ticker:<6s} {r['name'][:26]:<26s} {r.ps:6.1f}x"
              f" {_ordinal(round(100 * r.oh)):>5s} pct  rev {r.rg:+6.1f}%")
        w("")

    n_ps = int(num("ps.oh").notna().sum())
    n_pe = int(num("trailingPE.oh").notna().sum())
    w("---")
    w(f"Own-history percentiles exist for {n_ps} names on P/S and {n_pe} on "
      f"trailing P/E: only those that")
    w("reconstructed to within 1% of the vendor's published figure from SEC "
      "filings. EV/EBITDA and")
    w("FCF yield are absent -- neither could be reproduced accurately enough "
      "to show. A blank is")
    w('never "no change".')
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", default=str(ROOT / "dashboard.html"))
    ap.add_argument("--out", default=str(ROOT / "reports"))
    args = ap.parse_args(argv)

    page = Path(args.dashboard)
    if not page.exists():
        print(f"no dashboard at {page} -- run build_dashboard.py first",
              file=sys.stderr)
        return 1

    note = build_note(parse_dashboard(page))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"weekly_{date.today():%Y%m%d}.txt"
    dest.write_text(note + "\n", encoding="utf-8")
    print(note)
    print(f"\n[written to {dest}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
