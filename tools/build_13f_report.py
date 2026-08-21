"""Render one quarter's 13F positioning as a standalone HTML report.

Everything the page states is derived from the store rather than written in:
the quarter, the filing deadline, which managers did not file, and which names
needed a split adjustment. A report that hardcoded any of those would quietly
describe the wrong quarter the moment a new one landed.

Usage:  python3 tools/build_13f_report.py [--quarter YYYY-MM-DD] [--out DIR]
        (with no --quarter, renders the newest quarter in the store)
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import edgar     # noqa: E402
import history   # noqa: E402

TEMPLATE = Path(__file__).parent / "13f_report.template.html"
CONVICTION_FLOOR = 0.06
MAX_CROWD, MAX_LIST = 22, 10


def usd(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:,.1f}B"
    if a >= 1e6:
        return f"${v/1e6:,.0f}M"
    return f"${v:,.0f}"


def num(v: float) -> str:
    a, sign = abs(v), "-" if v < 0 else ""
    if a >= 1e6:
        return f"{sign}{a/1e6:,.1f}M"
    if a >= 1e3:
        return f"{sign}{a/1e3:,.0f}K"
    return f"{sign}{a:,.0f}"


def esc(t) -> str:
    return html.escape(str(t))


def quarter_label(q: str) -> str:
    y, m, _ = q.split("-")
    return f"Q{(int(m) - 1) // 3 + 1} {y}"


def quarter_tag(q: str) -> str:
    y, m, _ = q.split("-")
    return f"{y}q{(int(m) - 1) // 3 + 1}"


def pretty_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b} {d.year}"


def describe_splits(conn, quarter: str, prev: str | None) -> str:
    """State which names needed a split adjustment, or that none did.

    Derived rather than written down: a hardcoded example describes the wrong
    quarter the moment a new one lands.
    """
    lead = "<b>Share counts are split-adjusted before differencing.</b> "
    splits = history.split_ratios(conn, quarter, prev) if prev else {}
    if not splits:
        return (lead + "No name in the universe split this quarter, so no "
                "adjustment was applied.")
    parts = []
    for ticker, ratio in sorted(splits.items()):
        ratio = float(ratio)
        parts.append(f"{ticker} {ratio:g}-for-1" if ratio >= 1
                     else f"{ticker} 1-for-{1/ratio:g}")
    worst = max(float(r) for r in splits.values())
    tail = (f"compared as filed, an untouched position would read as adding "
            f"{(worst - 1) * 100:,.0f}%." if worst > 1 else
            "compared as filed, an untouched position would read as a cut.")
    return f"{lead}{'; '.join(parts)} inside this window; {tail}"


def describe_absent(conn, quarter: str, prev: str | None, n_exit: int) -> str:
    """State which managers did not file, and how many exits are therefore real."""
    lead = "<b>An absent position is an exit only when the manager filed.</b> "
    absent = [r[0] for r in conn.execute(
        "SELECT fund FROM thirteenf_filings WHERE quarter = ? AND status = 'no-filing'",
        (quarter,))]
    if not absent:
        return (f"{lead}Every manager filed for {quarter_label(quarter)}, so "
                f"all {n_exit} exits are real.")
    held_prev = 0
    if prev:
        marks = ",".join("?" * len(absent))
        held_prev = conn.execute(
            f"SELECT COUNT(*) FROM thirteenf t JOIN thirteenf_filings f "
            f"ON t.cik = f.cik AND t.quarter = f.quarter "
            f"WHERE f.quarter = ? AND f.fund IN ({marks})",
            (prev, *absent)).fetchone()[0]
    return (f"{lead}{' and '.join(esc(a) for a in absent)} had not filed for "
            f"{quarter_label(quarter)}, so {held_prev} prior position"
            f"{'s' if held_prev != 1 else ''} are reported as unknown rather "
            f"than as fabricated exits. The other {n_exit} exits are real.")


def build(conn, quarter: str | None, out_dir: Path) -> Path:
    quarters = history.thirteenf_quarters(conn)
    if not quarters:
        raise SystemExit("no 13F filings in the store")
    quarter = quarter or quarters[0]
    if quarter not in quarters:
        raise SystemExit(f"{quarter} not ingested; have {', '.join(quarters)}")
    prev = edgar.previous_quarter(quarter)
    prev = prev if prev in quarters else None

    changes = history.thirteenf_changes(conn, quarter, prev)
    meta = pd.read_sql_query(
        "SELECT cik, fund, cohort, book_value, n_positions, status "
        "FROM thirteenf_filings WHERE quarter = ?", conn,
        params=[quarter]).set_index("cik")
    names = pd.read_sql_query(
        "SELECT ticker, name FROM companies", conn).set_index("ticker")["name"]
    changes["cohort"] = changes.cik.map(meta.cohort)
    held = changes[changes.shares > 0]
    n_funds = int(meta[meta.status == "ok"].shape[0])

    # -- crowding ----------------------------------------------------------
    crowd = held.groupby("ticker").agg(
        funds=("fund", "nunique"), value=("value", "sum")).reset_index()
    crowd["net"] = crowd.ticker.map(changes.groupby("ticker").delta_shares.sum())
    crowd["adds"] = crowd.ticker.map(
        changes[changes.action.isin(["ADD", "NEW"])].groupby("ticker").size()).fillna(0)
    crowd["cuts"] = crowd.ticker.map(
        changes[changes.action.isin(["TRIM", "EXIT"])].groupby("ticker").size()).fillna(0)
    crowd["name"] = crowd.ticker.map(names)
    crowd = crowd.sort_values(["funds", "value"], ascending=False).head(MAX_CROWD)

    crowd_rows = ""
    for r in crowd.itertuples():
        tone = "pos" if r.net > 0 else "neg" if r.net < 0 else "flat"
        crowd_rows += (
            f'<tr><td class="tk">{esc(r.ticker)}</td>'
            f'<td class="nm">{esc(r.name or "")}</td>'
            f'<td class="n"><span class="cnt">{r.funds}</span>'
            f'<span class="track"><i style="width:{min(100, r.funds/max(n_funds,1)*100):.0f}%"></i></span></td>'
            f'<td class="n mono">{usd(r.value)}</td>'
            f'<td class="n mono {tone}">{"+" if r.net > 0 else ""}{num(r.net)}</td>'
            f'<td class="n"><span class="split"><b class="a">{int(r.adds)}</b>'
            f'<s></s><b class="c">{int(r.cuts)}</b></span></td></tr>')

    # -- manager books -----------------------------------------------------
    cohort_class = {"Tiger": "t", "Multi-strat": "m", "Concentrated": "c"}
    funds = []
    for cik, g in changes.groupby("cik"):
        if cik not in meta.index:
            continue
        m, h = meta.loc[cik], g[g.shares > 0]
        if not len(h):
            continue
        funds.append(dict(
            fund=m.fund, cohort=m.cohort or "", n=len(h), uni=float(h.value.sum()),
            book=float(m.book_value or 0), filed=int(m.n_positions or 0),
            top=float(h.pct_of_book.max()) if h.pct_of_book.notna().any() else 0.0,
            topt=h.loc[h.pct_of_book.idxmax(), "ticker"] if h.pct_of_book.notna().any() else "",
            adds=int(g.action.isin(["ADD", "NEW"]).sum()),
            cuts=int(g.action.isin(["TRIM", "EXIT"]).sum())))
    funds.sort(key=lambda f: -f["uni"])
    # Scale the top-position bar to the largest one on the page, so the column
    # compares managers against each other rather than an arbitrary ceiling.
    top_max = max((f["top"] for f in funds), default=0.01) or 0.01

    fund_rows = ""
    for f in funds:
        fund_rows += (
            f'<tr><td class="nm b">{esc(f["fund"])}</td>'
            f'<td><span class="chip {cohort_class.get(f["cohort"], "c")}">'
            f'{esc(f["cohort"])}</span></td>'
            f'<td class="n mono">{usd(f["book"])}</td>'
            f'<td class="n mono dim">{f["filed"]:,}</td>'
            f'<td class="n mono">{f["n"]}</td>'
            f'<td class="n mono">{usd(f["uni"])}</td>'
            f'<td class="n"><span class="bookbar">'
            f'<i style="width:{f["top"]/top_max*100:.0f}%"></i></span>'
            f'<span class="mono pct">{f["top"]*100:.1f}%</span> '
            f'<span class="tk sm">{esc(f["topt"])}</span></td>'
            f'<td class="n"><span class="split"><b class="a">{f["adds"]}</b>'
            f'<s></s><b class="c">{f["cuts"]}</b></span></td></tr>')

    # -- signal lists ------------------------------------------------------
    conv = held[held.pct_of_book > CONVICTION_FLOOR].sort_values(
        "pct_of_book", ascending=False).head(14)
    conv_max = float(conv.pct_of_book.max()) if len(conv) else 1.0
    conv_rows = "".join(
        f'<li><span class="tk">{esc(r.ticker)}</span>'
        f'<span class="cf">{esc(r.fund)}</span>'
        f'<span class="cb"><i style="width:{r.pct_of_book/conv_max*100:.0f}%"></i></span>'
        f'<span class="mono cp">{r.pct_of_book*100:.1f}%</span></li>'
        for r in conv.itertuples())

    new = changes[changes.action == "NEW"].sort_values(
        "value", ascending=False).head(MAX_LIST)
    new_rows = "".join(
        f'<li><span class="tk">{esc(r.ticker)}</span>'
        f'<span class="cf">{esc(r.fund)}</span>'
        f'<span class="mono cp pos">{usd(r.value)}</span></li>'
        for r in new.itertuples())

    ex = changes[changes.action == "EXIT"].sort_values(
        "prev_shares", ascending=False).head(MAX_LIST)
    ex_rows = "".join(
        f'<li><span class="tk">{esc(r.ticker)}</span>'
        f'<span class="cf">{esc(r.fund)}</span>'
        f'<span class="mono cp neg">{num(r.prev_shares)} sh</span></li>'
        for r in ex.itertuples())

    split_note = describe_splits(conn, quarter, prev)
    n_exit = int((changes.action == "EXIT").sum())
    absent_note = describe_absent(conn, quarter, prev, n_exit)

    cusip_map = ROOT / "data" / "cusip_map.csv"
    n_map = len(pd.read_csv(cusip_map)) if cusip_map.exists() else 0
    n_names = int(pd.read_sql_query(
        "SELECT COUNT(*) n FROM companies", conn)["n"].iloc[0])

    filled = (TEMPLATE.read_text()
        .replace("{{CROWD}}", crowd_rows).replace("{{FUNDS}}", fund_rows)
        .replace("{{CONV}}", conv_rows).replace("{{NEW}}", new_rows)
        .replace("{{EXIT}}", ex_rows)
        .replace("{{QLABEL}}", quarter_label(quarter))
        .replace("{{QEND}}", pretty_date(quarter))
        .replace("{{QDEADLINE}}", pretty_date(
            (date.fromisoformat(quarter) + timedelta(days=edgar.FILING_LAG_DAYS)).isoformat()))
        .replace("{{QPREV}}", quarter_label(prev) if prev else "no prior quarter")
        .replace("{{NFUNDS}}", str(n_funds))
        .replace("{{NNAMES}}", str(n_names))
        .replace("{{NHELD}}", str(int(held.ticker.nunique())))
        .replace("{{NPOS}}", f"{len(changes):,}")
        .replace("{{NNEW}}", str(int((changes.action == "NEW").sum())))
        .replace("{{NADD}}", str(int((changes.action == "ADD").sum())))
        .replace("{{NTRIM}}", str(int((changes.action == "TRIM").sum())))
        .replace("{{NEXIT}}", str(n_exit))
        .replace("{{TVALUE}}", usd(float(held.value.sum())))
        .replace("{{SPLITNOTE}}", split_note)
        .replace("{{ABSENTNOTE}}", absent_note)
        .replace("{{NMAP}}", str(n_map)))

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"13f_{quarter_tag(quarter)}.html"
    out.write_text(filled, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter", help="YYYY-MM-DD quarter end (default: newest)")
    ap.add_argument("--out", default=str(ROOT / "reports"))
    args = ap.parse_args()
    conn = history.connect()
    out = build(conn, args.quarter, Path(args.out))
    print(f"Wrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
