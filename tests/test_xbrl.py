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


def test_pick_tag_chooses_the_chain_member_with_the_most_facts():
    fetched = {
        "RevenueFromContractWithCustomerExcludingAssessedTax":
            _raw("xbrl_aapl_revenues"),
        "Revenues": b'{"units":{"USD":[]}}',
    }
    tag = xbrl.pick_tag("AAPL", xbrl.CONCEPTS["revenue"], fetched)
    assert tag == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_pick_tag_falls_through_to_the_second_member():
    fetched = {
        "RevenueFromContractWithCustomerExcludingAssessedTax":
            b'{"units":{"USD":[]}}',
        "Revenues": _raw("xbrl_ora_revenues"),
    }
    assert xbrl.pick_tag("ORCL", xbrl.CONCEPTS["revenue"], fetched) == "Revenues"


def test_pick_tag_returns_none_when_nothing_in_the_chain_has_facts():
    fetched = {t: b'{"units":{"USD":[]}}' for t in xbrl.CONCEPTS["revenue"]}
    assert xbrl.pick_tag("ZZZZ", xbrl.CONCEPTS["revenue"], fetched) is None


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


def test_pick_tag_prefers_the_larger_real_orcl_tag_over_chain_order():
    # Both Oracle revenue tags carry real, non-empty data: Revenues has 141
    # facts, RevenueFromContractWithCustomerExcludingAssessedTax has 104. A
    # chain-order rule ("first member with any facts") would pick the latter,
    # since it is listed first in CONCEPTS["revenue"] -- the count rule must
    # pick Revenues instead. Unlike the falls-through test, neither candidate
    # here is empty, so this is the only test the count rule cannot pass by
    # accident.
    fetched = {
        "RevenueFromContractWithCustomerExcludingAssessedTax":
            _raw("xbrl_ora_rev_contract"),
        "Revenues": _raw("xbrl_ora_revenues"),
    }
    assert xbrl.pick_tag("ORCL", xbrl.CONCEPTS["revenue"], fetched) == "Revenues"


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
