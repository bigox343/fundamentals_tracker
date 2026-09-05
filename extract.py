"""yfinance frames -> MetricRow observations.

Knows yfinance shapes and fiscal calendars. Knows no SQL and no HTML, so every
parsing function here is testable against captured fixtures with no network.
The network-touching helpers are confined to the bottom of the module.

MetricRow is imported from history because it is the shared contract between
producer and store; importing a NamedTuple is not knowledge of SQL.
"""
from __future__ import annotations

import re
import time
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

import pandas as pd
import yfinance as yf

from history import HoldingRow, InsiderRow, MetricRow, is_finite

# A quarterly statement frame is considered stale when the company has reported
# more recently than this many days after its newest known quarter end. Normal
# reporting lag is 20-60 days; a full quarter is ~91. Sitting between the two
# separates "reported on time" from "Yahoo has not ingested the filing yet".
STALE_QUARTER_DAYS = 60


def _is_month_end(d: date) -> bool:
    return d.day == monthrange(d.year, d.month)[1]


def add_months(d: date, n: int) -> date:
    """Shift by n months, preserving month-end.

    Fiscal periods sit on month ends and those ends have different lengths:
    30 April plus one quarter is 31 July, not 30 July. Getting this wrong
    misdates every backfilled estimate by a day.
    """
    total = (d.year * 12 + d.month - 1) + n
    year, month = divmod(total, 12)
    month += 1
    last = monthrange(year, month)[1]
    day = last if _is_month_end(d) else min(d.day, last)
    return date(year, month, day)


def snap_month_end(d: date) -> date:
    """Snap a fiscal date to the nearest month end, the canonical ref_period form.

    Two sources disagree about how to date the same fiscal period. Yahoo's
    statement frames normalise every column to a month end (148/148 tickers),
    while `info`'s fiscal fields carry the true 52/53-week dates: NVDA's FY2026
    ends 2026-01-25 in `info` and 2026-01-31 in `income_stmt`; AVGO's 2025-11-02
    against 2025-10-31. A 52/53-week date also drifts a few days every year, so
    raw dates never stabilise into a usable key.

    Snapping gives one label per fiscal period, stable across observations and
    across sources, which is what makes an estimate joinable to the actual it
    was forecasting.
    """
    this_end = date(d.year, d.month, monthrange(d.year, d.month)[1])
    prev_end = d.replace(day=1) - timedelta(days=1)
    return prev_end if (d - prev_end) < (this_end - d) else this_end


def _stale_adjusted_quarter(anchor: date, last_report: date | None) -> date:
    """Advance past quarters Yahoo's statement frame has not ingested yet.

    `quarterly_income_stmt` lags the filing by days to weeks. On 2026-08-15
    CSCO, SMCI, COHR and LITE had all reported their June quarter but their
    frames still ended in March, which would resolve 0q a full quarter short.
    """
    if last_report is None:
        return anchor
    guard = 0
    while last_report > anchor + timedelta(days=STALE_QUARTER_DAYS) and guard < 4:
        anchor = add_months(anchor, 3)
        guard += 1
    return anchor


