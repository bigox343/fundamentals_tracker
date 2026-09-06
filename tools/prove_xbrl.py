"""Prove the XBRL extraction against the source it is replacing.

The same standard as data/cusip_map.csv, which is not trusted but proved: a
candidate is accepted only when an independent observation agrees. Here the
independent observation is Yahoo's own published multiple over the days where
the snapshot table and the reconstruction overlap.

A (ticker, metric) pair that fails gets no own-history frame. That is the store's
"a missing value writes no row" extended to derived data: a wrong multiple is
worse than an absent one, because it looks like knowledge.
"""
from __future__ import annotations

import pandas as pd

# 1% rather than exact, because Yahoo rounds and may define EBITDA slightly
# differently. The median rather than the mean, so one bad day cannot reject a
# good mapping. The CUSIP map's equivalent check reached a median price error of
# 0.0000, so a wide spread here means something is wrong, not that this is tight.
PASS_TOLERANCE = 0.01

# Two days is the floor for a median to mean anything at all.
MIN_OVERLAP = 2


def proof(recon: pd.Series, reference: pd.Series) -> tuple[float, bool]:
    """(median relative error, passed) over the overlapping index.

    The error is median(|recon - reference| / reference): reference is always
    the denominator, so the two arguments are not interchangeable -- swapping
    them changes the error even though the verdict at 0% is unaffected.
    """
    pair = pd.concat([recon.rename("r"), reference.rename("y")],
                     axis=1, join="inner").dropna()
    pair = pair[pair.y != 0]
    if len(pair) < MIN_OVERLAP:
        return float("nan"), False
    err = float(((pair.r - pair.y) / pair.y).abs().median())
    return err, err < PASS_TOLERANCE


def summarize(results: dict) -> pd.DataFrame:
    """(ticker, metric) -> (median_error, passed), as a frame."""
    return pd.DataFrame(
        [{"ticker": t, "metric": m, "median_error": e, "passed": ok}
         for (t, m), (e, ok) in sorted(results.items())])


def report(conn) -> pd.DataFrame:
    """Prove every (ticker, metric) against Yahoo's own published value."""
    import valuation
    snap = pd.read_sql_query(
        "SELECT ticker, as_of, metric, value FROM metrics "
        "WHERE period_type='snapshot' AND metric IN "
        "('trailingPE','ps','evEbitda','fcfYield')", conn)
    tickers = sorted(snap.ticker.unique())
    built = valuation.build_all(conn, tickers)
    results = {}
    for ticker, frame in built.items():
        ref_all = snap[snap.ticker == ticker]
        for metric in frame.columns:
            ref = ref_all[ref_all.metric == metric]
            if ref.empty:
                continue
            reference = pd.Series(ref.value.values,
                                  index=pd.DatetimeIndex(ref.as_of))
            results[(ticker, metric)] = proof(frame[metric], reference)
    return summarize(results)
