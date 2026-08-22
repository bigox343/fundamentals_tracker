"""SEC EDGAR 13F filings -> ThirteenFRow positions.

Knows EDGAR's HTTP surface and the shape of a 13F information table. Knows no
SQL, no HTML, and nothing about the universe -- the fund roster and the CUSIP
map are handed in by the caller, so every parsing function here is testable
against captured fixtures with no network. As in extract.py, the
network-touching helpers are confined to the bottom of the module.

Why this is a separate module from extract.py: 13F is filed per *manager*, not
per issuer, so it inverts the grain of every other fetch in this repo. You pull
N funds and join into the universe rather than pulling N tickers.
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from history import FilingRow, ThirteenFRow

# SEC's fair-access policy requires a declared identity and caps traffic at 10
# requests/second. We stay an order of magnitude under it.
USER_AGENT = "fundamentals_tracker (li.gang.nju@gmail.com)"
FETCH_SLEEP = 0.25

# A 13F is due 45 days after the quarter it reports on.
FILING_LAG_DAYS = 45

# A pinned CIK whose newest filing is further behind than this is pointing at
# the wrong filer entity, not merely running late. Three of the 28 CIKs
# originally resolved by company-name search failed exactly this way -- they
# returned perfectly parseable XML from a wound-down or superseded entity
# (Baupost's /ADV shell last filed in 2002; Marshall Wace Asia in 2021).
STALE_QUARTERS = 2

QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


# --------------------------------------------------------------------------
# Quarters
# --------------------------------------------------------------------------

def quarter_ends(n: int, today: date | None = None) -> list[str]:
    """The n most recent quarter ends whose filing deadline has passed."""
    today = today or date.today()
    out: list[str] = []
    year = today.year + 1
    while len(out) < n and year > today.year - 40:
        for month, day in reversed(QUARTER_ENDS):
            end = date(year, month, day)
            if end + timedelta(days=FILING_LAG_DAYS) <= today:
                out.append(end.isoformat())
                if len(out) == n:
                    break
        year -= 1
    return out


def previous_quarter(quarter: str) -> str:
    """The quarter end immediately before the given one."""
    d = date.fromisoformat(quarter)
    idx = QUARTER_ENDS.index((d.month, d.day))
    month, day = QUARTER_ENDS[idx - 1]
    year = d.year - 1 if idx == 0 else d.year
    return date(year, month, day).isoformat()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_info_table(xml: str) -> list[dict]:
    """Expand a 13F information table into one dict per reported line.

    Matched on XML local-name, ignoring the namespace prefix, because filers are
    not consistent: Tiger Global emits <infoTable> and Millennium emits
    <n1:infoTable>. A parser keyed on the bare tag returns zero rows for the
    namespaced half -- which does not surface as a parse error, it surfaces as a
    manager that appears to hold nothing. Five of 28 probed funds silently
    parsed to 0 positions before this was found.
    """
    if not xml or not xml.strip():
        return []
    root = ET.fromstring(xml)
    out: list[dict] = []
    for node in root.iter():
        if _local(node.tag) != "infoTable":
            continue
        rec: dict = {}
        for child in node.iter():
            key = _local(child.tag)
            if key == "infoTable":
                continue
            text = (child.text or "").strip()
            # setdefault: <votingAuthority> nests <Sole>/<Shared>/<None>, and
            # the first occurrence of each name is the one that matters.
            if text:
                rec.setdefault(key, text)
        if rec:
            out.append(rec)
    return out


_NON_EQUITY_CLASS = re.compile(
    r"\b(NOTE|NTS|BOND|DEB|DBCV|PFD|PREFERRED|WT|WTS|WARRANT|RIGHT|"
    r"UNIT|DEP|DEPOSITARY|DEPS)\b")


def is_equity_line(rec: dict) -> bool:
    """True for a plain long common-stock position.

    Three independent rules, each of which was necessary against live data:

    * sshPrnamtType must be SH. PRN is a principal amount -- a bond.
    * putCall must be absent. 7,627 of Citadel's 16,127 lines carry it (47%;
      Point72 50%, Millennium 34%), and an option is not ownership.
    * CUSIP positions 7-8 are the issue-type characters: digits for equity,
      letters for debt. Name-matching alone pulled 833445AB5 (a Snowflake
      convertible note) and 25809KAB1 (a DoorDash note) in beside the real
      equity CUSIPs, and a convert is not an equity stake.

    titleOfClass is checked too, which catches what the CUSIP rule cannot:
    Alphabet's DEP SHS RP1/20 has an equity issue type but each unit is one
    twentieth of a share, so its count is on a different basis from common.
    """
    if rec.get("putCall"):
        return False
    if (rec.get("sshPrnamtType") or "SH").upper() != "SH":
        return False
    cusip = (rec.get("cusip") or "").strip()
    if len(cusip) != 9 or not cusip[6:8].isdigit():
        return False
    if _NON_EQUITY_CLASS.search((rec.get("titleOfClass") or "").upper()):
        return False
    return True


def _num(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _squeeze(text: str) -> str:
    """Filers pad fields with runs of spaces ('CAP       STK  CL  A')."""
    return re.sub(r"\s+", " ", (text or "").strip())


def position_rows(cik: str, fund: str, quarter: str, records: Iterable[dict],
                  cusip_map: dict[str, str]) -> tuple[list[ThirteenFRow], int, float]:
    """Resolve equity lines into universe positions.

    Returns the universe rows, the count of equity lines in the whole filing,
    and the filing's total equity value. The last two describe positions this
    store deliberately does not keep, and they are what make a position's share
    of the manager's book computable later -- the ratio that separates a
    conviction holding from market-making flow.
    """
    rows: list[ThirteenFRow] = []
    n_equity = 0
    book = 0.0
    merged: dict[str, ThirteenFRow] = {}
    for rec in records:
        if not is_equity_line(rec):
            continue
        n_equity += 1
        value = _num(rec.get("value")) or 0.0
        book += value
        cusip = rec["cusip"].strip()
        ticker = cusip_map.get(cusip)
        if not ticker:
            continue
        shares = _num(rec.get("sshPrnamt")) or 0.0
        # A manager may report one CUSIP on several lines (different accounts or
        # discretion categories). The store's key is (cik, quarter, cusip), so
        # they must be summed here or the last line would overwrite the rest.
        prior = merged.get(cusip)
        if prior:
            merged[cusip] = prior._replace(
                shares=(prior.shares or 0.0) + shares,
                value=(prior.value or 0.0) + value)
        else:
            merged[cusip] = ThirteenFRow(
                cik, fund, quarter, cusip, ticker,
                _squeeze(rec.get("nameOfIssuer", "")),
                _squeeze(rec.get("titleOfClass", "")),
                shares, value)
    rows = list(merged.values())
    return rows, n_equity, book


def load_cusip_map(path: Path) -> dict[str, str]:
    """CUSIP -> universe ticker, from the checked-in map."""
    if not Path(path).exists():
        return {}
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {r["cusip"].strip(): r["ticker"].strip()
            for r in frame.to_dict("records")
            if r.get("cusip") and r.get("ticker")}


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

def _get(url: str, tries: int = 3) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept-Encoding": "gzip, deflate"})
    last: Exception | None = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read()
            return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def filing_index(cik: str) -> list[tuple[str, str, str, str]]:
    """Every 13F-HR (and amendment) a manager has filed, newest quarter first.

    Returns (quarter, filed_date, accession, form).
    """
    payload = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    recent = payload["filings"]["recent"]
    seen = {
        (period, filed, accession, form)
        for form, filed, accession, period in zip(
            recent["form"], recent["filingDate"],
            recent["accessionNumber"], recent["reportDate"])
        if form in ("13F-HR", "13F-HR/A") and period
    }
    # Sorted by (quarter, filed_date) so an amendment, filed later for the same
    # quarter, sorts ahead of the original it restates.
    return sorted(seen, reverse=True)


def info_table_xml(cik: str, accession: str) -> str:
    """Fetch a filing's information table, whatever the filer named it."""
    base = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}")
    listing = _get(base + "/").decode("utf-8", "replace")
    names = [n for n in re.findall(r'href="([^"]+\.xml)"', listing)
             if "primary_doc" not in n.lower()]
    if not names:
        return ""
    url = ("https://www.sec.gov" + names[0]) if names[0].startswith("/") \
        else f"{base}/{names[0]}"
    return _get(url).decode("utf-8", "replace")