def resolve_ref_periods(
    as_of: date,
    annual_ends: Sequence[date],
    quarter_ends: Sequence[date],
    last_fiscal_year_end: date | None = None,
    last_report: date | None = None,
) -> dict[str, str]:
    """Map relative estimate horizons to absolute fiscal period end dates.

    Horizon labels are relative: '0y' means a different fiscal year after
    rollover. Storing by label alone renders that rollover as an enormous fake
    revision, so every estimate row carries the resolved absolute period.

    The horizons count forward from the last *reported* period, never from
    as_of. Yahoo's `0q` is the quarter that reports next, which has usually
    already ended: on 2026-08-15 NVDA's `0q` was the quarter ending 2026-07-31,
    reporting on the 26th. Resolving from as_of lands a full quarter late for
    every company sitting between quarter close and earnings -- verified wrong
    for NVDA, AVGO and WMT, and correct only by luck for the others.

    `last_fiscal_year_end` comes from `info` and is preferred over the statement
    columns, which can lag a full fiscal year: six tracked names had reported
    FY results that `income_stmt` had not yet picked up.

    Labels are prefixed `FY` or `FQ` because a fiscal year and a fiscal quarter
    ending on the same day are different periods carrying different numbers, and
    the store keys estimates on (ticker, as_of, period_type, metric, ref_period)
    where period_type is 'estimate' for both. A bare date collides: AVGO's +1q
    and 0y both end 2026-10-31, COST's 0q and 0y both end 2026-08-31, and CSX's
    +1q and 0y both end 2026-12-31 -- 3 of 8 names probed. Observed live, the
    annual figure overwrote the quarterly one in place (AVGO stored 11.62543
    where the quarter was 3.87377), which is the silent, plausible-looking
    corruption the primary key exists to prevent.

    Horizons that cannot be resolved are omitted. Callers must skip them
    rather than substitute a guess: a wrong reference period corrupts
    attribution invisibly, while a missing point is merely missing.
    """
    out: dict[str, str] = {}

    fy_anchor = None
    if last_fiscal_year_end is not None:
        fy_anchor = snap_month_end(last_fiscal_year_end)
        # `info` may roll to the new fiscal year at the year end rather than at
        # the report. If nothing has been reported since, that year is still the
        # one consensus is forecasting, so step back to keep 0y on it.
        if last_report is not None and last_report < fy_anchor:
            fy_anchor = add_months(fy_anchor, -12)
    elif annual_ends:
        fy_anchor = snap_month_end(max(annual_ends))

    if fy_anchor is not None:
        out["0y"] = "FY" + add_months(fy_anchor, 12).isoformat()
        out["+1y"] = "FY" + add_months(fy_anchor, 24).isoformat()

    if quarter_ends:
        q_anchor = _stale_adjusted_quarter(
            snap_month_end(max(quarter_ends)), last_report
        )
        out["0q"] = "FQ" + add_months(q_anchor, 3).isoformat()
        out["+1q"] = "FQ" + add_months(q_anchor, 6).isoformat()

    return out


# eps_trend column -> how far before as_of the observation was taken.
#
# Calendar days, not trading days. Tested across 228 ticker-horizon cases on
# 2026-08-15: reading them as trading days places the largest consensus jump
# strictly before the earnings report that caused it in 18% of cases, against 6%
# for calendar days. The sharpest group -- names that reported 71-95 days before
# as_of -- puts the jump in the oldest bracket 40 times out of 44, which only
# calendar spacing explains.
#
# Kept as a table rather than parsed from the column name so a single edit
# corrects every backfilled date if that ever changes.
TREND_OFFSETS: dict[str, int] = {
    "current": 0,
    "7daysAgo": 7,
    "30daysAgo": 30,
    "60daysAgo": 60,
    "90daysAgo": 90,
}


def eps_trend_rows(
    ticker: str,
    frame,
    as_of: date,
    ref_periods: dict[str, str],
) -> list[MetricRow]:
    """Expand an eps_trend frame into dated consensus observations.

    This is the only forward-looking field carrying history, so it seeds the
    revision series with roughly 90 days of real observations on first run.

    Backfilled points are accurate to a few days at best -- revisions trickle in
    for days after a report, so a column dated 30 days back reflects a consensus
    that moved over a window, not an instant. The store distinguishes them for
    free: a backfilled row carries an `ingested_at` far later than its `as_of`,
    where a captured row has the two within a day.
    """
    rows: list[MetricRow] = []
    for horizon, series in frame.iterrows():
        ref = ref_periods.get(str(horizon))
        if ref is None:
            continue
        for column, offset in TREND_OFFSETS.items():
            if column not in series.index:
                continue
            value = series[column]
            if not is_finite(value) or float(value) == 0.0:
                # Yahoo pads a lookback column it has no data for with 0.0, not
                # NaN, and only ever in the oldest column: on 2026-08-15, all 32
                # zeros across 13 tickers sat in 90daysAgo and none in 60/30/7 or
                # current, while the point-in-time frames had none at all. Stored,
                # a zero reads as "consensus was $0.00" and the next observation
                # renders an infinite revision -- HD went 0.00 -> 14.96. §6.2
                # requires a missing value to write no row so that "never
                # observed" stays distinct from a genuine zero, and a real
                # consensus EPS of exactly 0.00000 does not occur.
                continue
            observed = (as_of - timedelta(days=offset)).isoformat()
            rows.append(
                MetricRow(ticker, observed, "estimate", "epsEst", ref, float(value))
            )
    return rows


