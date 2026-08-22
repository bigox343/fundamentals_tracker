"""The target_weights record: idempotent writes and per-leg attribution."""
import pytest

import history
from history import TargetWeightRow


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    history.ensure_schema(c)
    return c


def _row(ticker, weight, status="optimal"):
    return TargetWeightRow("2026-08-21", ticker, weight, weight * 2,
                           0.1, 0.2, 0.3, status)


def test_round_trips_weights_and_contributions(conn):
    history.upsert_target_weights(conn, [_row("NVDA", 0.03), _row("INTC", -0.03)])
    out = history.target_weights(conn, "2026-08-21").set_index("ticker")

    assert out.loc["NVDA", "weight"] == pytest.approx(0.03)
    assert out.loc["INTC", "weight"] == pytest.approx(-0.03)
    assert out.loc["NVDA", "contrib_13f"] == pytest.approx(0.2)


def test_rerunning_a_date_replaces_rather_than_duplicates(conn):
    history.upsert_target_weights(conn, [_row("NVDA", 0.03)])
    history.upsert_target_weights(conn, [_row("NVDA", 0.01)])
    out = history.target_weights(conn, "2026-08-21")

    assert len(out) == 1
    assert out.iloc[0]["weight"] == pytest.approx(0.01)


def test_solver_status_is_preserved(conn):
    history.upsert_target_weights(conn, [_row("NVDA", 0.03, "relaxed:gross")])
    out = history.target_weights(conn, "2026-08-21")

    assert out.iloc[0]["solver_status"] == "relaxed:gross"
