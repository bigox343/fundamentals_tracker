import json
import urllib.error
from pathlib import Path

import pytest

import xbrl

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_ticker_map_zero_pads_to_ten_digits():
    raw = (FIXTURES / "company_tickers_sample.json").read_bytes()
    m = xbrl.parse_ticker_map(raw)
    assert m["AAPL"] == "0000320193"
    assert m["NVDA"] == "0001045810"
    assert len(m["NET"]) == 10


def test_parse_ticker_map_covers_every_entry():
    raw = (FIXTURES / "company_tickers_sample.json").read_bytes()
    assert set(xbrl.parse_ticker_map(raw)) == {
        "AAPL", "MSFT", "NVDA", "AVGO", "NET"}


def _raw(name):
    return (FIXTURES / f"{name}.json").read_bytes()


def test_classify_span_separates_a_quarter_from_a_ytd_and_a_year():
    assert xbrl.classify_span("2026-03-29", "2026-06-27") == "quarter"
    assert xbrl.classify_span("2025-09-29", "2026-03-28") == "ytd"
    assert xbrl.classify_span("2025-09-29", "2026-09-27") == "annual"
    assert xbrl.classify_span("", "2026-06-27") == "instant"


def test_a_period_end_carrying_both_ytd_and_quarter_keeps_only_the_quarter():
    facts = xbrl.parse_concept("AAPL", "NetIncomeLoss", _raw("xbrl_aapl_netincome"))
    same_end = [f for f in facts if f.period_end == "2026-03-28"
                and f.period_start]
    spans = {xbrl.classify_span(f.period_start, f.period_end) for f in same_end}
    assert "quarter" in spans and "ytd" in spans, "fixture must contain both"

    q = xbrl.quarterly(facts)
    ends = [f.period_end for f in q]
    assert ends.count("2026-03-28") == 1
    kept = next(f for f in q if f.period_end == "2026-03-28")
    assert xbrl.classify_span(kept.period_start, kept.period_end) == "quarter"


def test_an_amendment_does_not_replace_the_original():
    facts = xbrl.parse_concept("AAPL", "NetIncomeLoss", _raw("xbrl_aapl_netincome"))
    by_key = {}
    for f in facts:
        by_key.setdefault((f.period_start, f.period_end), set()).add(f.filed)
    assert any(len(v) > 1 for v in by_key.values()), \
        "at least one period should carry an original and a later filing"


def test_q4_is_reconstructed_from_the_annual_minus_the_first_three():
    # A 10-K carries no Q4 fact. Without reconstruction every Q4 is a hole.
    facts = [
        xbrl.Fact("T", "NetIncomeLoss", "2025-01-01", "2025-03-31",
                  2025, "Q1", "10-Q", "2025-04-30", 100.0),
        xbrl.Fact("T", "NetIncomeLoss", "2025-04-01", "2025-06-30",
                  2025, "Q2", "10-Q", "2025-07-30", 110.0),
        xbrl.Fact("T", "NetIncomeLoss", "2025-07-01", "2025-09-30",
                  2025, "Q3", "10-Q", "2025-10-30", 120.0),
        xbrl.Fact("T", "NetIncomeLoss", "2025-01-01", "2025-12-31",
                  2025, "FY", "10-K", "2026-02-15", 500.0),
    ]
    q = xbrl.quarterly(facts)
    q4 = [f for f in q if f.period_end == "2025-12-31"]
    assert len(q4) == 1
    assert q4[0].value == pytest.approx(170.0)      # 500 - (100+110+120)
    assert q4[0].fp == "Q4"
    # It became knowable when the 10-K was filed, not at period end.
    assert q4[0].filed == "2026-02-15"


def test_a_november_fiscal_year_end_still_classifies_cleanly():
    # AVGO's fiscal year ends in early November and its quarters are 4-4-5, so
    # a calendar-quarter assumption would misclassify every one of them.
    assert xbrl.classify_span("2026-02-02", "2026-05-03") == "quarter"
    assert xbrl.classify_span("2025-11-03", "2026-11-01") == "annual"
    assert xbrl.classify_span("2025-11-03", "2026-05-03") == "ytd"


def test_a_53_week_year_is_still_an_annual_span():
    # A 52/53-week filer runs 371 days in the long year.
    assert xbrl.classify_span("2025-09-29", "2026-10-05") == "annual"


