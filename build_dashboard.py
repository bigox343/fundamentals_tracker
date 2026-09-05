"""Company-fundamentals dashboard for TMT, Industrials, and Consumer + an SPX section.

Pulls live fundamentals from Yahoo Finance (yfinance), computes relative heatmap
scores *within sub-industry* (semis vs semis, infra software vs infra software),
and renders a single self-contained HTML file you can open in a browser. Designed
to be rerun on a schedule (cron) — each run overwrites `dashboard.html` and drops
a dated raw CSV in `data/`.

Usage:
    python build_dashboard.py            # fetch live + rebuild dashboard.html
    python build_dashboard.py --no-fetch # rebuild from the newest cached CSV

Metrics (all groups): valuation, growth, profitability, balance-sheet/cash.
Heatmap coloring uses the validated colorblind-safe diverging blue<->red palette
(blue = more favorable than sector peers, red = less favorable). The 6M price
sparklines reuse that same pair for direction — green/red would fail protanope
separation (OKLab ΔE 2.9 vs the 8.0 target); blue/red clears it at 20.4.
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import math
import os
import sys
import time
import warnings
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

import edgar
import extract
import history

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# --------------------------------------------------------------------------- #
# Universe: ~18-20 bellwether names per sector. Edit these lists to taste.     #
# --------------------------------------------------------------------------- #
UNIVERSE: dict[str, dict[str, list[str]]] = {
    "TMT (Tech · Media · Telecom)": {
        "Semiconductors": [
            "NVDA", "AVGO", "AMD", "QCOM", "TXN", "INTC", "MU", "ADI",
            "NXPI", "MRVL", "ON", "MCHP", "ARM", "MPWR", "ALAB", "CRDO",
            "TSM",
        ],
        "Semicap Equipment": ["AMAT", "LRCX", "KLAC", "ASML", "TER", "ENTG"],
        "Software — Infrastructure": [
            "MSFT", "ORCL", "PANW", "CRWD", "FTNT", "DDOG", "SNOW", "MDB",
            "NET", "ZS",
        ],
        "Software — Applications": [
            "CRM", "ADBE", "NOW", "INTU", "WDAY", "TEAM", "HUBS", "VEEV",
            "ADSK", "SNPS", "CDNS",
        ],
        "Internet": ["GOOGL", "META", "SPOT", "PINS", "SNAP", "TTD", "RDDT"],
        "Media & Entertainment": [
            "NFLX", "DIS", "CMCSA", "WBD", "LYV", "EA", "TTWO", "RBLX",
        ],
        "Telecom": ["TMUS", "VZ", "T", "CHTR"],
        "Hardware & Networking": [
            "AAPL", "CSCO", "DELL", "SMCI", "ANET", "WDC", "STX", "NTAP",
            "MSI", "GLW", "COHR", "LITE", "CIEN",
        ],
        "IT Services": ["IBM", "ACN", "CTSH", "INFY", "EPAM"],
    },
    "Industrials": {
        "Aerospace & Defense": [
            "BA", "RTX", "LMT", "GD", "NOC", "LHX", "TDG", "HWM",
        ],
        "Machinery": ["CAT", "DE", "CMI", "PCAR", "ITW", "PH", "DOV"],
        "Transport & Logistics": ["UNP", "CSX", "NSC", "UPS", "FDX", "ODFL"],
        "Electrical Equipment": ["GE", "HON", "ETN", "EMR", "ROK", "AME"],
        # data-center power, thermal and grid buildout — kept out of Electrical
        # Equipment on purpose: scoring VRT's growth against EMR/ROK's would
        # tint two different businesses against each other
        "Data Center & Power": ["GEV", "VRT", "PWR", "NVT", "HUBB"],
        "Multi-Industrial": ["MMM", "CARR", "JCI", "IR", "TT", "OTIS",
                             "ROP", "FTV"],
    },
    "Consumer (Staples · Discretionary)": {
        "Internet Retail": ["AMZN", "BKNG", "ABNB", "DASH", "EBAY", "CHWY"],
        "Broadline & Specialty Retail": [
            "WMT", "COST", "TGT", "HD", "LOW", "TJX", "ROST",
        ],
        "Staples": ["PG", "KO", "PEP", "PM", "MO", "MDLZ", "CL", "KMB"],
        "Restaurants": ["MCD", "SBUX", "CMG", "YUM", "QSR", "DRI"],
        "Autos & Apparel": ["TSLA", "GM", "F", "NKE", "LULU"],
    },
}

# --------------------------------------------------------------------------- #
# Benchmark proxies: the liquid ETF that best stands in for each band.        #
# None -> no clean single-ETF proxy exists, so the band falls back to the      #
# equal-weight median return of its own tracked names (shown as "peer med").   #
# Keys are sector names first, then sub-industry names.                        #
# --------------------------------------------------------------------------- #
# ---------------------------------------------------------------------------
# The 13F fund roster
# ---------------------------------------------------------------------------
# CIKs are pinned rather than resolved by company-name search, because that
# search is not reliable for managers: "Balyasny" returns three entities that
# file no 13F at all, while the real filer (1218710) files under two different
# legal names. Worse, a wrong CIK does not error -- it returns perfectly
# parseable XML from the wrong book. Three of the first 28 resolved this way
# were wrong and looked fine: Baupost's /ADV shell (last 13F 2002), Marshall
# Wace *Asia* rather than LLP (2021), and Greenlight's pre-2024 entity, which
# Einhorn superseded with DME Capital Management.
#
# edgar.STALE_QUARTERS is the guard: a pinned CIK that falls more than two
# quarters behind is reported loudly rather than silently serving a stale book.
# Tybourne was dropped from this roster after winding down in 2025.
FUNDS: list[dict[str, str]] = [
    # Tiger lineage -- concentrated long/short equity
    {"name": "Tiger Global",    "cik": "0001167483", "cohort": "Tiger"},
    {"name": "Coatue",          "cik": "0001135730", "cohort": "Tiger"},
    {"name": "Lone Pine",       "cik": "0001061165", "cohort": "Tiger"},
    {"name": "Viking Global",   "cik": "0001103804", "cohort": "Tiger"},
    {"name": "Maverick",        "cik": "0000934639", "cohort": "Tiger"},
    {"name": "D1 Capital",      "cik": "0001747057", "cohort": "Tiger"},
    {"name": "Durable",         "cik": "0001798849", "cohort": "Tiger"},
    {"name": "Light Street",    "cik": "0001569049", "cohort": "Tiger"},
    {"name": "Whale Rock",      "cik": "0001387322", "cohort": "Tiger"},
    {"name": "Altimeter",       "cik": "0001541617", "cohort": "Tiger"},
    # Multi-strategy -- large books, much of which is hedging and market making
    {"name": "Citadel",         "cik": "0001423053", "cohort": "Multi-strat"},
    {"name": "Millennium",      "cik": "0001273087", "cohort": "Multi-strat"},
    {"name": "Point72",         "cik": "0001603466", "cohort": "Multi-strat"},
    {"name": "Balyasny",        "cik": "0001218710", "cohort": "Multi-strat"},
    {"name": "ExodusPoint",     "cik": "0001736225", "cohort": "Multi-strat"},
    {"name": "Schonfeld",       "cik": "0001665241", "cohort": "Multi-strat"},
    {"name": "Verition",        "cik": "0001454027", "cohort": "Multi-strat"},
    {"name": "Hudson Bay",      "cik": "0001393825", "cohort": "Multi-strat"},
    # Concentrated / activist / value
    {"name": "Pershing Square", "cik": "0001336528", "cohort": "Concentrated"},
    {"name": "Third Point",     "cik": "0001040273", "cohort": "Concentrated"},
    {"name": "Greenlight",      "cik": "0001489933", "cohort": "Concentrated"},
    {"name": "Appaloosa",       "cik": "0001656456", "cohort": "Concentrated"},
    {"name": "Baupost",         "cik": "0001061768", "cohort": "Concentrated"},
    {"name": "TCI",             "cik": "0001647251", "cohort": "Concentrated"},
    {"name": "Egerton",         "cik": "0001581811", "cohort": "Concentrated"},
    {"name": "Marshall Wace",   "cik": "0001318757", "cohort": "Concentrated"},
    {"name": "Soroban",         "cik": "0001517857", "cohort": "Concentrated"},
]

COHORTS: dict[str, str] = {f["name"]: f["cohort"] for f in FUNDS}

# How many quarters of 13F history to hold. Two years: enough to tell a fund
# building a position from one round-tripping it, which a single delta cannot.
THIRTEENF_QUARTERS = 8

# How stale the 13F sweep may get before EDGAR is asked again. A new quarter is
# still picked up the day its deadline passes, because that quarter has no rows
# at all and forces a sweep regardless. This interval governs only re-checks:
# amendments, which restate a quarter under a new accession, and managers who
# file late. Weekly keeps the steady state near zero requests without letting a
# restatement sit unnoticed for a quarter.
THIRTEENF_RECHECK_DAYS = 7

CUSIP_MAP_PATH = DATA_DIR / "cusip_map.csv"

# How many dated files of each kind to keep on disk. The observations
# themselves live in history.db, which is where every reader gets them; these
# files are the path back if the database is lost.
#
# Set to 1, the database stops being reconstructable and becomes the only copy
# of the perishable estimate history -- so the pruning below deletes a file
# only once the store confirms it holds that date, and `--rebuild-history`
# reports what it can no longer restore. Raise this to keep more.
#
# Not covered here, deliberately: data/13f_*.csv.gz and data/cusip_map.csv are
# not daily files (four a year, and one checked-in reference), and 13F cannot
# be refetched for a quarter whose filings have been amended away.
RETAIN_DATED = 1


PROXY_ETFS: dict[str, str | None] = {
    # sector level
    "TMT (Tech · Media · Telecom)": "XLK",
    "Industrials": "XLI",
    "Consumer (Staples · Discretionary)": "XLY",
    # TMT sub-industries
    "Semiconductors": "SMH",
    "Semicap Equipment": None,          # SOXX/SMH are broad semis, not semicap
    "Software — Infrastructure": "IGV",
    "Software — Applications": "IGV",
    "Internet": "FDN",
    "Media & Entertainment": "XLC",
    "Telecom": "IYZ",
    "Hardware & Networking": "IYW",
    "IT Services": None,
    # Industrials sub-industries
    "Aerospace & Defense": "ITA",
    "Machinery": "PAVE",
    "Transport & Logistics": "IYT",
    "Electrical Equipment": None,
    "Data Center & Power": "GRID",      # smart-grid/electrification infrastructure
    "Multi-Industrial": "XLI",
    # Consumer sub-industries
    "Internet Retail": "IBUY",
    "Broadline & Specialty Retail": "XRT",
    "Staples": "XLP",
    "Restaurants": "PEJ",
    "Autos & Apparel": None,            # CARZ misses the apparel half
}

HIST_PERIOD = "1y"     # how much daily history to pull (YTD/1Y need the full year)
STORE_PERIOD = "5y"    # how much daily history the history store keeps
SPARK_DAYS = 126       # trading days in the sparkline window (~6 months)
SPARK_POINTS = 40      # points actually plotted, downsampled from SPARK_DAYS
WIN_1M = 22            # trading days ~ 1 month

# --------------------------------------------------------------------------- #
# Metric spec: (yfinance key or derived), label, group, format, higher_better #
# higher_better=None -> no heatmap tint (identity / size columns).            #
# --------------------------------------------------------------------------- #
METRICS = [
    ("price",        "Price",         "id",     "usd",   None),
    ("spark",        "6M Trend",      "id",     "spark", None),
    ("marketCap",    "Mkt Cap",       "id",     "bigusd", None),
    ("forwardPE",    "Fwd P/E",       "val",    "x",     False),
    ("trailingPE",   "Trail P/E",     "val",    "x",     False),
    ("evEbitda",     "EV/EBITDA",     "val",    "x",     False),
    ("ps",           "P/S",           "val",    "x",     False),
    ("fcfYield",     "FCF Yld",       "val",    "pct",   True),
    ("revGrowth",    "Rev Gr",        "grow",   "pct",   True),
    ("epsGrowth",    "EPS Gr",        "grow",   "pct",   True),
    ("grossMargin",  "Gross %",       "prof",   "pct",   True),
    ("opMargin",     "Op %",          "prof",   "pct",   True),
    ("netMargin",    "Net %",         "prof",   "pct",   True),
    ("roe",          "ROE",           "prof",   "pct",   True),
    ("netDebtEbitda","ND/EBITDA",     "bal",    "x",     False),
    ("fcf",          "FCF",           "bal",    "bigusd", True),
    ("cash",         "Cash",          "bal",    "bigusd", None),
]

GROUP_LABELS = {
    "id": "", "val": "Valuation", "grow": "Growth",
    "prof": "Profitability", "bal": "Balance Sheet & Cash",
}


# --------------------------------------------------------------------------- #
# Data pull                                                                    #
# --------------------------------------------------------------------------- #
def _pct(x):
    return x * 100 if isinstance(x, (int, float)) and not math.isnan(x) else None


def market_cap(info: dict):
    """Resolve market cap across yfinance's inconsistent field names.

    Some tickers (CRM, HD, LOW, TGT observed) come back with only
    `nonDilutedMarketCap`; a few carry neither and must be rebuilt from shares
    x price. Without this, P/S and FCF yield go blank and the row sorts last.
    """
    for key in ("marketCap", "nonDilutedMarketCap"):
        v = info.get(key)
        if isinstance(v, (int, float)) and not math.isnan(v) and v > 0:
            return v
    shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if shares and price:
        return shares * price
    return None


def fetch_row(ticker: str) -> dict:
    """Pull one company's fundamentals, with a BF.B -> BF-B symbol fallback."""
    info = None
    # dedupe: tickers without a dot would otherwise be requested twice
    for sym in dict.fromkeys((ticker, ticker.replace(".", "-"))):
        try:
            probe = yf.Ticker(sym).info
        except Exception:
            continue
        if probe:
            info = probe
            if market_cap(probe):
                break
    if not info:
        return {"ticker": ticker, "name": ticker}

    ebitda = info.get("ebitda")
    debt = info.get("totalDebt")
    cash = info.get("totalCash")
    mcap = market_cap(info)
    fcf = info.get("freeCashflow")

    net_debt_ebitda = None
    if ebitda and debt is not None and cash is not None and ebitda != 0:
        net_debt_ebitda = (debt - cash) / ebitda

    fcf_yield = None
    if fcf and mcap:
        fcf_yield = _pct(fcf / mcap)

    # same tickers that hide marketCap also omit the precomputed P/S
    ps = info.get("priceToSalesTrailing12Months")
    if ps is None and mcap and info.get("totalRevenue"):
        ps = mcap / info["totalRevenue"]

    return {
        "ticker": ticker,
        "name": info.get("shortName") or ticker,
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "marketCap": mcap,
        "forwardPE": info.get("forwardPE"),
        "trailingPE": info.get("trailingPE"),
        "evEbitda": info.get("enterpriseToEbitda"),
        "ps": ps,
        "fcfYield": fcf_yield,
        "revGrowth": _pct(info.get("revenueGrowth")),
        "epsGrowth": _pct(info.get("earningsGrowth")),
        "grossMargin": _pct(info.get("grossMargins")),
        "opMargin": _pct(info.get("operatingMargins")),
        "netMargin": _pct(info.get("profitMargins")),
        "roe": _pct(info.get("returnOnEquity")),
        "netDebtEbitda": net_debt_ebitda,
        "fcf": fcf,
        "cash": cash,
    }