# frame name -> {yfinance column: stored metric name}.
#
# Yahoo's casing is inconsistent within a single frame (upLast7days but
# downLast7Days). These names are copied verbatim from the live API; a
# plausible-looking correction silently drops the column.
#
# growth_estimates is deliberately absent: its LTG row has no fiscal period,
# and its remaining rows duplicate the growth figures already captured here.
FRAME_FIELDS: dict[str, dict[str, str]] = {
    "earnings_estimate": {
        "avg": "epsEstAvg",
        "low": "epsEstLow",
        "high": "epsEstHigh",
        "numberOfAnalysts": "epsEstAnalysts",
        "growth": "epsEstGrowth",
    },
    "revenue_estimate": {
        "avg": "revEstAvg",
        "low": "revEstLow",
        "high": "revEstHigh",
        "numberOfAnalysts": "revEstAnalysts",
        "growth": "revEstGrowth",
    },
    "eps_revisions": {
        "upLast7days": "epsRevUp7",
        "upLast30days": "epsRevUp30",
        "downLast7Days": "epsRevDown7",
        "downLast30days": "epsRevDown30",
    },
}


def is_degenerate_estimate(series) -> bool:
    """True when a consensus row is self-referential and therefore meaningless.

    Not every bad value is NaN. LITE's 0y row on 2026-08-15 read avg=8.22662,
    low=19.05, high=25.27 -- a mean below its own low -- with `yearAgoEps` also
    8.22662 and growth 0. Every field is finite and none is individually absurd,
    so §7's NaN and inf checks pass it straight through and it lands as a
    plausible -66% revision against the next capture.

    Two structural invariants catch it without inventing per-metric bounds: a
    mean cannot fall outside its own range, and a consensus cannot equal its own
    prior-year actual to five decimals.
    """
    if "avg" not in series.index:
        return False
    avg = series["avg"]
    if not is_finite(avg):
        return False
    avg = float(avg)

    low, high = series.get("low"), series.get("high")
    if is_finite(low) and is_finite(high) and not (float(low) <= avg <= float(high)):
        return True

    for prior in ("yearAgoEps", "yearAgoRevenue"):
        ago = series.get(prior)
        if is_finite(ago) and avg == float(ago):
            return True
    return False


def frame_rows(
    ticker: str,
    frame_name: str,
    frame,
    as_of: date,
    ref_periods: dict[str, str],
) -> list[MetricRow]:
    """Expand a point-in-time estimate frame into observations dated as_of.

    Unlike eps_trend these frames carry no history, so every row is stamped
    with today's date and the series accrues only from capture forward.
    """
    fields = FRAME_FIELDS.get(frame_name)
    if not fields:
        return []
    stamp = as_of.isoformat()
    rows: list[MetricRow] = []
    for horizon, series in frame.iterrows():
        ref = ref_periods.get(str(horizon))
        if ref is None:
            continue
        if is_degenerate_estimate(series):
            continue
        for column, metric in fields.items():
            if column not in series.index:
                continue
            value = series[column]
            if not is_finite(value):
                continue
            rows.append(
                MetricRow(ticker, stamp, "estimate", metric, ref, float(value))
            )
    return rows


ESTIMATES_COLUMNS: tuple[str, ...] = MetricRow._fields

# Matches the politeness delay fetch_all() already uses.
FETCH_SLEEP = 0.3


def write_estimates_csv(rows: list[MetricRow], path: Path) -> int:
    """Write the durable record of a day's estimates.

    Estimates are the one dataset that cannot be refetched, so they are
    written as plain text on the same run that captures them. This keeps
    history.db derived and therefore disposable.
    """
    frame = pd.DataFrame(list(rows), columns=list(ESTIMATES_COLUMNS))
    frame.to_csv(csv_path(path), index=False, compression="gzip")
    return len(rows)


