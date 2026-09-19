"""Generated output must not be committed.

A committed render is stale the next day, and a checkout silently restores it
over fresh output -- which is how dashboard.html came to show Costco at $947.74
a month after that was its price, while history.db held the correct $895.31.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return set(out.split())


@pytest.mark.parametrize("path", [
    "dashboard.html",
    "history.html",
])
def test_rendered_pages_are_not_tracked(path):
    assert path not in tracked(), (
        f"{path} is rendered from the store on every run; committing it means a "
        f"checkout replaces fresh output with a stale copy")


def test_no_report_is_tracked():
    bad = {p for p in tracked()
           if p.startswith("reports/") and p.endswith(".html")}
    assert not bad, f"generated reports must not be committed: {sorted(bad)}"


def test_daily_capture_is_not_tracked():
    """Arbitrary days in git back nothing up and make --no-fetch render stale."""
    bad = {p for p in tracked()
           if any(p.startswith(f"data/{k}_") for k in
                  ("fundamentals", "estimates", "holdings", "insiders"))}
    assert not bad, f"daily capture must not be committed: {sorted(bad)}"


@pytest.mark.parametrize("path", [
    "data/cusip_map.csv",             # checked-in reference, price-verified
    "data/ff_factors.csv.gz",         # checked-in reference
    "data/13f_filings.csv.gz",        # every filing looked at; not refetchable
    "data/13f_2026Q2.csv.gz",         # quarterly; an amended 13F is gone for good
    "tools/13f_report.template.html",  # source, not output
])
def test_reference_data_and_sources_stay_tracked(path):
    """The ignore rules must not overreach into what genuinely belongs in git."""
    assert path in tracked(), f"{path} should be version controlled"
