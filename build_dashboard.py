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
import render
from render import cell_style, fmt, relative_scores, render_html, spark_cell, sparkline

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
def _select_price_column(raw: pd.DataFrame, yh: dict[str, str],
                         column: str) -> pd.DataFrame:
    """Pull one OHLC column out of a yf.download response, renamed back.

    yfinance wants BF-B where the universe lists BF.B, so callers download
    under the dashed form and this renames back to the input symbol. Also
    handles yfinance's single-symbol quirk: a lone ticker comes back as flat,
    unprefixed columns rather than a MultiIndex.
    """
    frame = (raw[column] if isinstance(raw.columns, pd.MultiIndex)
             else raw[[column]]).copy()
    if len(yh) == 1 and frame.shape[1] == 1:
        frame.columns = list(yh.values())
    out = pd.DataFrame(index=frame.index)
    for sym, ysym in yh.items():
        if ysym in frame.columns:
            out[sym] = frame[ysym]
    return out


def fetch_closes(symbols: list[str], period: str = HIST_PERIOD,
                 adjusted: bool = True) -> pd.DataFrame:
    """Daily closes for `symbols`, columns keyed by the input symbol.

    One batched call — no per-ticker throttling needed here, unlike the
    `.info` pulls in fetch_all().

    The dashboard asks for HIST_PERIOD; the history store asks for STORE_PERIOD.

    `adjusted` selects which of the two price bases the store keeps. True
    (the default) is split- and dividend-adjusted -- correct for returns and
    sparklines, which is what every caller of this function wants (the daily
    record path instead uses fetch_close_pair() below, so it never needs
    `adjusted=False` here). False asks yfinance for auto_adjust=False, whose
    `Close` is still split-adjusted -- only the dividend adjustment comes off.
    That is the basis a market cap needs: a dividend-adjusted price
    understates what the market actually paid, by an amount that compounds
    with yield. Measured at 2021-09-01, Close against Adj Close: VZ 54.94 vs
    40.05 (37.2%), IBM 133.17 vs 109.98 (21.1%), KO 56.69 vs 48.90 (15.9%),
    PG 143.84 vs 126.50 (13.7%), MSFT 301.83 vs 289.67 (4.2%). Every
    historical multiple built on the adjusted close would read that much too
    cheap, worst on exactly the income names where a P/E history is most
    often consulted. (Kept as a standalone mode for tools/backfill_close_raw.py,
    a one-time sweep that only ever needs this one series.)
    """
    if not symbols:
        return pd.DataFrame()
    yh = {s: s.replace(".", "-") for s in symbols}
    try:
        raw = yf.download(
            list(dict.fromkeys(yh.values())), period=period, interval="1d",
            auto_adjust=adjusted, progress=False, threads=True,
        )
    except Exception as e:
        print(f"  price history pull failed: {e}", file=sys.stderr)
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    return _select_price_column(raw, yh, "Close")


def fetch_close_pair(symbols: list[str],
                     period: str = HIST_PERIOD) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One auto_adjust=False download, yielding both price bases at once.

    Returns (adjusted, raw). `adjusted` is yfinance's `Adj Close`, which
    matches fetch_closes(..., adjusted=True) to within float32-rounding noise
    (re-verified here: max abs diff 4.6e-5 on KO/VZ/NVDA over 5 years, a
    relative difference of ~5e-7 -- far below the precision anything in this
    store is read at) and is what the `close` metric keeps holding. `raw` is
    `Close`, split-adjusted only, and is what `closeRaw` needs.

    Deriving both series from one response rather than issuing two downloads
    matters for two independent reasons:

    1. Correctness: two separate downloads can disagree on which tickers came
       back -- a transient failure on one call and not the other -- which
       would let `close` and `closeRaw` silently diverge in date coverage for
       the same name. One response makes that impossible: the two frames
       returned here always share the same index and the same columns.
    2. Cost: `yf.download(threads=True)` over the universe is the documented
       cause of this project's file-descriptor incident (README.md, "The
       file-descriptor limit is load-bearing") -- every worker thread holds
       an HTTPS socket *and* a SQLite connection to yfinance's own tz cache,
       and at launchd's default 256-file soft limit that cost the daily run a
       third of the universe, misreported as "possibly delisted" rather than
       as resource exhaustion. A second full download on every daily run
       would double exposure to that failure mode for no benefit, since one
       auto_adjust=False call already carries both series.
    """
    if not symbols:
        return pd.DataFrame(), pd.DataFrame()
    yh = {s: s.replace(".", "-") for s in symbols}
    try:
        raw = yf.download(
            list(dict.fromkeys(yh.values())), period=period, interval="1d",
            auto_adjust=False, progress=False, threads=True,
        )
    except Exception as e:
        print(f"  price history pull failed: {e}", file=sys.stderr)
        return pd.DataFrame(), pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    return (_select_price_column(raw, yh, "Adj Close"),
           _select_price_column(raw, yh, "Close"))


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


def record_history(df, closes, closes_raw, est_rows, as_of, failed, own=None):
    """Persist one day's observations. Never fatal to the dashboard build.

    `closes` and `closes_raw` come from one fetch_close_pair() call at the
    caller, not two separate fetch_closes() calls here -- see that function's
    docstring for why: it is both a correctness guarantee (the two series
    cannot end up with different ticker/date coverage) and a cost one (this
    daily run does not double its exposure to the file-descriptor failure
    mode documented in README.md).
    """
    conn = history.connect()
    try:
        history.ensure_schema(conn)
        run_id = history.start_run(conn)
        written = history.ingest_snapshot(conn, df, as_of)
        history.upsert_companies(conn, df)
        written += history.ingest_prices(conn, closes)
        # The unadjusted basis, for valuation only -- see fetch_close_pair()'s
        # docstring for the measured VZ/IBM/KO gap this exists to avoid.
        written += history.ingest_prices(conn, closes_raw, metric="closeRaw")
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
        closes, closes_raw = fetch_close_pair(df["ticker"].tolist(),
                                              period=STORE_PERIOD)

        record_history(df, closes, closes_raw, est_rows, as_of.isoformat(),
                       failed, own=(holds, ins))

        print("Fetching SPX snapshot...")
        spx = fetch_spx()

        prune_dated_files(as_of.isoformat())

    print("Fetching proxy-ETF benchmarks...")
    proxies = fetch_proxies(df)

    html = render.render_html(df, spx, proxies, METRICS, UNIVERSE, GROUP_LABELS,
                              DOMAINS)
    out = ROOT / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"\nWrote {out}  ({len(df)} companies)")


if __name__ == "__main__":
    main()
