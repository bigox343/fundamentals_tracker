"""Solve today's book, record it, and render it as a standalone page.

Fetches nothing. Solving and rendering are separate functions so a rendering
change cannot alter what was recorded.
"""
from __future__ import annotations

import html as _html
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import factors           # noqa: E402
import history           # noqa: E402
import portfolio         # noqa: E402

REPORTS = ROOT / "reports"


def solve_today(conn, as_of: str):
    """Build the alpha composite and solve. Returns (alpha frame, solution)."""
    factor_returns = factors.load_cached()
    companies = pd.read_sql_query(
        "SELECT ticker, sector, subindustry FROM companies", conn)
    sectors = companies.set_index("ticker")["sector"]
    subind = companies.set_index("ticker")["subindustry"]

    rets = portfolio.returns_matrix(conn, as_of)
    universe = portfolio.eligible_universe(rets)
    rm = portfolio.estimate_risk(rets[universe], factor_returns, sectors)
    alpha = portfolio.build_alpha(conn, as_of, rm.tickers, subind)
    solution = portfolio.solve(alpha["mu"], rm, sectors, subind)
    return alpha, solution


def record(conn, as_of: str, alpha: pd.DataFrame,
           solution: portfolio.Solution) -> int:
    rows = [history.TargetWeightRow(
        as_of, ticker, float(weight),
        float(alpha.loc[ticker, "mu"]),
        float(alpha.loc[ticker, "contrib_momentum"]),
        float(alpha.loc[ticker, "contrib_13f"]),
        float(alpha.loc[ticker, "contrib_insider"]),
        solution.status)
        for ticker, weight in solution.weights.items()]
    return history.upsert_target_weights(conn, rows)


def render(as_of: str, alpha: pd.DataFrame,
           solution: portfolio.Solution) -> Path:
    book = (pd.DataFrame({"weight": solution.weights})
              .join(alpha).sort_values("weight", ascending=False))
    longs = book[book["weight"] > 1e-6]
    shorts = book[book["weight"] < -1e-6].sort_values("weight")

    def table(frame: pd.DataFrame) -> str:
        rows = "".join(
            f"<tr><td>{_html.escape(str(t))}</td><td>{r.weight:+.2%}</td>"
            f"<td>{r.mu:+.2f}</td><td>{r.contrib_momentum:+.2f}</td>"
            f"<td>{r.contrib_13f:+.2f}</td><td>{r.contrib_insider:+.2f}</td></tr>"
            for t, r in frame.iterrows())
        return ("<table><tr><th>Ticker</th><th>Weight</th><th>mu</th>"
                "<th>Mom</th><th>13F</th><th>Insider</th></tr>"
                f"{rows}</table>")

    gross = solution.weights.abs().sum()
    net = solution.weights.sum()
    html = f"""<!doctype html><meta charset="utf-8">
<title>Portfolio {as_of}</title>
<style>body{{font:14px system-ui,sans-serif;margin:2rem;max-width:60rem}}
table{{border-collapse:collapse;margin:1rem 0}}
td,th{{border:1px solid #ccc;padding:.3rem .6rem;text-align:right}}
td:first-child,th:first-child{{text-align:left}}
.note{{color:#666;font-size:13px;border-left:3px solid #ddd;padding-left:.8rem}}</style>
<h1>Market-neutral book &mdash; {as_of}</h1>
<p>Status <b>{_html.escape(solution.status)}</b> &middot;
gross {gross:.2f} &middot; net {net:+.6f} &middot;
{len(longs)} long / {len(shorts)} short</p>
<p class="note">Dollar-, sector- and factor-neutral against FF5+UMD and two
sector factors. Alpha is an equal-weight blend of 12-1 momentum, 13F
positioning change and insider purchases, z-scored within sub-industry
(insider leg universe-wide). Weights are a research output, not advice.</p>
<h2>Longs</h2>{table(longs)}
<h2>Shorts</h2>{table(shorts)}
"""
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"portfolio_{as_of.replace('-', '')}.html"
    path.write_text(html)
    # The writer owns its own retention. Pruning these from the daily run
    # instead would run before this file is written, leaving one more on disk
    # than RETAIN_DATED asks for.
    import build_dashboard
    import extract
    extract.prune_dated(REPORTS, "portfolio_*.html", build_dashboard.RETAIN_DATED)
    return path


if __name__ == "__main__":
    as_of = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    conn = history.connect()
    # connect() does not create tables. An existing store predates the
    # target_weights table, and CREATE TABLE IF NOT EXISTS makes this a no-op
    # everywhere else -- matching how build_dashboard.py opens the store.
    history.ensure_schema(conn)
    alpha, solution = solve_today(conn, as_of)
    print(f"recorded {record(conn, as_of, alpha, solution)} weights "
          f"({solution.status})")
    print(f"wrote {render(as_of, alpha, solution)}")