def read_estimates_csv(path: Path) -> list[MetricRow]:
    """Read a day's estimates back, exactly.

    `float_precision="round_trip"` is not optional: the default C parser is
    fast rather than exact, and turns a written 1.9626000000000001 back into
    1.9626. Small enough to look like nothing, but it means a rebuilt store
    does not equal the one it replaced, which is the property the raw CSVs
    exist to guarantee.
    """
    found = open_csv(path)
    if found is None:
        raise FileNotFoundError(path)
    frame = read_csv_any(
        found,
        dtype={"ticker": str, "as_of": str, "period_type": str,
               "metric": str, "ref_period": str},
        keep_default_na=False,
    )
    return [
        MetricRow(r["ticker"], r["as_of"], r["period_type"],
                  r["metric"], r["ref_period"], float(r["value"]))
        for r in frame.to_dict("records")
    ]


def _statement_ends(frame) -> list[date]:
    if frame is None or getattr(frame, "empty", True):
        return []
    return [d.date() for d in pd.to_datetime(list(frame.columns))]


def _epoch_date(value) -> date | None:
    if not isinstance(value, (int, float)) or not value:
        return None
    try:
        return pd.to_datetime(value, unit="s", utc=True).date()
    except (ValueError, OverflowError):
        return None


def _last_report_date(handle, as_of: date) -> date | None:
    """The most recent earnings date on or before as_of, or None.

    Read from `get_earnings_dates()` rather than `info`: `earningsTimestamp` is
    unreliable (null for MU, a past date for INFY) and `earningsTimestampStart`
    was stale for 7 of 147 names. This anchors both staleness guards, so it is
    worth the extra request.
    """
    try:
        frame = handle.get_earnings_dates(limit=8)
    except Exception:  # noqa: BLE001 - a missing calendar must not fail the ticker
        return None
    if frame is None or getattr(frame, "empty", True):
        return None
    index = pd.to_datetime(frame.index)
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    past = index[index <= pd.Timestamp(as_of)]
    return past.max().date() if len(past) else None


def fetch_ticker_estimates(ticker: str, as_of: date) -> list[MetricRow]:
    """Pull one company's estimate frames and expand them into rows.

    Raises on a failed fetch so the caller can count the ticker as failed;
    a partial day is acceptable but a silently empty one is not.
    """
    handle = yf.Ticker(ticker)
    info = handle.info or {}
    ref_periods = resolve_ref_periods(
        as_of,
        _statement_ends(handle.income_stmt),
        _statement_ends(handle.quarterly_income_stmt),
        last_fiscal_year_end=_epoch_date(info.get("lastFiscalYearEnd")),
        last_report=_last_report_date(handle, as_of),
    )
    if not ref_periods:
        return []

    rows = eps_trend_rows(ticker, handle.eps_trend, as_of, ref_periods)
    for name in FRAME_FIELDS:
        frame = getattr(handle, name, None)
        if frame is None or getattr(frame, "empty", True):
            continue
        rows.extend(frame_rows(ticker, name, frame, as_of, ref_periods))
    return rows


# Re-exported so callers reach for one module: extract owns the fetch-and-write
# side, and pairing the row type with its column order here keeps the CSV writer
# and the store from drifting apart.
HOLDING_COLUMNS: tuple[str, ...] = HoldingRow._fields
INSIDER_COLUMNS: tuple[str, ...] = InsiderRow._fields

HOLDER_FRAMES = {"institutional_holders": "institution",
                 "mutualfund_holders": "fund"}

# yfinance returns up to 150 filing lines; the drilldown shows the recent tail
# and the store keeps what it is given, so this only bounds the fetch.
INSIDER_LIMIT = 60


def _iso(value) -> str | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts).date().isoformat()


