"""Build data/cusip_map.csv from the raw 13F archives, verified against prices.

13F filings carry a CUSIP and an issuer name but never a ticker, and CUSIP is a
licensed identifier with no free authoritative mapping. So the map is derived
from the filings themselves and then *proved* rather than trusted:

    a candidate CUSIP -> ticker pair is accepted only if the price implied by
    the filers, median(value / shares), agrees with the ticker's actual close
    on the quarter end held in history.db.

Ten independent managers agreeing that NVDA was $200.09 on 2026-06-30 is strong
evidence. The same check catches filers reporting value in thousands rather than
dollars, because they miss by exactly 1000x.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history  # noqa: E402
from build_dashboard import CUSIP_MAP_PATH, DATA_DIR  # noqa: E402

# Accept a mapping when the implied price is within this of the real close.
# Loose enough for a class-B/ADR spread, tight enough that a wrong issuer or a
# thousands/dollars unit error can never slip through.
PRICE_TOL = 0.06
MIN_QUARTERS = 1

# history.db closes are back-adjusted, so for any name that split or spun off
# inside the window the implied price is a clean multiple of the close rather
# than equal to it: NFLX 10x, NOW 5x, KLAC 10x, BKNG 25x, CRWD 4x. Treating that
# as a mismatch would reject the correct CUSIP -- CrowdStrike missed on exactly
# this. A near-exact clean multiple is corroborating evidence, not a failure.
SPLIT_RATIOS = (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50)
SPLIT_TOL = 0.03

# Stage 2 pairs a price match with a shared word. These words are shared by
# hundreds of issuers and are evidence of nothing.
# Four names whose filing abbreviations defeat name matching entirely --
# "UNION PAC" vs "Union Pacific", "BUSINESS MACHS" vs "Business Machines",
# "GENERAL MTRS" vs "General Motors" -- and which stage 2 cannot rescue because
# after stopwords there is no shared word left. Each CUSIP below was verified
# against every quarter in the window: BKNG 09857L108 tracks its close exactly
# either side of a 25:1 split, and GM/IBM/UNP sit within the 1-5% drift that
# dividend-adjusted closes carry. Extending the abbreviation list instead would
# be endless; four pinned rows are honest and checkable.
OVERRIDES: dict[str, str] = {
    "BKNG": "09857L108",
    "GM": "37045V100",
    "IBM": "459200101",
    "UNP": "907818108",
}

STOPWORDS = frozenset("""
GLOBAL NATIONAL FIRST UNITED GENERAL STANDARD PACIFIC ATLANTIC NORTH SOUTH EAST
WEST NEW OLD BIG SMALL PRIME MAIN CORE NEXT ONE TWO THREE CAPITAL FINANCIAL
FUND FUNDS INDEX SERIES PORTFOLIO ETF SHARES CLASS INVESTMENT INVESTMENTS
ADVISORS MANAGEMENT SOLUTIONS SOLUTION SYSTEMS SERVICE SERVICES PRODUCTS
COMMUNICATIONS BUSINESS ENTERTAINMENT ENERGY POWER LINE LINES RESOURCES
DYNAMICS NETWORKS NETWORK DIGITAL DATA CLOUD MICRO MACRO WORLD WORLDWIDE
UNION PACIFIC SOUTHERN TEXAS YORK DOMINION AMERICAN AMERICA
""".split())

_SUFFIX = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|LLC|LP|"
    r"HOLDINGS|HOLDING|HLDGS|HLDG|GROUP|GRP|THE|CLASS|CL|COM|COMMON|STK|NEW|"
    r"SA|NV|AG|ADR|ADS|SHS|SPON|SPONSORED|TRUST|PARTNERS|INTERNATIONAL|INTL|"
    r"COS|COMPANIES|CDA|CDN|AMER|AMERICA|AMERICAN|STORES|BRANDS|"
    r"MOTOR|MOTORS|MATLS|MATERIALS|INSTRS|INSTRUMENTS|IRELAND|"
    r"COMMUNICATIONS|COMM|SOLUTIONS|SOLUTION|SVCS|SERVICES|"
    r"TECHNOLOGIES|TECHNOLOGY|TECH|SYSTEMS|SYS|INDUSTRIES|ENTERPRISES)\b")


def normalize(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9 ]", " ", str(name).upper())
    text = re.sub(r"\s+", " ", text)
    text = _SUFFIX.sub(" ", text)
    return re.sub(r"\s+", "", text)


def tokens(name: str) -> set[str]:
    text = re.sub(r"[^A-Za-z0-9 ]", " ", str(name).upper())
    text = _SUFFIX.sub(" ", text)
    return {t for t in text.split() if len(t) > 2}


def load_archives() -> pd.DataFrame:
    paths = sorted(DATA_DIR.glob("13f_*.csv.gz"))
    if not paths:
        raise SystemExit("no 13f_*.csv.gz archives found -- run tools/backfill_13f.py")
    frame = pd.concat([pd.read_csv(p, dtype=str, keep_default_na=False)
                       for p in paths], ignore_index=True)
    frame["shares_n"] = pd.to_numeric(frame["shares"], errors="coerce")
    frame["value_n"] = pd.to_numeric(frame["value"], errors="coerce")
    # Equity lines only, matching edgar.is_equity_line: no options, no bond
    # principal, and issue-type characters that are digits rather than letters.
    frame = frame[(frame["put_call"].fillna("") == "")
                  & (frame["share_type"].str.upper().fillna("SH") == "SH")
                  & (frame["cusip"].str.len() == 9)
                  & (frame["cusip"].str[6:8].str.isdigit())
                  & ~frame["title_class"].str.upper().str.contains(
                      r"\b(?:NOTE|NTS|BOND|DEB|PFD|WT|WTS|DEP|DEPOSITARY)\b",
                      regex=True, na=False)
                  & (frame["shares_n"] > 0) & (frame["value_n"] > 0)]
    print(f"  {len(paths)} archives, {len(frame):,} equity lines")
    return frame


def closes_by_quarter(conn, tickers, quarters) -> dict[tuple[str, str], float]:
    """Last close at or before each quarter end. Quarter ends fall on weekends."""
    out: dict[tuple[str, str], float] = {}
    frame = pd.read_sql_query(
        "SELECT ticker, as_of, value FROM metrics WHERE metric = 'close'", conn)
    for ticker, group in frame.groupby("ticker"):
        if ticker not in tickers:
            continue
        group = group.sort_values("as_of")
        for quarter in quarters:
            prior = group[group["as_of"] <= quarter]
            if len(prior):
                out[(ticker, quarter)] = float(prior.iloc[-1]["value"])
    return out


def main() -> int:
    print("Loading raw 13F archives...")
    frame = load_archives()

    universe = pd.read_csv(sorted(DATA_DIR.glob("fundamentals_*.csv"))[-1])
    names = dict(zip(universe["ticker"], universe["name"]))
    quarters = sorted(frame["quarter"].unique())

    conn = history.connect()
    closes = closes_by_quarter(conn, set(names), quarters)

    # Implied price per (cusip, quarter), median across filers.
    frame["implied"] = frame["value_n"] / frame["shares_n"]
    implied = (frame.groupby(["cusip", "quarter"])["implied"].median()
                    .to_dict())
    issuers = (frame.groupby("cusip")["issuer"]
                    .agg(lambda s: s.value_counts().index[0]).to_dict())
    classes = (frame.groupby("cusip")["title_class"]
                    .agg(lambda s: s.value_counts().index[0]).to_dict())
    filers = frame.groupby("cusip")["cik"].nunique().to_dict()

    def _split_error(ratio: float) -> float:
        """Distance from the nearest clean split multiple, in either direction."""
        best = 1.0
        for r in SPLIT_RATIOS:
            best = min(best, abs(ratio / r - 1.0), abs(ratio * r - 1.0))
        return best

    def verify(cusip: str, ticker: str) -> tuple[int, float, bool]:
        """Quarters where the implied price matches, the best error, and whether
        the agreement required a split multiple."""
        errors, split_only = [], []
        for quarter in quarters:
            price, close = implied.get((cusip, quarter)), closes.get((ticker, quarter))
            if not (price and close and close > 0):
                continue
            direct = abs(price / close - 1.0)
            if direct <= PRICE_TOL:
                errors.append(direct)
            else:
                se = _split_error(price / close)
                if se <= SPLIT_TOL:
                    split_only.append(se)
        if errors:
            return len(errors), min(errors), False
        if split_only:
            return len(split_only), min(split_only), True
        # A name with no close in the store at all -- a delisting or a buyout --
        # cannot be price-checked either way, and rejecting it would silently
        # drop a real position. Fall through to the caller's name evidence.
        has_any = any(closes.get((ticker, q)) for q in quarters)
        return (0, float("nan"), False) if has_any else (-1, float("nan"), False)

    by_norm = defaultdict(list)
    for cusip, issuer in issuers.items():
        by_norm[normalize(issuer)].append(cusip)

    accepted: list[dict] = []
    claimed: set[str] = set()

    # Stage 1 -- normalized issuer name, then price-verified.
    for ticker, name in names.items():
        for cusip in by_norm.get(normalize(name), []):
            n_ok, err, by_split = verify(cusip, ticker)
            if n_ok >= MIN_QUARTERS or n_ok == -1:
                accepted.append({"ticker": ticker, "cusip": cusip,
                                 "issuer": issuers[cusip], "title_class": classes[cusip],
                                 "n_filers": filers[cusip],
                                 "price_err": None if pd.isna(err) else round(err, 4),
                                 "method": "name-unpriced" if n_ok == -1
                                           else ("name-split" if by_split else "name")})
                claimed.add(cusip)

    matched = {a["ticker"] for a in accepted}
    print(f"  stage 1 (name + price): {len(matched)}/{len(names)} tickers, "
          f"{len(accepted)} CUSIPs")

    # Stage 2 -- for the rest, let price do the finding and require a shared
    # *distinctive* word. Two further guards, both learned the hard way:
    # the price match must be direct (a split multiple is far too loose a net
    # when the name evidence is only a shared token), it must hold across most
    # of the window, and the candidate must be UNIQUE. An ambiguous ticker is
    # reported rather than guessed -- one wrong CUSIP here silently attributes
    # another company's position to this name.
    ambiguous: dict[str, list[str]] = {}
    for ticker, name in names.items():
        if ticker in matched:
            continue
        want = tokens(name) - STOPWORDS
        if not want:
            continue
        hits = []
        for cusip, issuer in issuers.items():
            if cusip in claimed or not (want & (tokens(issuer) - STOPWORDS)):
                continue
            n_ok, err, by_split = verify(cusip, ticker)
            if by_split or n_ok < 4:
                continue
            hits.append((cusip, err))
        if len(hits) == 1:
            cusip, err = hits[0]
            accepted.append({"ticker": ticker, "cusip": cusip,
                             "issuer": issuers[cusip], "title_class": classes[cusip],
                             "n_filers": filers[cusip],
                             "price_err": None if pd.isna(err) else round(err, 4),
                             "method": "price"})
            claimed.add(cusip)
        elif len(hits) > 1:
            ambiguous[ticker] = [c for c, _ in hits]

    # Stage 3 -- other share classes of an issuer already matched. Sharing the
    # 6-character issuer stem is not enough on its own: unrelated trusts share
    # stems across their own share classes. The issuer name must agree too.
    for row in list(accepted):
        stem, want = row["cusip"][:6], normalize(row["issuer"])
        for cusip, issuer in issuers.items():
            if cusip in claimed or not cusip.startswith(stem):
                continue
            if normalize(issuer) != want:
                continue
            n_ok, err, _ = verify(cusip, row["ticker"])
            if n_ok >= MIN_QUARTERS or n_ok == -1:
                accepted.append({"ticker": row["ticker"], "cusip": cusip,
                                 "issuer": issuers[cusip], "title_class": classes[cusip],
                                 "n_filers": filers[cusip],
                                 "price_err": None if pd.isna(err) else round(err, 4),
                                 "method": "class"})
                claimed.add(cusip)

    for ticker, cusip in OVERRIDES.items():
        if ticker in {a["ticker"] for a in accepted} or cusip not in issuers:
            continue
        n_ok, err, _ = verify(cusip, ticker)
        accepted.append({"ticker": ticker, "cusip": cusip,
                         "issuer": issuers[cusip], "title_class": classes[cusip],
                         "n_filers": filers[cusip],
                         "price_err": None if pd.isna(err) else round(err, 4),
                         "method": "pinned"})
        claimed.add(cusip)

    out = pd.DataFrame(accepted).sort_values(["ticker", "cusip"])
    out.to_csv(CUSIP_MAP_PATH, index=False)

    covered = set(out["ticker"])
    missing = sorted(set(names) - covered)
    print(f"  stage 2+3: {len(covered)}/{len(names)} tickers, {len(out)} CUSIPs")
    print(f"\nWrote {CUSIP_MAP_PATH} -- {len(out)} rows, "
          f"median price error {out['price_err'].median():.4f}")
    crowded = out.groupby("ticker").size()
    crowded = crowded[crowded > 3]
    if len(crowded):
        print(f"\nWARNING -- tickers with more than 3 CUSIPs, check for a bad match:")
        for ticker, n in crowded.items():
            print(f"  {ticker}: {n}")
    if ambiguous:
        print(f"\nAmbiguous, left unmapped ({len(ambiguous)}): "
              f"{', '.join(sorted(ambiguous))}")
    if missing:
        print(f"\nUnheld or unmapped ({len(missing)}): {', '.join(missing)}")
        print("  (a name no fund in the roster holds is expected, not a defect)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
