import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "backfill_xbrl", Path(__file__).parent.parent / "tools" / "backfill_xbrl.py")
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_ticker_never_swept_is_always_due():
    assert backfill.due(["AAPL"], {}, "2026-09-05") == ["AAPL"]


def test_a_ticker_filed_inside_the_window_is_not_due():
    assert backfill.due(["AAPL"], {"AAPL": "2026-07-31"}, "2026-09-05") == []


def test_a_ticker_whose_last_filing_has_gone_stale_is_due():
    # 2026-04-30 is 128 days before 2026-09-05, past the ~85-day window.
    assert backfill.due(["AAPL"], {"AAPL": "2026-04-30"}, "2026-09-05") == ["AAPL"]


def test_sweep_ticker_stores_every_chain_member_it_fetched(monkeypatch):
    # The election (xbrl.splice) now happens at read time, not at sweep time.
    # Storing every tag the sweep already fetched -- rather than throwing away
    # all but one -- costs no additional HTTP requests and lets a future
    # change to the election rule apply without a re-fetch.
    calls = []

    def fake_fetch(cik, tag, taxonomy="us-gaap"):
        calls.append(tag)
        if tag == "RevenueFromContractWithCustomerExcludingAssessedTax":
            return (FIXTURES / "xbrl_aapl_revenues.json").read_bytes()
        if tag == "NetIncomeLoss":
            return (FIXTURES / "xbrl_aapl_netincome.json").read_bytes()
        return b'{"units":{"USD":[]}}'

    facts = backfill.sweep_ticker("AAPL", "0000320193", fake_fetch)
    tags = {f.concept for f in facts}
    assert "RevenueFromContractWithCustomerExcludingAssessedTax" in tags
    assert all(f.ticker == "AAPL" for f in facts)
    # Every tag in the revenue chain was fetched, even though only one of
    # them carried any facts in this fixture.
    assert set(backfill.xbrl.CONCEPTS["revenue"]) <= set(calls)
    # The dei chain (the cover-page share count) is swept too, not only
    # us-gaap.
    assert set(backfill.xbrl.DEI_CONCEPTS["sharesOutstanding"]) <= set(calls)


def test_sweep_ticker_fetches_the_dei_chain_under_the_dei_taxonomy():
    # DEI_CONCEPTS lives on the same companyconcept endpoint as CONCEPTS, one
    # path segment apart (dei vs us-gaap). A sweep that fetched every tag
    # under "us-gaap" would come back empty for a dei-only tag -- SEC would
    # 404 it -- so the taxonomy actually requested per tag matters, not just
    # the tag name.
    seen = {}

    def fake_fetch(cik, tag, taxonomy="us-gaap"):
        seen[tag] = taxonomy
        return b'{"units":{"USD":[]}}' if taxonomy == "us-gaap" \
            else b'{"units":{"shares":[]}}'

    backfill.sweep_ticker("AAPL", "0000320193", fake_fetch)
    assert seen["EntityCommonStockSharesOutstanding"] == "dei"
    assert seen["NetIncomeLoss"] == "us-gaap"


def test_sweep_ticker_never_reconstructs_a_q4_for_the_dei_share_count():
    # EntityCommonStockSharesOutstanding is an instant (a count on the filing's
    # cover date), like cash and debt -- xbrl.quarterly() would silently drop
    # every one of these facts if it ever saw them, since an instant fact has
    # no duration to match against a three-month span.
    raw = (b'{"units":{"shares":[{"start":"","end":"2026-06-30",'
           b'"val":1000.0,"fy":2026,"fp":"Q2","form":"10-Q",'
           b'"filed":"2026-07-31"}]}}')

    def fake_fetch(cik, tag, taxonomy="us-gaap"):
        if tag == "EntityCommonStockSharesOutstanding":
            return raw
        return b'{"units":{"USD":[]}}'

    facts = backfill.sweep_ticker("AAPL", "0000320193", fake_fetch)
    shares = [f for f in facts if f.concept == "EntityCommonStockSharesOutstanding"]
    assert len(shares) == 1
    assert shares[0].value == 1000.0


def test_a_ticker_that_fails_does_not_abort_the_sweep(monkeypatch):
    def fake_fetch(cik, tag, taxonomy="us-gaap"):
        raise RuntimeError("503 from SEC")

    facts = backfill.sweep_ticker("AAPL", "0000320193", fake_fetch)
    assert facts == [], "a failure yields nothing, it does not raise"