def holding_rows(ticker: str, kind: str, frame) -> list[HoldingRow]:
    """Expand a holders frame into rows, dated by each holder's own report date.

    The report date is per row, not per frame: yfinance mixes quarters inside one
    response (NVDA carried both 2026-03-31 and 2026-06-30), so stamping the frame
    with a single date would misdate whichever half is older -- and a holder's
    quarter-over-quarter change would then be measured against the wrong quarter.
    """
    if frame is None or getattr(frame, "empty", True):
        return []
    rows: list[HoldingRow] = []
    for rec in frame.to_dict("records"):
        holder = rec.get("Holder")
        as_of = _iso(rec.get("Date Reported"))
        # NaN is truthy, so a bare falsiness check lets a missing holder through
        # and writes a row keyed on the string "nan".
        if as_of is None or holder is None or pd.isna(holder) or not str(holder).strip():
            continue
        rows.append(HoldingRow(
            ticker, as_of, kind, str(holder),
            rec.get("Shares"), rec.get("Value"),
            rec.get("pctHeld"), rec.get("pctChange"),
        ))
    return rows


def insider_rows(ticker: str, frame) -> list[InsiderRow]:
    """Expand an insider-transactions frame into filing lines."""
    if frame is None or getattr(frame, "empty", True):
        return []
    rows: list[InsiderRow] = []
    for rec in frame.to_dict("records"):
        as_of = _iso(rec.get("Start Date"))
        insider = rec.get("Insider")
        if insider is not None and pd.isna(insider):
            insider = None
        # `Text` carries the human description ("Stock Gift at price 0.00"); the
        # `Transaction` column is frequently blank, so fall back to it rather
        # than dropping the line.
        action = rec.get("Transaction") or rec.get("Text") or ""
        if not as_of or not insider or not str(action).strip():
            continue
        rows.append(InsiderRow(
            ticker, as_of, str(insider), str(rec.get("Position") or ""),
            str(action).strip(), rec.get("Shares"), rec.get("Value"),
            str(rec.get("Ownership") or ""),
        ))
    return rows


def fetch_ticker_ownership(ticker: str):
    """Pull one company's holder and insider frames.

    Ownership is quarterly and yfinance only ever exposes the current snapshot,
    so last quarter's list is gone once it rolls -- the same perishability
    argument that puts estimates in the store.
    """
    handle = yf.Ticker(ticker)
    holds: list[HoldingRow] = []
    for attr, kind in HOLDER_FRAMES.items():
        try:
            holds.extend(holding_rows(ticker, kind, getattr(handle, attr, None)))
        except Exception:  # noqa: BLE001 - one missing frame must not lose the other
            continue
    try:
        ins = insider_rows(ticker, handle.insider_transactions)[:INSIDER_LIMIT]
    except Exception:  # noqa: BLE001
        ins = []
    return holds, ins


def collect_ownership(tickers: Sequence[str]):
    """Fetch ownership across the universe, tolerating per-ticker failures."""
    holds: list[HoldingRow] = []
    ins: list[InsiderRow] = []
    failed: list[str] = []
    for i, ticker in enumerate(tickers, 1):
        try:
            h, n = fetch_ticker_ownership(ticker)
            holds.extend(h)
            ins.extend(n)
        except Exception as exc:  # noqa: BLE001
            failed.append(ticker)
            print(f"  [{i:>3}/{len(tickers)}] {ticker:<6} ownership FAILED: {exc}",
                  flush=True)
        else:
            print(f"  [{i:>3}/{len(tickers)}] {ticker:<6} ownership "
                  f"({len(h)} holders, {len(n)} filings)", flush=True)
        time.sleep(FETCH_SLEEP)
    return holds, ins, failed


# The dated CSVs are written compressed. They are extremely repetitive -- only
# 152 of 11,228 estimate rows change day over day -- so they compress 5.9x blended
# (estimates 8.8x, insiders 6.3x, fundamentals only 2.2x), taking data/ from
# 433 MB/yr uncompressed to 73 MB/yr.
#
# Compressing rather than pruning is deliberate. Deleting old dated files would
# not lose data today, since it is all in history.db, but it would delete the
# rebuild path and invert the guarantee the store rests on: history.db is
# derived from these files and therefore disposable. Pruned, the database
# becomes the only copy of the perishable estimate history, which is the one
# dataset here that cannot be refetched.
CSV_SUFFIX = ".csv.gz"


