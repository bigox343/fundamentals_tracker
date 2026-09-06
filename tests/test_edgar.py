"""13F parsing, filtering and resolution. No network."""
from datetime import date
from pathlib import Path

import pytest

import edgar

FIXTURES = Path(__file__).parent / "fixtures"


def _xml(name):
    return (FIXTURES / name).read_text()


# --------------------------------------------------------------------------
# Quarters
# --------------------------------------------------------------------------

def test_quarter_ends_excludes_a_quarter_still_inside_its_filing_window():
    """Q2 closes 30 June; the 45-day deadline falls on 14 August."""
    assert edgar.quarter_ends(1, date(2026, 8, 13))[0] == "2026-03-31"
    assert edgar.quarter_ends(1, date(2026, 8, 14))[0] == "2026-06-30"


def test_quarter_ends_walks_back_across_a_year_boundary():
    """On 1 March, Q4's deadline (14 February) has passed but Q1's has not."""
    assert edgar.quarter_ends(3, date(2026, 3, 1)) == [
        "2025-12-31", "2025-09-30", "2025-06-30"]


@pytest.mark.parametrize("quarter,expected", [
    ("2026-06-30", "2026-03-31"),
    ("2026-03-31", "2025-12-31"),
    ("2025-12-31", "2025-09-30"),
])
def test_previous_quarter(quarter, expected):
    assert edgar.previous_quarter(quarter) == expected


# --------------------------------------------------------------------------
# Parsing -- the namespace trap
# --------------------------------------------------------------------------

def test_parses_filings_with_bare_tags():
    records = edgar.parse_info_table(_xml("13f_bare_namespace.xml"))
    assert len(records) == 4
    assert records[1]["nameOfIssuer"] == "ADVANCED MICRO DEVICES INC"
    assert records[1]["cusip"] == "007903107"


def test_parses_filings_whose_tags_carry_a_namespace_prefix():
    """Millennium emits <n1:infoTable>, Tiger Global emits <infoTable>.

    A parser keyed on the bare tag returns zero rows here, which reads as a
    manager holding nothing rather than as a parse failure.
    """
    records = edgar.parse_info_table(_xml("13f_n1_namespace.xml"))
    assert len(records) == 4
    assert records[0]["cusip"] == "68243Q106"


def test_both_namespace_forms_yield_the_same_fields():
    bare = edgar.parse_info_table(_xml("13f_bare_namespace.xml"))[0]
    prefixed = edgar.parse_info_table(_xml("13f_n1_namespace.xml"))[0]
    required = {"nameOfIssuer", "titleOfClass", "cusip", "value",
                "sshPrnamt", "sshPrnamtType"}
    assert required <= set(bare) and required <= set(prefixed)


def test_empty_input_yields_no_rows():
    assert edgar.parse_info_table("") == []
    assert edgar.parse_info_table("   ") == []


# --------------------------------------------------------------------------
# The equity filter
# --------------------------------------------------------------------------

def _line(**over):
    base = {"cusip": "67066G104", "titleOfClass": "COM", "value": "1000",
            "sshPrnamt": "10", "sshPrnamtType": "SH"}
    base.update(over)
    return base


def test_plain_common_stock_is_equity():
    assert edgar.is_equity_line(_line())


def test_option_lines_are_rejected():
    """Nearly half of Citadel's reported lines are puts and calls."""
    assert not edgar.is_equity_line(_line(putCall="Call"))
    assert not edgar.is_equity_line(_line(putCall="Put"))


def test_bond_principal_is_rejected():
    assert not edgar.is_equity_line(_line(sshPrnamtType="PRN"))


def test_convertible_note_cusips_are_rejected():
    """833445AB5 is a Snowflake convertible note, not Snowflake equity.

    Issue-type characters (positions 7-8) are digits for equity, letters for
    debt -- which is what separates it from 833445109.
    """
    assert not edgar.is_equity_line(_line(cusip="833445AB5"))
    assert not edgar.is_equity_line(_line(cusip="25809KAB1"))
    assert edgar.is_equity_line(_line(cusip="833445109"))


@pytest.mark.parametrize("title", [
    "NOTE 10/0", "NOTE 0.500% 3/0", "DEP SHS RP1/20 A", "PFD SER A", "WTS",
])
def test_non_common_share_classes_are_rejected(title):
    assert not edgar.is_equity_line(_line(titleOfClass=title))


def test_malformed_cusips_are_rejected():
    assert not edgar.is_equity_line(_line(cusip=""))
    assert not edgar.is_equity_line(_line(cusip="12345"))


# --------------------------------------------------------------------------
# Resolution into the universe
# --------------------------------------------------------------------------

def test_position_rows_keeps_only_mapped_tickers_but_counts_the_whole_book():
    """Off-universe positions are dropped from the store yet still counted.

    The store holds universe rows only, so a position's share of the manager's
    book is computable only because the whole-book total is measured here.
    """
    records = [
        _line(cusip="007903107", value="400", sshPrnamt="4"),   # mapped
        _line(cusip="999999109", value="600", sshPrnamt="6"),   # off-universe
        _line(cusip="833445AB5", value="900", sshPrnamt="9"),   # a bond
    ]
    rows, n_equity, book = edgar.position_rows(
        "0001", "Fund", "2026-06-30", records, {"007903107": "AMD"})
    assert [r.ticker for r in rows] == ["AMD"]
    assert n_equity == 2          # the bond is not equity
    assert book == 1000.0         # but the off-universe equity still counts


