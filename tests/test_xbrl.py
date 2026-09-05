import json
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