def csv_path(path: Path) -> Path:
    """The compressed name for a dated CSV."""
    path = Path(path)
    return path if path.name.endswith(".gz") else path.with_suffix(".csv.gz")


def open_csv(path: Path) -> Path | None:
    """Find a dated CSV whether it was written compressed or plain.

    Files written before the switch stay readable, so no migration is required
    for the store to rebuild.
    """
    path = Path(path)
    for candidate in (csv_path(path), path.with_suffix(".csv")):
        if candidate.exists():
            return candidate
    return None


def read_csv_any(path: Path, **kwargs):
    """Read a dated CSV in either form, always with exact float round-trip.

    float_precision="round_trip" is load-bearing: the default parser is fast
    rather than exact and turns a written 1.9626000000000001 back into 1.9626,
    which means a rebuilt store does not equal the one it replaced -- the one
    property these raw files exist to guarantee.
    """
    kwargs.setdefault("float_precision", "round_trip")
    return pd.read_csv(path, **kwargs)


_DATED_RE = re.compile(r"_(\d{4})(\d{2})(\d{2})\.[A-Za-z.]+$")


def dated_stamp(path) -> str | None:
    """'estimates_20260904.csv.gz' -> '2026-09-04'."""
    match = _DATED_RE.search(str(path))
    return "-".join(match.groups()) if match else None


def prune_dated(directory: Path, pattern: str, keep: int,
                safe_dates: set[str] | None = None) -> list[Path]:
    """Delete all but the newest `keep` dated files matching `pattern`.

    `safe_dates` is a guard, not a filter: a file is removed only if its date
    is in that set. The set is the store's own record of what it has ingested,
    so a run that wrote a CSV and then failed before recording it cannot have
    that CSV deleted underneath it by the next run. Pass None for outputs that
    are rendered rather than ingested, where there is nothing to lose.

    Returns the paths removed.
    """
    files = sorted((p for p in Path(directory).glob(pattern)
                    if dated_stamp(p)), key=lambda p: dated_stamp(p))
    removed: list[Path] = []
    for path in files[:max(0, len(files) - keep)]:
        if safe_dates is not None and dated_stamp(path) not in safe_dates:
            continue
        path.unlink()
        removed.append(path)
    return removed


def write_rows_csv(rows, columns, path: Path) -> int:
    """Durable record for a table-shaped capture (holdings, insider filings)."""
    pd.DataFrame(list(rows), columns=list(columns)).to_csv(
        csv_path(path), index=False, compression="gzip")
    return len(rows)


def read_rows_csv(path: Path, factory, columns):
    found = open_csv(path)
    if found is None:
        raise FileNotFoundError(path)
    frame = read_csv_any(found, keep_default_na=False)
    out = []
    for rec in frame.to_dict("records"):
        vals = []
        for c in columns:
            v = rec.get(c, "")
            if c in ("shares", "value", "pct_held", "pct_change"):
                vals.append(None if v == "" else float(v))
            else:
                vals.append(str(v))
        out.append(factory(*vals))
    return out


def collect_estimates(
    tickers: Sequence[str], as_of: date
) -> tuple[list[MetricRow], list[str]]:
    """Fetch estimates for the universe, tolerating per-ticker failures."""
    rows: list[MetricRow] = []
    failed: list[str] = []
    for i, ticker in enumerate(tickers, 1):
        try:
            rows.extend(fetch_ticker_estimates(ticker, as_of))
        except Exception as exc:  # noqa: BLE001 - one bad name must not end the run
            failed.append(ticker)
            print(f"  [{i:>3}/{len(tickers)}] {ticker:<6} estimates FAILED: {exc}",
                  flush=True)
        else:
            print(f"  [{i:>3}/{len(tickers)}] {ticker:<6} estimates", flush=True)
        time.sleep(FETCH_SLEEP)
    return rows, failed