def test_non_finite_values_are_dropped_at_the_parse_boundary():
    raw = (b'{"units":{"USD":[{"start":"2025-01-01","end":"2025-03-31",'
           b'"val":null,"fy":2025,"fp":"Q1","form":"10-Q","filed":"2025-04-30"}]}}')
    assert xbrl.parse_concept("T", "NetIncomeLoss", raw) == []


def test_no_synthesized_fact_has_a_backwards_span():
    # Regression test for grouping Q4 candidates by the SEC's fy/fp labels,
    # which describe the *filing's* fiscal context, not the period a fact
    # covers. On these exact fixtures that bug produced a synthesized AAPL Q4
    # spanning 2011-06-25..2009-09-26 with value -11,064,000,000 (34/51 AAPL
    # netIncome, 14/21 AAPL revenue, 18/27 ORCL revenue synthesized quarters
    # were corrupt this way). Containment-based grouping must produce zero.
    for name, tag in [
        ("xbrl_aapl_netincome", "NetIncomeLoss"),
        ("xbrl_aapl_revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("xbrl_ora_revenues", "Revenues"),
        ("xbrl_ora_rev_contract", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ]:
        facts = xbrl.parse_concept("T", tag, _raw(name))
        q = xbrl.quarterly(facts)
        backwards = [f for f in q if f.period_start and f.period_start >= f.period_end]
        assert backwards == [], f"{name}: {backwards}"


def test_a_duplicate_annual_frame_does_not_produce_two_q4_facts():
    # The companyconcept API can carry the same annual duration fact under
    # more than one frame (identical start/end/filed, different frame label
    # in a field the parser does not keep), which collapse to identical
    # Facts here. Without the seen-guard, quarterly() would process the
    # duplicate annual fact a second time and emit a second, identical Q4.
    quarters = [
        xbrl.Fact("T", "NetIncomeLoss", "2025-01-01", "2025-03-31",
                  2025, "Q1", "10-Q", "2025-04-30", 100.0),
        xbrl.Fact("T", "NetIncomeLoss", "2025-04-01", "2025-06-30",
                  2025, "Q2", "10-Q", "2025-07-30", 110.0),
        xbrl.Fact("T", "NetIncomeLoss", "2025-07-01", "2025-09-30",
                  2025, "Q3", "10-Q", "2025-10-30", 120.0),
    ]
    annual = xbrl.Fact("T", "NetIncomeLoss", "2025-01-01", "2025-12-31",
                        2025, "FY", "10-K", "2026-02-15", 500.0)
    q = xbrl.quarterly(quarters + [annual, annual])
    q4 = [f for f in q if f.period_end == "2025-12-31"]
    assert len(q4) == 1


def test_a_null_end_writes_no_fact():
    raw = (b'{"units":{"USD":[{"start":"2025-01-01","end":null,'
           b'"val":100.0,"fy":2025,"fp":"Q1","form":"10-Q","filed":"2025-04-30"}]}}')
    assert xbrl.parse_concept("T", "NetIncomeLoss", raw) == []


def test_a_null_filed_writes_no_fact():
    raw = (b'{"units":{"USD":[{"start":"2025-01-01","end":"2025-03-31",'
           b'"val":100.0,"fy":2025,"fp":"Q1","form":"10-Q","filed":null}]}}')
    assert xbrl.parse_concept("T", "NetIncomeLoss", raw) == []


def test_fetch_concept_treats_a_404_as_absent_without_retrying(monkeypatch):
    # Measured: edgar.sec_get's default retry (3 tries, 1.5+3.0+4.5s backoff)
    # turns a 404 -- the normal outcome when a filer does not use a tag --
    # into 9.2s of pure backoff. A sweep probes ~4-6 absent tags per ticker,
    # so this must return on the first attempt, not sec_get's third.
    calls = []

    def fake_sec_get(url, tries=3):
        calls.append(tries)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(xbrl, "sec_get", fake_sec_get)
    monkeypatch.setattr(xbrl.time, "sleep", lambda s: pytest.fail(
        "a 404 must not sleep between retries; there is no retry"))

    with pytest.raises(urllib.error.HTTPError):
        xbrl.fetch_concept("0000320193", "SomeTagNobodyFiles")
    assert calls == [1], "a 404 must be tried exactly once"


def test_fetch_concept_still_retries_a_non_404_failure(monkeypatch):
    # A timeout, 503 or reset is genuinely transient, unlike a missing tag,
    # and must keep the exact 3-try, 1.5/3.0/4.5s-backoff schedule
    # edgar.sec_get itself would have given it.
    calls, sleeps = [], []

    def fake_sec_get(url, tries=3):
        calls.append(tries)
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(xbrl, "sec_get", fake_sec_get)
    monkeypatch.setattr(xbrl.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(urllib.error.HTTPError):
        xbrl.fetch_concept("0000320193", "SomeTag")
    assert len(calls) == 3, "a non-404 failure must still be retried 3 times"
    assert sleeps == [1.5, 3.0, 4.5]


# --------------------------------------------------------------------------- #
# splice                                                                       #
# --------------------------------------------------------------------------- #
def _rev(tag, end, start, filed, value=1.0):
    return xbrl.Fact("T", tag, start, end, None, None, "10-Q", filed, value)


def _span(tag, first_year, last_year):
    """One fact per year-end from first_year to last_year inclusive."""
    return [_rev(tag, f"{y}-12-31", f"{y}-01-01", f"{y + 1}-02-15")
            for y in range(first_year, last_year + 1)]


def test_splice_prefers_the_live_tag_over_the_one_with_more_facts():
    # MMM's shape: legacy has eleven years, modern has eight and is current.
    facts = (_span("SalesRevenueNet", 2007, 2017)
             + _span("Revenues", 2018, 2026))
    out = xbrl.splice(facts, xbrl.CONCEPTS["revenue"])
    assert max(f.period_end for f in out) == "2026-12-31", \
        "electing the tag with the most facts strands the series in 2017"


def test_splice_keeps_the_legacy_history_behind_the_live_tag():
    facts = (_span("SalesRevenueNet", 2007, 2017)
             + _span("Revenues", 2018, 2026))
    out = xbrl.splice(facts, xbrl.CONCEPTS["revenue"])
    assert min(f.period_end for f in out) == "2007-12-31"
    assert len(out) == 20, "every year from 2007 to 2026, none twice"


def test_splice_never_reports_a_period_from_two_tags():
    # Oracle's shape: both tags populated over the same years. The overlap must
    # come from the primary alone -- a period reported twice renders as a
    # revision that never happened.
    facts = (_span("Revenues", 2016, 2026)
             + _span("RevenueFromContractWithCustomerExcludingAssessedTax",
                     2016, 2026))
    out = xbrl.splice(facts, xbrl.CONCEPTS["revenue"])
    ends = [f.period_end for f in out]
    assert len(ends) == len(set(ends))
    assert len({f.concept for f in out}) == 1


def test_splice_picks_the_live_tag_that_is_not_the_newest_chain_member():
    # LMT's shape: the ASC 606 tag exists but is a stub; Revenues is live.
    facts = (_span("SalesRevenueNet", 2007, 2017)
             + _span("RevenueFromContractWithCustomerExcludingAssessedTax",
                     2017, 2019)
             + _span("Revenues", 2016, 2026))
    out = xbrl.splice(facts, xbrl.CONCEPTS["revenue"])
    assert {f.concept for f in out if f.period_end >= "2020-12-31"} \
        == {"Revenues"}
    assert max(f.period_end for f in out) == "2026-12-31"


def test_splice_of_a_single_tag_chain_is_that_tag():
    facts = _span("NetIncomeLoss", 2020, 2026)
    assert xbrl.splice(facts, xbrl.CONCEPTS["netIncome"]) == \
        sorted(facts, key=lambda f: (f.period_end, f.filed))


def test_splice_of_nothing_is_nothing():
    assert xbrl.splice([], xbrl.CONCEPTS["revenue"]) == []


def test_a_ticker_that_stopped_filing_still_resolves_a_primary():
    # Acquired in 2019. Liveness is relative to the chain's own newest fact,
    # so this must not come back empty.
    facts = (_span("SalesRevenueNet", 2007, 2016)
             + _span("Revenues", 2017, 2019))
    out = xbrl.splice(facts, xbrl.CONCEPTS["revenue"])
    assert max(f.period_end for f in out) == "2019-12-31"
    assert min(f.period_end for f in out) == "2007-12-31"


def test_splice_breaks_a_tie_toward_the_preferred_chain_member():
    facts = (_span("Revenues", 2020, 2026)
             + _span("RevenueFromContractWithCustomerExcludingAssessedTax",
                     2020, 2026))
    out = xbrl.splice(facts, xbrl.CONCEPTS["revenue"])
    assert {f.concept for f in out} == \
        {"RevenueFromContractWithCustomerExcludingAssessedTax"}, \
        "CONCEPTS lists the preferred tag first; a tie must respect that"


# --------------------------------------------------------------------------- #
# quarterly(): Q4 reconstruction defects                                      #
# --------------------------------------------------------------------------- #
def test_no_q4_is_synthesized_when_the_filer_published_that_quarter():
    """Defect C: AAPL's own Q4 starts one day after Q3 ends; the synthesized
    one starts on the day Q3 ends, so a key of (start, end, filed) misses."""
    filed = "2011-10-26"
    facts = [
        _rev("NetIncomeLoss", "2010-12-25", "2010-09-26", filed, 6.0e9),
        _rev("NetIncomeLoss", "2011-03-26", "2010-12-26", filed, 5.0e9),
        _rev("NetIncomeLoss", "2011-06-25", "2011-03-27", filed, 7.3e9),
        _rev("NetIncomeLoss", "2011-09-24", "2011-06-26", filed, 6.623e9),
        _rev("NetIncomeLoss", "2011-09-24", "2010-09-26", filed, 25.923e9),
    ]
    out = xbrl.quarterly(facts)
    q4 = [f for f in out if f.period_end == "2011-09-24"]
    assert len(q4) == 1, "the filer's own Q4 is already here; do not add one"
    assert q4[0].period_start == "2011-06-26", "keep the filer's, not ours"


def test_a_rolling_twelve_month_fact_is_not_decomposed():
    """Defect A: AMZN publishes TTM facts ending at quarter ends. Decomposing
    one emitted a Q4 of -518,000,000 where the truth was +82,000,000."""
    filed = "2013-04-26"
    facts = [
        _rev("NetIncomeLoss", "2012-06-30", "2012-04-01", filed, 7.0e6),
        _rev("NetIncomeLoss", "2012-09-30", "2012-07-01", filed, -274.0e6),
        _rev("NetIncomeLoss", "2012-12-31", "2012-10-01", filed, 97.0e6),
        _rev("NetIncomeLoss", "2013-03-31", "2013-01-01", filed, 82.0e6),
        # the rolling year, ending at a Q1 end rather than a fiscal year end
        _rev("NetIncomeLoss", "2013-03-31", "2012-04-01", filed, -88.0e6),
    ]
    out = xbrl.quarterly(facts)
    ends = [f.period_end for f in out]
    assert ends.count("2013-03-31") == 1, \
        "only the filer's real Q1 -- no Q4 synthesized from a rolling year"


def test_a_synthesized_quarter_that_is_not_a_quarter_is_dropped():
    """Defect B: three 80-day candidates tile an 380-day annual span from its
    own start and chain contiguously, so the tiling rule alone accepts them --
    but the 140-day remainder they leave is not a quarter, which is what
    AMZN DepreciationDepletionAndAmortization filed 2020-05-01 did with a
    183-day remainder shipped as a quarter worth 6,561,000,000 beside the
    filer's real 5,362,000,000.

    NOTE: this fixture replaces the brief's literal Step 4b text, which built
    an annual fact spanning 2019-01-01..2020-03-31 (455 days). That span is
    classified "other", not "annual", by classify_span's own bands, so
    quarterly() never attempted to decompose it and the test passed before
    any of the three rules existed. See task-10.5-report.md for the guard-
    mutation finding this uncovered.
    """
    filed = "2020-05-01"
    facts = [
        _rev("Dep", "2019-03-22", "2019-01-01", filed, 1.0e9),
        _rev("Dep", "2019-06-10", "2019-03-22", filed, 1.1e9),
        _rev("Dep", "2019-08-29", "2019-06-10", filed, 1.2e9),
        _rev("Dep", "2020-01-16", "2019-01-01", filed, 9.9e9),
    ]
    out = xbrl.quarterly(facts)
    for f in out:
        assert xbrl.classify_span(f.period_start, f.period_end) == "quarter", \
            f"emitted a {f.period_start}..{f.period_end} span as a quarter"


def test_three_quarters_that_start_well_after_the_annual_span_do_not_tile_it():
    """Rule 2 in isolation. Three 90-day quarters and a 95-day remainder are
    each individually quarter-shaped, so rule 3's reclassification would let
    this through, and no other kept quarter already ends where the annual
    fact ends, so rule 1 would too. Only the tiling requirement -- the first
    candidate must start within days of the annual span's own start -- catches
    that these three quarters begin ten days after the annual fact starts,
    leaving an unaccounted-for slice at the front that the reconstruction
    would otherwise silently drop.
    """
    filed = "2019-11-01"
    facts = [
        _rev("NetIncomeLoss", "2019-01-09", "2018-10-11", filed, 10.0e6),
        _rev("NetIncomeLoss", "2019-04-09", "2019-01-09", filed, 11.0e6),
        _rev("NetIncomeLoss", "2019-07-08", "2019-04-09", filed, 12.0e6),
        _rev("NetIncomeLoss", "2019-10-11", "2018-10-01", filed, 50.0e6),
    ]
    out = xbrl.quarterly(facts)
    assert [f for f in out if f.period_end == "2019-10-11"] == [], \
        "the three candidates start 10 days after the annual span begins"


def test_a_reconstructed_q4_equals_the_one_the_filer_published():
    """The property the 74.5% exact-agreement measurement establishes. When
    the filer's own Q4 is withheld, reconstruction must recover it exactly."""
    filed = "2011-10-26"
    quarters = [
        _rev("NetIncomeLoss", "2010-12-25", "2010-09-26", filed, 6.0e9),
        _rev("NetIncomeLoss", "2011-03-26", "2010-12-26", filed, 5.0e9),
        _rev("NetIncomeLoss", "2011-06-25", "2011-03-27", filed, 7.3e9),
    ]
    annual = _rev("NetIncomeLoss", "2011-09-24", "2010-09-26", filed, 24.923e9)
    out = xbrl.quarterly(quarters + [annual])
    q4 = [f for f in out if f.period_end == "2011-09-24"]
    assert len(q4) == 1
    assert q4[0].value == pytest.approx(6.623e9)


def _republished_annual():
    """Three real quarters plus one annual figure carried by three filings.

    SEC serves it exactly this way: the 10-K states the year, and every later
    filing that shows it as a prior-year comparative repeats it under its own
    filing date.
    """
    quarters = [
        _rev("NetIncomeLoss", "2010-12-25", "2010-09-26", "2011-10-26", 6.0e9),
        _rev("NetIncomeLoss", "2011-03-26", "2010-12-26", "2011-10-26", 5.0e9),
        _rev("NetIncomeLoss", "2011-06-25", "2011-03-27", "2011-10-26", 7.3e9),
    ]
    annuals = [_rev("NetIncomeLoss", "2011-09-24", "2010-09-26", d, 24.923e9)
               for d in ("2011-10-26", "2012-10-31", "2013-10-30")]
    return quarters + annuals


def test_a_republished_annual_yields_a_q4_for_each_filing_date():
    # Each filing is its own point in time: the same Q4 becomes knowable again
    # on each date, and _known_at picks the newest filed version as of any
    # date. Keeping only one would erase the earlier filings -- a TTM asked
    # for 2012 would find no Q4 at all, though the number was published in
    # October 2011.
    q4 = [f for f in xbrl.quarterly(_republished_annual())
          if f.period_end == "2011-09-24"]
    assert sorted(f.filed for f in q4) == \
        ["2011-10-26", "2012-10-31", "2013-10-30"]
    assert all(f.value == pytest.approx(6.623e9) for f in q4)


def test_which_filings_survive_does_not_depend_on_input_order():
    # The order SEC's JSON happens to arrive in must not decide which filing
    # dates reach the store -- filed is part of the primary key and drives
    # every point-in-time answer downstream.
    facts = _republished_annual()
    forward = xbrl.quarterly(facts)
    backward = xbrl.quarterly(list(reversed(facts)))
    key = lambda fs: sorted(  # noqa: E731
        (f.concept, f.period_start, f.period_end, f.filed, f.value) for f in fs)
    assert key(forward) == key(backward)


def test_quarterly_groups_by_concept_before_reconstructing_a_q4():
    # Once splice hands quarterly() a multi-tag stream, an annual fact under
    # one tag must never be reduced by quarters filed under a different tag --
    # that would subtract, say, three SalesRevenueNet quarters from a Revenues
    # year and emit the difference as a fabricated "Q4".
    filed = "2026-02-15"
    facts = [
        _rev("TagB", "2025-03-31", "2025-01-01", filed, 100.0),
        _rev("TagB", "2025-06-30", "2025-04-01", filed, 110.0),
        _rev("TagB", "2025-09-30", "2025-07-01", filed, 120.0),
        _rev("TagA", "2025-12-31", "2025-01-01", filed, 500.0),
    ]
    out = xbrl.quarterly(facts)
    assert [f for f in out if f.period_end == "2025-12-31"] == [], \
        "TagA's annual fact has no same-tag quarters to reconstruct a Q4 from"