def collect_13f(funds: Sequence[dict], quarters: Sequence[str],
                cusip_map: dict[str, str],
                already: dict[tuple[str, str], str] | None = None):
    """Fetch the wanted quarters for every fund, tolerating per-fund failures.

    `already` maps (cik, quarter) to the accession previously ingested; a filing
    is skipped when its accession is unchanged. In steady state every filing is
    already held and this makes no network requests at all -- 13Fs land in a
    burst four times a year and are unchanged the other ~85 days of a quarter.
    """
    already = already or {}
    wanted = set(quarters)
    rows: list[ThirteenFRow] = []
    filings: list[FilingRow] = []
    raw: list[dict] = []
    stale: list[str] = []
    failed: list[str] = []
    unmapped: dict[str, tuple[str, set[str]]] = {}

    for i, fund in enumerate(funds, 1):
        cik, name = fund["cik"], fund["name"]
        cohort = fund.get("cohort", "")
        try:
            index = filing_index(cik)
        except Exception as exc:  # noqa: BLE001 - one fund must not lose the rest
            failed.append(name)
            print(f"  [{i:>2}/{len(funds)}] {name:<17} index FAILED: {exc}",
                  flush=True)
            continue

        if index and quarters:
            behind = sum(1 for q in quarters if q > index[0][0])
            if behind > STALE_QUARTERS:
                stale.append(f"{name} (CIK {cik}): newest filing {index[0][0]}")

        newest: dict[str, tuple[str, str, str]] = {}
        for quarter, filed, accession, _form in index:
            if quarter in wanted and quarter not in newest:
                newest[quarter] = (filed, accession, _form)

        for quarter in sorted(wanted - set(newest)):
            # Viking Global filed nothing for Q1 2026 and Pershing Square
            # nothing for Q2. Without a marker these count as missing on every
            # subsequent run, and the fetch can never reach a quiet state.
            filings.append(FilingRow(cik, name, cohort, quarter, "", "",
                                     0, 0, 0.0, "no-filing"))

        fetched = skipped = 0
        for quarter, (filed, accession, _form) in sorted(newest.items(),
                                                         reverse=True):
            if already.get((cik, quarter)) == accession:
                skipped += 1
                continue
            try:
                xml = info_table_xml(cik, accession)
                records = parse_info_table(xml)
                positions, n_equity, book = position_rows(
                    cik, name, quarter, records, cusip_map)
                rows.extend(positions)
                filings.append(FilingRow(cik, name, cohort, quarter, accession,
                                         filed, n_equity, len(positions), book,
                                         "ok"))
                for rec in records:
                    cusip = (rec.get("cusip") or "").strip()
                    if is_equity_line(rec) and cusip not in cusip_map:
                        issuer, seen = unmapped.setdefault(
                            cusip, (_squeeze(rec.get("nameOfIssuer", "")), set()))
                        seen.add(cik)
                    raw.append({"cik": cik, "fund": name, "cohort": cohort,
                                "quarter": quarter, "accession": accession,
                                "filed_date": filed,
                                "cusip": rec.get("cusip", ""),
                                "issuer": _squeeze(rec.get("nameOfIssuer", "")),
                                "title_class": _squeeze(rec.get("titleOfClass", "")),
                                "shares": rec.get("sshPrnamt"),
                                "share_type": rec.get("sshPrnamtType"),
                                "value": rec.get("value"),
                                "put_call": rec.get("putCall", "")})
                fetched += 1
            except Exception as exc:  # noqa: BLE001
                filings.append(FilingRow(cik, name, cohort, quarter, accession,
                                         filed, 0, 0, 0.0, "failed"))
                print(f"       {name:<17} {quarter} FAILED: {exc}", flush=True)
            time.sleep(FETCH_SLEEP)

        print(f"  [{i:>2}/{len(funds)}] {name:<17} "
              f"{fetched} fetched, {skipped} current", flush=True)

    # A CUSIP many managers hold but the map does not know is the signal that
    # the map has drifted from the universe -- a new share class, or a name
    # added to UNIVERSE without rebuilding the map.
    drift = sorted(((len(seen), cusip, issuer)
                    for cusip, (issuer, seen) in unmapped.items()),
                   reverse=True)
    return rows, filings, raw, stale, failed, drift


