import pandas as pd
import pytest

import build_dashboard as bd


def _band():
    """Software — Infrastructure as it stood on 2026-09-04, EV/EBITDA."""
    return pd.DataFrame(
        {"evEbitda": [-27887.75, -93.92, 19.18, 19.77, 42.84,
                      145.42, 462.38, 885.52, 2020.94, 2065.37]},
        index=["NET", "SNOW", "ORCL", "MSFT", "FTNT",
               "PANW", "ZS", "DDOG", "CRWD", "MDB"],
    )


def test_negative_ev_ebitda_is_not_scored_at_all():
    df = _band()
    scores = bd.relative_scores(df, "evEbitda", False, bd.DOMAINS["evEbitda"])
    assert "NET" not in scores
    assert "SNOW" not in scores


def test_excluding_them_repairs_the_rest_of_the_band():
    df = _band()
    before = bd.relative_scores(df, "evEbitda", False)
    after = bd.relative_scores(df, "evEbitda", False, bd.DOMAINS["evEbitda"])
    # ORCL and MSFT are the cheapest real names and were barely tinted.
    assert before["ORCL"] == pytest.approx(0.18, abs=0.02)
    assert after["ORCL"] == pytest.approx(0.67, abs=0.02)
    assert after["ORCL"] > before["ORCL"] + 0.4


def test_net_debt_ebitda_keys_off_the_ebitda_sign_not_its_own():
    # BA: ~$50B net debt against negative EBITDA renders as -10.02, which reads
    # as net cash. GD's 0.78 and RTX's 1.92 are real, positive-EBITDA ratios.
    # RTX is added beyond the brief's 3-name fixture solely to clear
    # relative_scores' pre-existing `len(valid) < 3` floor (predates this task,
    # present since the initial commit): with only BA/GD/HWM, excluding BA
    # leaves 2 valid names and the whole band scores {} regardless of the
    # domain fix, which would make this test unable to observe the behavior
    # under test. The real Aerospace & Defense band has 8 names (BA, RTX, LMT,
    # GD, NOC, LHX, TDG, HWM), so this never happens in production.
    #
    # MDB is the discriminating row: -170.08 over its *own* column reads as
    # a bad ratio, but its EBITDA (2065.37) is positive, so -170.08 is a real
    # net-cash position and must keep scoring. A domain keyed off
    # netDebtEbitda's own sign (the bug being guarded against) would exclude
    # MDB right alongside BA; only keying off evEbitda's sign tells them apart.
    # Without this row, BA and GD alone can't catch that regression: BA's own
    # netDebtEbitda (-10.02) happens to share evEbitda's sign, so a domain
    # keyed off either column excludes it and the test passes either way.
    df = pd.DataFrame({"netDebtEbitda": [-10.02, 0.78, 1.47, 1.92, -170.08],
                       "evEbitda": [-67.40, 15.68, 38.65, 19.17, 2065.37]},
                      index=["BA", "GD", "HWM", "RTX", "MDB"])
    scores = bd.relative_scores(df, "netDebtEbitda", False,
                                bd.DOMAINS["netDebtEbitda"])
    assert "BA" not in scores
    assert "GD" in scores
    assert "MDB" in scores


def test_a_genuinely_negative_metric_is_still_scored():
    # A negative FCF yield or margin is meaningful, not undefined.
    df = pd.DataFrame({"fcfYield": [-2.0, 1.0, 3.0, 5.0]},
                      index=["A", "B", "C", "D"])
    scores = bd.relative_scores(df, "fcfYield", True, bd.DOMAINS.get("fcfYield"))
    assert set(scores) == {"A", "B", "C", "D"}


def test_a_band_that_falls_under_three_valid_names_scores_nothing():
    df = pd.DataFrame({"evEbitda": [-5.0, -3.0, 12.0]}, index=["A", "B", "C"])
    assert bd.relative_scores(df, "evEbitda", False,
                              bd.DOMAINS["evEbitda"]) == {}