def fetch_all() -> pd.DataFrame:
    rows = []
    all_tk = [
        (sec, sub, tk)
        for sec, subs in UNIVERSE.items()
        for sub, tks in subs.items()
        for tk in tks
    ]
    for i, (sec, sub, tk) in enumerate(all_tk, 1):
        row = fetch_row(tk)
        row["sector"] = sec
        row["subindustry"] = sub
        rows.append(row)
        print(f"  [{i:>3}/{len(all_tk)}] {tk:<6} {sub}", flush=True)
        time.sleep(0.3)  # be polite to the endpoint
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Price history: one bulk download feeds both the per-ticker sparklines and    #
# the proxy-ETF band returns.                                                  #
# --------------------------------------------------------------------------- #
def fetch_closes(symbols: list[str], period: str = HIST_PERIOD) -> pd.DataFrame:
    """Adjusted daily closes for `symbols`, columns keyed by the input symbol.

    yfinance wants BF-B where the universe lists BF.B, so download under the
    dashed form and rename back. One batched call — no per-ticker throttling
    needed here, unlike the `.info` pulls in fetch_all().

    The dashboard asks for HIST_PERIOD; the history store asks for STORE_PERIOD.
    """
    if not symbols:
        return pd.DataFrame()
    yh = {s: s.replace(".", "-") for s in symbols}
    try:
        raw = yf.download(
            list(dict.fromkeys(yh.values())), period=period, interval="1d",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        print(f"  price history pull failed: {e}", file=sys.stderr)
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    close = (raw["Close"] if isinstance(raw.columns, pd.MultiIndex)
             else raw[["Close"]]).copy()
    # a lone symbol comes back as a single unnamed 'Close' column
    if len(yh) == 1 and close.shape[1] == 1:
        close.columns = list(yh.values())
    out = pd.DataFrame(index=close.index)
    for sym, ysym in yh.items():
        if ysym in close.columns:
            out[sym] = close[ysym]
    return out


def _ret(close: pd.Series, back: int):
    """Percent change over `back` trading days, or None if history is short."""
    if len(close) <= back:
        return None
    return (close.iloc[-1] / close.iloc[-1 - back] - 1) * 100


def series_stats(close: pd.Series) -> dict:
    """Sparkline payload + trailing returns for one price series."""
    close = pd.to_numeric(close, errors="coerce").dropna()
    if len(close) < 5:
        return {}
    win = close.iloc[-SPARK_DAYS:]
    # downsample evenly but always keep the first and last points, so the drawn
    # line starts and ends where the quoted 6M return does
    step = max(1, len(win) // SPARK_POINTS)
    pts = list(win.iloc[::step])
    if pts[-1] != win.iloc[-1]:
        pts.append(win.iloc[-1])

    # YTD needs a baseline that predates the year boundary — a series that only
    # starts mid-year (IPO, relisting, a name that stopped trading) has no
    # year-open to measure from, so report nothing rather than a partial window
    ytd = None
    year = close.index[-1].year
    jan = close[close.index >= f"{year}-01-01"]
    if len(jan) > 1 and close.index[0].year < year:
        ytd = (close.iloc[-1] / jan.iloc[0] - 1) * 100

    # measured off the *drawn* window's own endpoints, so the quoted 6M % and the
    # line the reader sees can never disagree. Withheld entirely when the name is
    # younger than the window (recent IPOs) rather than passing off a shorter
    # window as six months.
    ret6m = (win.iloc[-1] / win.iloc[0] - 1) * 100 if len(win) >= SPARK_DAYS else None
    return {
        "spark": ";".join(f"{v:.4g}" for v in pts),
        "ret1m": _ret(close, WIN_1M),
        "ret6m": ret6m,
        "retYtd": ytd,
    }


def attach_history(df: pd.DataFrame) -> pd.DataFrame:
    """Add spark / ret1m / ret6m / retYtd columns to the company frame."""
    closes = fetch_closes(sorted(df["ticker"].dropna().unique().tolist()))
    stats = {
        tk: series_stats(closes[tk]) for tk in closes.columns
    } if not closes.empty else {}
    missing = [t for t in df["ticker"] if not stats.get(t)]
    if missing:
        print(f"  no price history for: {', '.join(missing)}", file=sys.stderr)
    for col in ("spark", "ret1m", "ret6m", "retYtd"):
        df[col] = [stats.get(t, {}).get(col) for t in df["ticker"]]
    return df


def fetch_proxies(df: pd.DataFrame) -> dict:
    """Per-band benchmark: the proxy ETF's series, or a peer-median fallback.

    Returns {band_name: {proxy, spark, ret1m, ret6m, retYtd}} for every sector
    and sub-industry in PROXY_ETFS. Bands mapped to None — and any ETF whose
    download comes back empty — fall back to the median return of the band's
    own tracked names, labelled "peer med" so the source is never ambiguous.
    """
    etfs = sorted({v for v in PROXY_ETFS.values() if v})
    closes = fetch_closes(etfs)
    etf_stats = {
        tk: series_stats(closes[tk]) for tk in closes.columns
    } if not closes.empty else {}

    def peer_median(band: str) -> dict:
        sub = df[(df.get("subindustry") == band) | (df.get("sector") == band)]
        if sub.empty:
            return {}
        out = {"proxy": None}
        for col in ("ret1m", "ret6m", "retYtd"):
            if col in sub:
                v = pd.to_numeric(sub[col], errors="coerce").median()
                out[col] = None if pd.isna(v) else v
        return out

    proxies = {}
    for band, etf in PROXY_ETFS.items():
        s = etf_stats.get(etf) if etf else None
        proxies[band] = {"proxy": etf, **s} if s else peer_median(band)
    return proxies


def fetch_spx() -> dict:
    """Index-level snapshot: level + returns from ^GSPC, valuation via SPY proxy."""
    out = {"asof": datetime.now(timezone.utc)}
    try:
        hist = yf.Ticker("^GSPC").history(period="1y", auto_adjust=False)
        close = hist["Close"].dropna()
        last = close.iloc[-1]
        out["level"] = last
        out["ret_1d"] = (last / close.iloc[-2] - 1) * 100 if len(close) > 1 else None
        out["ret_1m"] = (last / close.iloc[-22] - 1) * 100 if len(close) > 22 else None
        out["ret_1y"] = (last / close.iloc[0] - 1) * 100
        yr_start = close[close.index >= f"{datetime.now().year}-01-01"]
        out["ret_ytd"] = (last / yr_start.iloc[0] - 1) * 100 if len(yr_start) else None
    except Exception as e:
        print(f"  ^GSPC pull failed: {e}", file=sys.stderr)
    try:
        spy = yf.Ticker("SPY").info
        out["fwd_pe"] = spy.get("forwardPE")
        out["trail_pe"] = spy.get("trailingPE")
        out["yield"] = _pct(spy.get("yield")) if spy.get("yield") else spy.get("dividendYield")
    except Exception as e:
        print(f"  SPY pull failed: {e}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #
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


def cell_style(score: float | None) -> str:
    """Diverging blue<->red tint. score in [-1,1]; +1 favorable (blue), -1 red."""
    if score is None:
        return ""
    a = min(0.34, abs(score) * 0.34)
    rgb = "37,106,191" if score > 0 else "208,59,59"  # seq-blue-500 / status-critical
    return f" style=\"background:rgba({rgb},{a:.3f})\""


def relative_scores(df: pd.DataFrame, key: str, higher_better: bool | None) -> dict:
    """Signed z-ish score per row within a sector, clipped to [-1,1]."""
    if higher_better is None:
        return {}
    col = pd.to_numeric(df[key], errors="coerce")
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


def render_sector(sector: str, df: pd.DataFrame, proxies: dict) -> str:
    gcount = {}
    for _k, _l, g, _f, _hb in METRICS:
        gcount[g] = gcount.get(g, 0) + 1

    # group header
    ghead = "<tr class='ghead'><th class='sticky'></th>"
    seen = []
    for _k, _l, g, _f, _hb in METRICS:
        if g not in seen:
            seen.append(g)
            lbl = GROUP_LABELS[g]
            cls = f" class='grp g-{g}'" if lbl else ""
            ghead += f"<th colspan='{gcount[g]}'{cls}>{lbl}</th>"
    ghead += "</tr>"

    colhead = ("<tr class='colhead'><th class='sticky left sortable' data-key='__name'>"
               "Company<span class='ind'></span></th>")
    for key, lab, g, _f, _hb in METRICS:
        colhead += f"<th class='g-{g} sortable' data-key='{key}'>{lab}<span class='ind'></span></th>"
    colhead += "</tr>"

    # ticker-indexed so sub-industry and sector scores can be looked up side by
    # side; the JS toggle swaps which basis paints the cell
    tdf = df.set_index("ticker", drop=False)
    sec_scores = {
        key: relative_scores(tdf, key, hb)
        for key, _l, _g, _f, hb in METRICS if hb is not None
    }

    ncols = len(METRICS) + 1
    body = ""
    # one band per sub-industry; peer scoring happens *within* the band so a
    # semi is never tinted against a telco
    for sub in UNIVERSE[sector]:
        sdf = tdf[tdf.subindustry == sub]
        if sdf.empty:
            continue
        sdf = sdf.sort_values("marketCap", ascending=False, na_position="last")
        sub_scores = {
            key: relative_scores(sdf, key, hb)
            for key, _l, _g, _f, hb in METRICS if hb is not None
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
            for key, _lab, g, f, _hb in METRICS:
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
                cell = spark_cell(r) if f == "spark" else fmt(v, f)
                body += f"<td class='num g-{g}'{attrs}{cell_style(ss)}>{cell}</td>"
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


def render_spx(spx: dict, df: pd.DataFrame, proxies: dict) -> str:
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
    for sector in UNIVERSE:
        secdf = df[df.sector == sector]
        if secdf.empty:
            continue
        rows += (f"<tr class='secrow'><td class='left'>{sector}</td>"
                 f"{med_cells(secdf)}{perf_cells(sector)}</tr>")
        for sub in UNIVERSE[sector]:
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
var KEYS=__KEYS__, LABELS=__LABELS__, STAMP=__STAMP__;
var $=function(s){return document.querySelector(s);};
var $$=function(s){return Array.prototype.slice.call(document.querySelectorAll(s));};
var st={tint:'ss',flat:false,q:'',groups:{val:1,grow:1,prof:1,bal:1}};

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
function applyTint(){
  $$('td.num').forEach(function(td){
    var v=st.tint==='off'?null:td.getAttribute(st.tint==='ss'?'data-ss':'data-sc');
    td.style.background=(v===null)?'':tintOf(parseFloat(v));
  });
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
measure();applyGroups();applyTint();applyFilter();
})();
"""


def build_js() -> str:
    keys = [k for k, _l, _g, _f, _hb in METRICS]
    labels = [l for _k, l, _g, _f, _hb in METRICS]
    return (JS_TMPL
            .replace("__KEYS__", json.dumps(keys))
            .replace("__LABELS__", json.dumps(labels))
            .replace("__STAMP__", json.dumps(datetime.now().strftime("%Y%m%d"))))


def render_html(df: pd.DataFrame, spx: dict, proxies: dict) -> str:
    asof = spx.get("asof", datetime.now(timezone.utc))
    asof_s = asof.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    sectors = "".join(
        render_sector(sec, df[df.sector == sec], proxies) for sec in UNIVERSE
    )
    spx_html = render_spx(spx, df, proxies)
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
<footer>
  Generated by <code>build_dashboard.py</code>. Fundamentals from Yahoo Finance via yfinance —
  figures are consensus/TTM as reported by the source and may lag or contain gaps.
  Sparklines and all returns use split/dividend-adjusted daily closes over the trailing
  ~6 months (1M = 22 trading days, 6M = 126, YTD from Jan 1); proxy ETFs are stand-ins
  for each band, not the band itself. Valuation multiples move with price; treat as a
  directional screen, not investment advice.
</footer>
</div>
<script>{build_js()}</script>
</body></html>"""


# --------------------------------------------------------------------------- #
LOCK_PATH = ROOT / ".run.lock"


@contextmanager
def single_instance():
    """Refuse to run twice at once.

    A daily LaunchAgent can fire while a slow run is still going. macOS has no
    flock(1), so the lock is taken here with fcntl; the OS releases it when the
    process exits, which means no stale lock file to clean up.
    """
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print("Another run is in progress; exiting.")
        sys.exit(0)
    try:
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def record_history(df, closes, est_rows, as_of, failed, own=None):
    """Persist one day's observations. Never fatal to the dashboard build."""
    conn = history.connect()
    try:
        history.ensure_schema(conn)
        run_id = history.start_run(conn)
        written = history.ingest_snapshot(conn, df, as_of)
        history.upsert_companies(conn, df)
        written += history.ingest_prices(conn, closes)
        written += history.upsert_rows(conn, est_rows)
        if own:
            holds, ins = own
            written += history.upsert_holdings(conn, holds)
            written += history.upsert_insiders(conn, ins)
        closes_ok = history.latest_close_coverage(closes)
        history.finish_run(
            conn, run_id, "ok",
            tickers_ok=len(df) - len(failed), tickers_failed=len(failed),
            closes_ok=closes_ok, closes_expected=len(df),
        )
        print(f"History: {written} rows written for {as_of} "
              f"({closes_ok}/{len(df)} closes)")
        if closes_ok < history.PARTIAL_CLOSE_RATIO * len(df):
            print(f"  WARNING: only {closes_ok} of {len(df)} closes landed; "
                  f"run recorded as partial", file=sys.stderr)
    finally:
        conn.close()


def fetch_13f(force: bool = False) -> None:
    """Pull any 13F filing the store is missing, then ingest it.

    Almost always a no-op. A 13F lands 45 days after quarter end and does not
    change again, so the store already holds every wanted filing on roughly 85
    of every 90 days -- `edgar.collect_13f` compares accession numbers and makes
    no request at all when nothing is new. That is what makes it safe to hang a
    quarterly dataset off a daily run.
    """
    cusip_map = edgar.load_cusip_map(CUSIP_MAP_PATH)
    if not cusip_map:
        print("No data/cusip_map.csv -- skipping 13F "
              "(build it with tools/build_cusip_map.py)")
        return
    quarters = edgar.quarter_ends(THIRTEENF_QUARTERS)
    try:
        conn = history.connect()
        history.ensure_schema(conn)
        already = {} if force else history.ingested_filings(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"13F skipped, store unavailable: {exc}")
        return

    seen = history.seen_filings(conn)
    unseen = [(f, q) for f in FUNDS for q in quarters if (f["cik"], q) not in seen]

    if not unseen and not force:
        # Nothing new is possible today, so only the periodic re-check applies.
        stamp = history.last_sweep(conn)
        if stamp:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(stamp)).days
            if age < THIRTEENF_RECHECK_DAYS:
                print(f"13F current: {len(FUNDS)} funds x {len(quarters)} "
                      f"quarters, last checked {age}d ago.")
                conn.close()
                return

    missing = len(unseen)
    print(f"Fetching 13F filings ({missing} unseen of "
          f"{len(FUNDS) * len(quarters)})...")
    rows, filings, raw, stale, failed, drift = edgar.collect_13f(
        FUNDS, quarters, cusip_map, already)

    by_quarter: dict[str, list[dict]] = {}
    for rec in raw:
        by_quarter.setdefault(rec["quarter"], []).append(rec)
    for quarter, records in by_quarter.items():
        year, month, _ = quarter.split("-")
        tag = f"{year}Q{(int(month) - 1) // 3 + 1}"
        edgar.write_raw_archive(records, DATA_DIR / f"13f_{tag}.csv.gz")
    edgar.write_filings_manifest(filings, DATA_DIR / edgar.FILINGS_MANIFEST)

    try:
        for filing in filings:
            if filing.status == "ok":
                history.replace_quarter(conn, filing.cik, filing.quarter)
        written = history.upsert_thirteenf(conn, rows)
        history.upsert_filings(conn, filings)
        print(f"  stored {written} universe positions from "
              f"{sum(1 for f in filings if f.status == 'ok')} filings")
    except Exception as exc:  # noqa: BLE001 - the dashboard must still render
        print(f"  13F store write failed: {exc}")
    finally:
        conn.close()

    # Loud, because the failure mode this catches is silent: a superseded CIK
    # returns valid XML from the wrong book rather than an error.
    for line in stale:
        print(f"  STALE CIK -- {line}")
    if failed:
        print(f"  13F fetch failed for: {', '.join(failed)}")
    for n_filers, cusip, issuer in drift[:5]:
        if n_filers >= 5:
            print(f"  UNMAPPED CUSIP -- {cusip} {issuer!r} held by {n_filers} "
                  f"managers; rebuild with tools/build_cusip_map.py")


def rebuild_13f() -> int:
    """Re-ingest 13F from the raw archives with no network."""
    cusip_map = edgar.load_cusip_map(CUSIP_MAP_PATH)
    if not cusip_map:
        print("No data/cusip_map.csv -- run tools/build_cusip_map.py first")
        return 1
    paths = [p for p in sorted(DATA_DIR.glob("13f_*.csv.gz"))
             if p.name != edgar.FILINGS_MANIFEST]
    if not paths:
        print("No 13f_*.csv.gz archives found")
        return 1
    rows, filings = edgar.ingest_archives(
        paths, cusip_map, DATA_DIR / edgar.FILINGS_MANIFEST)
    conn = history.connect()
    history.ensure_schema(conn)
    conn.execute("DELETE FROM thirteenf")
    conn.execute("DELETE FROM thirteenf_filings")
    conn.commit()
    written = history.upsert_thirteenf(conn, rows)
    history.upsert_filings(conn, filings)
    conn.close()
    print(f"Rebuilt 13F from {len(paths)} archives: {written} universe "
          f"positions across {len(filings)} filings")
    return 0


def prune_dated_files(as_of: str) -> None:
    """Keep only the newest RETAIN_DATED dated files of each kind.

    Runs after the store has been written, and only removes a file whose date
    the store confirms it holds -- so a run that fetched but failed to record
    cannot have its evidence deleted by the next one.
    """
    if RETAIN_DATED <= 0:
        return
    try:
        conn = history.connect()
        ingested = {
            "fundamentals": {r[0] for r in conn.execute(
                "SELECT DISTINCT as_of FROM metrics WHERE period_type = 'snapshot'")},
            "estimates": {r[0] for r in conn.execute(
                "SELECT DISTINCT as_of FROM metrics WHERE period_type = 'estimate'")},
        }
        conn.close()
    except Exception as exc:  # noqa: BLE001 - never let housekeeping fail a run
        print(f"  prune skipped, store unavailable: {exc}")
        return

    removed = 0
    for kind in ("fundamentals", "estimates"):
        removed += len(extract.prune_dated(
            DATA_DIR, f"{kind}_*.csv.gz", RETAIN_DATED, ingested[kind]))
    # Holder lists and insider filings are current-state snapshots that can be
    # refetched, so neither needs the ingestion guard. The rendered reports are
    # pruned by their own writers, which run after this one.
    for pattern in ("holdings_*.csv.gz", "insiders_*.csv.gz"):
        removed += len(extract.prune_dated(DATA_DIR, pattern, RETAIN_DATED))
    if removed:
        print(f"Pruned {removed} dated files, keeping the newest "
              f"{RETAIN_DATED} of each.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true",
                    help="rebuild from newest cached CSV instead of pulling live")
    ap.add_argument("--rebuild-13f", action="store_true",
                    help="re-ingest 13F from data/13f_*.csv.gz, no network")
    ap.add_argument("--rebuild-history", action="store_true",
                    help="drop history.db and rebuild it from the raw CSVs")
    args = ap.parse_args()

    if args.rebuild_13f:
        return rebuild_13f()

    if args.rebuild_history:
        conn = history.connect()
        try:
            report = history.rebuild(conn, DATA_DIR)
        finally:
            conn.close()
        print(f"Rebuilt history: {report['rows']} rows from "
              f"{report['snapshots']} snapshots and "
              f"{report['estimates']} estimate files")
        if RETAIN_DATED and report["estimates"] <= RETAIN_DATED:
            print(f"  only {report['estimates']} day(s) of estimates restored: "
                  f"RETAIN_DATED={RETAIN_DATED} prunes the rest after ingest, so "
                  f"a rebuild recovers the retained days and not the history "
                  f"before them. Raise RETAIN_DATED to keep more.")
        if not report["prices"]:
            print("  no daily closes restored — they live only in the store, "
                  "being refetchable. The next normal run repopulates them.")
        return

    with single_instance():
        _run(args)


def _run(args):
    if args.no_fetch:
        caches = [str(p) for p in history._dated(DATA_DIR, "fundamentals")]
        if not caches:
            sys.exit("No cached CSV found; run without --no-fetch first.")
        df = extract.read_csv_any(caches[-1])
        if "subindustry" not in df.columns:
            sys.exit(
                f"Cache {caches[-1]} predates sub-industry grouping; "
                "rerun without --no-fetch to refresh."
            )
        print(f"Loaded cache {caches[-1]}")
        if "spark" not in df.columns:
            # cache predates sparklines; history is one cheap bulk call, so
            # backfill it rather than rendering a column of dashes
            print("Cache has no price history; pulling it live...")
            df = attach_history(df)
        spx = fetch_spx()
    else:
        print("Fetching fundamentals...")
        df = fetch_all()
        print("Fetching price history...")
        df = attach_history(df)
        stamp = datetime.now().strftime("%Y%m%d")
        df.to_csv(extract.csv_path(DATA_DIR / f"fundamentals_{stamp}.csv"),
                  index=False, compression="gzip")

        as_of = date.today()
        print("Fetching analyst estimates...")
        est_rows, failed = extract.collect_estimates(df["ticker"].tolist(), as_of)
        extract.write_estimates_csv(est_rows, DATA_DIR / f"estimates_{stamp}.csv")

        print("Fetching ownership (top institutional and fund holders, "
              "insider filings)...")
        holds, ins, own_failed = extract.collect_ownership(df["ticker"].tolist())
        extract.write_rows_csv(holds, extract.HOLDING_COLUMNS,
                               DATA_DIR / f"holdings_{stamp}.csv")
        extract.write_rows_csv(ins, extract.INSIDER_COLUMNS,
                               DATA_DIR / f"insiders_{stamp}.csv")

        fetch_13f()

        print("Fetching 5y closes for the history store...")
        closes = fetch_closes(df["ticker"].tolist(), period=STORE_PERIOD)

        record_history(df, closes, est_rows, as_of.isoformat(), failed,
                       own=(holds, ins))

        print("Fetching SPX snapshot...")
        spx = fetch_spx()

        prune_dated_files(as_of.isoformat())

    print("Fetching proxy-ETF benchmarks...")
    proxies = fetch_proxies(df)

    html = render_html(df, spx, proxies)
    out = ROOT / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"\nWrote {out}  ({len(df)} companies)")


if __name__ == "__main__":
    main()