FILINGS_MANIFEST = "13f_filings.csv.gz"


def read_filings_manifest(path: Path) -> list[FilingRow]:
    """Load the manifest of every filing ever looked at."""
    if not Path(path).exists():
        return []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    out: list[FilingRow] = []
    for rec in frame.to_dict("records"):
        out.append(FilingRow(
            rec["cik"], rec["fund"], rec.get("cohort", ""), rec["quarter"],
            rec.get("accession", ""), rec.get("filed_date", ""),
            int(rec.get("n_positions") or 0), int(rec.get("n_universe") or 0),
            float(rec.get("book_value") or 0.0), rec.get("status", "ok")))
    return out


def write_filings_manifest(rows: Iterable[FilingRow], path: Path) -> int:
    """Merge new filing records into the manifest, keyed by (cik, quarter).

    The position archives cannot stand alone as the record of truth, because a
    filing with no reportable positions leaves no rows in them -- Viking Global
    filed an empty information table for Q1 2026. Rebuilt from positions only,
    the store would forget that filing happened, and every position Viking held
    the quarter before would render as an exit it never made. The manifest is
    what keeps an absent position distinguishable from an absent filing across
    a rebuild.
    """
    merged: dict[tuple[str, str], FilingRow] = {
        (r.cik, r.quarter): r for r in read_filings_manifest(path)}
    for row in rows:
        merged[(row.cik, row.quarter)] = row
    frame = pd.DataFrame([r._asdict() for r in merged.values()])
    if not len(frame):
        return 0
    frame = frame.sort_values(["fund", "quarter"])
    frame.to_csv(path, index=False, compression="gzip")
    return len(frame)


