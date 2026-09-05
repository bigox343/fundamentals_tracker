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


def test_sweep_ticker_holds_one_tag_per_concept(monkeypatch):
    calls = []

    def fake_fetch(cik, tag):
        calls.append(tag)
        if tag == "RevenueFromContractWithCustomerExcludingAssessedTax":
            return (FIXTURES / "xbrl_aapl_revenues.json").read_bytes()
        if tag == "NetIncomeLoss":
            return (FIXTURES / "xbrl_aapl_netincome.json").read_bytes()
        return b'{"units":{"USD":[]}}'

    facts = backfill.sweep_ticker("AAPL", "0000320193", fake_fetch)
    tags = {f.concept for f in facts}
    assert "RevenueFromContractWithCustomerExcludingAssessedTax" in tags
    assert "Revenues" not in tags, "the chain must resolve to one tag, not mix"
    assert all(f.ticker == "AAPL" for f in facts)


def test_a_ticker_that_fails_does_not_abort_the_sweep(monkeypatch):
    def fake_fetch(cik, tag):
        raise RuntimeError("503 from SEC")

    facts = backfill.sweep_ticker("AAPL", "0000320193", fake_fetch)
    assert facts == [], "a failure yields nothing, it does not raise"