def test_repeated_cusip_lines_are_summed_not_overwritten():
    """A manager may report one CUSIP on several lines.

    The store's key is (cik, quarter, cusip), so without summing here the last
    line would silently replace the rest of the position.
    """
    records = [_line(cusip="007903107", value="400", sshPrnamt="4"),
               _line(cusip="007903107", value="600", sshPrnamt="6")]
    rows, _, _ = edgar.position_rows(
        "0001", "Fund", "2026-06-30", records, {"007903107": "AMD"})
    assert len(rows) == 1
    assert rows[0].shares == 10.0
    assert rows[0].value == 1000.0


def test_padded_issuer_names_are_squeezed():
    records = edgar.parse_info_table(_xml("13f_n1_namespace.xml"))
    rows, _, _ = edgar.position_rows(
        "0001", "Fund", "2026-06-30", records, {"68243Q106": "FLWS"})
    assert rows[0].issuer == "1 800 FLOWERS COM INC"
    assert rows[0].title_class == "CL A"


def test_an_empty_cusip_map_stores_nothing_but_still_measures_the_book():
    records = edgar.parse_info_table(_xml("13f_bare_namespace.xml"))
    rows, n_equity, book = edgar.position_rows(
        "0001", "Fund", "2026-06-30", records, {})
    assert rows == []
    assert n_equity > 0 and book > 0


# --------------------------------------------------------------------------
# The filings manifest
# --------------------------------------------------------------------------

def _filing(cik="1", quarter="2026-06-30", n=5, status="ok"):
    from history import FilingRow
    return FilingRow(cik, "Fund", "Tiger", quarter, f"acc-{cik}-{quarter}",
                     "2026-08-14", n, n, 1000.0, status)


def test_manifest_round_trips(tmp_path):
    path = tmp_path / "13f_filings.csv.gz"
    edgar.write_filings_manifest([_filing()], path)
    assert edgar.read_filings_manifest(path) == [_filing()]


def test_manifest_merges_rather_than_replacing(tmp_path):
    """Each run fetches only some quarters; the manifest is cumulative."""
    path = tmp_path / "13f_filings.csv.gz"
    edgar.write_filings_manifest([_filing(quarter="2026-03-31")], path)
    edgar.write_filings_manifest([_filing(quarter="2026-06-30")], path)
    assert {r.quarter for r in edgar.read_filings_manifest(path)} == {
        "2026-03-31", "2026-06-30"}


def test_manifest_updates_a_restated_quarter_in_place(tmp_path):
    path = tmp_path / "13f_filings.csv.gz"
    edgar.write_filings_manifest([_filing(n=5)], path)
    edgar.write_filings_manifest([_filing(n=9)], path)
    rows = edgar.read_filings_manifest(path)
    assert len(rows) == 1 and rows[0].n_positions == 9


def test_a_filing_with_no_positions_survives_a_rebuild(tmp_path):
    """The defect the manifest exists to fix.

    Viking Global's Q1 2026 information table was empty, so it leaves no rows
    in the position archives. Rebuilt from those alone, the store forgets the
    filing happened -- and every position held the quarter before renders as an
    exit that never occurred.
    """
    import pandas as pd
    archive = tmp_path / "13f_2026Q1.csv.gz"
    pd.DataFrame([{"cik": "2", "fund": "Other", "cohort": "Tiger",
                   "quarter": "2026-03-31", "accession": "acc-2",
                   "filed_date": "2026-05-15", "cusip": "007903107",
                   "issuer": "AMD", "title_class": "COM", "shares": "10",
                   "share_type": "SH", "value": "1000", "put_call": ""}]
                 ).to_csv(archive, index=False, compression="gzip")
    manifest = tmp_path / "13f_filings.csv.gz"
    edgar.write_filings_manifest(
        [_filing(cik="1", quarter="2026-03-31", n=0)], manifest)

    _rows, filings = edgar.ingest_archives(
        [archive], {"007903107": "AMD"}, manifest)

    assert ("1", "2026-03-31") in {(f.cik, f.quarter) for f in filings}
    assert ("2", "2026-03-31") in {(f.cik, f.quarter) for f in filings}


def test_ingest_without_a_manifest_still_works(tmp_path):
    """Archives written before the manifest existed must remain ingestible."""
    import pandas as pd
    archive = tmp_path / "13f_2026Q1.csv.gz"
    pd.DataFrame([{"cik": "2", "fund": "Other", "cohort": "Tiger",
                   "quarter": "2026-03-31", "accession": "acc-2",
                   "filed_date": "2026-05-15", "cusip": "007903107",
                   "issuer": "AMD", "title_class": "COM", "shares": "10",
                   "share_type": "SH", "value": "1000", "put_call": ""}]
                 ).to_csv(archive, index=False, compression="gzip")
    rows, filings = edgar.ingest_archives([archive], {"007903107": "AMD"})
    assert len(rows) == 1 and len(filings) == 1


def test_sec_get_does_not_back_off_after_its_final_attempt(monkeypatch):
    # The sleep before a retry is the point; the sleep before raising is dead
    # wall clock. A full XBRL sweep takes the failure path thousands of times.
    slept = []
    monkeypatch.setattr(edgar.time, "sleep", lambda s: slept.append(s))

    def always_fail(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(edgar.urllib.request, "urlopen", always_fail)
    with pytest.raises(OSError):
        edgar.sec_get("https://example.invalid/x", tries=3)
    assert len(slept) == 2, f"3 attempts need 2 waits, not {len(slept)}: {slept}"
