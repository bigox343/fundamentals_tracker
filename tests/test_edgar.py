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