def ingest_archives(paths: Sequence[Path], cusip_map: dict[str, str],
                    manifest: Path | None = None):
    """Rebuild store rows from the raw archives, with no network.

    This is what makes the archive worth keeping. Adding a 149th ticker, or
    correcting a CUSIP, is a re-parse of files already on disk rather than 216
    fresh requests to EDGAR -- the same guarantee `--rebuild-history` gives for
    the rest of the store.
    """
    rows: list[ThirteenFRow] = []
    # The manifest is authoritative for which filings exist, including the ones
    # that reported nothing and so appear nowhere in the position archives.
    from_manifest = {(r.cik, r.quarter): r
                     for r in (read_filings_manifest(manifest) if manifest else [])}
    filings: list[FilingRow] = []
    for path in sorted(paths):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        for (cik, quarter), group in frame.groupby(["cik", "quarter"]):
            records = group.to_dict("records")
            first = records[0]
            name = first.get("fund", "")
            positions, n_equity, book = position_rows(
                cik, name, quarter,
                [{"cusip": r["cusip"], "nameOfIssuer": r["issuer"],
                  "titleOfClass": r["title_class"], "sshPrnamt": r["shares"],
                  "sshPrnamtType": r["share_type"], "value": r["value"],
                  "putCall": r["put_call"] or None}
                 for r in records],
                cusip_map)
            rows.extend(positions)
            if (cik, quarter) not in from_manifest:
                from_manifest[(cik, quarter)] = FilingRow(
                    cik, name, first.get("cohort", ""), quarter,
                    first.get("accession", ""), first.get("filed_date", ""),
                    n_equity, len(positions), book, "ok")
    filings = list(from_manifest.values())
    return rows, filings


def write_raw_archive(raw: Sequence[dict], path: Path) -> int:
    """The record of truth: every reported line, before the universe filter.

    The store keeps only universe positions, so without this a 149th ticker
    would cost a re-fetch of every fund and quarter. Compressed because these
    tables are large and highly repetitive -- one quarter across 27 funds is
    ~38,000 lines.
    """
    if not raw:
        return 0
    frame = pd.DataFrame(list(raw))
    frame.to_csv(path, index=False, compression="gzip")
    return len(frame)
