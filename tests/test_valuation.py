"""The valuation models (§6), checked against hand-computed values.

These are the numbers a subscriber acts on, so each model is verified against
arithmetic done independently of the implementation, not against a snapshot of
whatever the code happened to produce.
"""

import math

import pytest

from equity_analyst.config import Assumptions
from equity_analyst.valuation.models import (
    bharat_shah,
    buffetology,
    buffett_dcf,
    graham,
)


@pytest.fixture
def a():
    return Assumptions()


# -- Warren Buffett Way ----------------------------------------------------


def test_dcf_matches_independent_arithmetic(a):
    fcf0, shares, net_cash = 100.0, 10.0, 0.0
    res = buffett_dcf(fcf0, shares, net_cash, a)

    pv = 0.0
    fcf = fcf0
    for year in range(1, a.forecast_years + 1):
        fcf *= 1 + a.fcf_growth
        pv += fcf / (1 + a.discount_rate) ** year
    terminal = fcf * (1 + a.terminal_growth) / (a.discount_rate - a.terminal_growth)
    pv += terminal / (1 + a.discount_rate) ** a.forecast_years

    assert res.value == pytest.approx(pv / shares, rel=1e-9)


def test_dcf_adds_net_cash_and_subtracts_debt(a):
    base = buffett_dcf(100.0, 10.0, 0.0, a).value
    with_cash = buffett_dcf(100.0, 10.0, 500.0, a).value
    with_debt = buffett_dcf(100.0, 10.0, -500.0, a).value
    assert with_cash == pytest.approx(base + 50.0)
    assert with_debt == pytest.approx(base - 50.0)


def test_dcf_refuses_negative_base_fcf(a):
    res = buffett_dcf(-50.0, 10.0, 0.0, a)
    assert res.value is None
    assert "negative" in res.unavailable_reason


def test_dcf_refuses_when_discount_below_terminal_growth(a):
    a.discount_rate, a.terminal_growth = 0.02, 0.05
    res = buffett_dcf(100.0, 10.0, 0.0, a)
    assert res.value is None
    assert "diverges" in res.unavailable_reason


def test_dcf_flags_terminal_value_dominance(a):
    res = buffett_dcf(100.0, 10.0, 0.0, a)
    assert res.inputs["terminal_share_of_value"] > 0.5
    assert any("terminal value" in c for c in res.caveats)


# -- Graham ----------------------------------------------------------------


def test_graham_number_is_the_textbook_formula(a):
    eps, bvps = 5.0, 40.0
    res = graham(eps, bvps, None, a)
    assert res.value == pytest.approx(math.sqrt(22.5 * eps * bvps))


def test_graham_takes_the_lower_of_the_two_forms(a):
    eps, bvps = 5.0, 40.0
    res = graham(eps, bvps, 12.0, a)
    number = math.sqrt(22.5 * eps * bvps)
    earnings = eps * (a.graham_base_pe + 2 * 12.0)
    assert res.value == pytest.approx(min(number, earnings))


def test_graham_caps_growth_conservatively(a):
    """A 90% growth rate must not inflate the conservative floor."""
    res = graham(5.0, 40.0, 90.0, a)
    assert res.inputs["capped_growth_pct"] == pytest.approx(a.graham_growth_cap * 100)


def test_graham_undefined_on_a_loss(a):
    assert graham(-2.0, 40.0, 10.0, a).value is None
    assert graham(5.0, -40.0, 10.0, a).value is None


# -- Bharat Shah -----------------------------------------------------------


def test_bharat_shah_compounds_at_roiic_times_reinvestment(a):
    quality = {"clears_all_three": False, "metrics": {}}
    res = bharat_shah(10.0, 12.0, 0.5, quality, a, horizon=10)
    g = 0.12 * 0.5                        # 6%
    expected = 10.0 * (1 + g) ** 10 * a.avg_sustainable_pe / (1 + a.discount_rate) ** 10
    assert res.value == pytest.approx(expected)


def test_bharat_shah_caps_runaway_compounding(a):
    """ROIIC 80% x 90% reinvestment is capped at the DCF growth ceiling."""
    res = bharat_shah(10.0, 80.0, 0.9, {"metrics": {}}, a)
    assert res.inputs["intrinsic_compounding_rate"] == pytest.approx(a.fcf_growth)
    assert any("capped" in c for c in res.caveats)


def test_bharat_shah_rewards_the_quality_gate(a):
    plain = bharat_shah(10.0, 12.0, 0.5, {"metrics": {}}, a)
    quality = bharat_shah(
        10.0, 12.0, 0.5, {"clears_all_three": True, "metrics": {}}, a
    )
    assert quality.value > plain.value


def test_bharat_shah_needs_measured_roiic(a):
    res = bharat_shah(10.0, None, 0.5, {"metrics": {}}, a)
    assert res.value is None
    assert "ROIIC" in res.unavailable_reason


# -- Buffetology -----------------------------------------------------------


def test_buffetology_annualises_per_the_workbook(a):
    eps, price = 5.0, 60.0
    res = buffetology(eps, 30.0, price, 10.0, None, None, None, a)
    eps10 = eps * 1.10 ** 10
    total = a.avg_sustainable_pe * eps10       # no dividends supplied
    expected = (total / price) ** (1 / a.return_annualisation_years) - 1
    assert res.expected_return["historical"] == pytest.approx(expected)


def test_buffetology_computes_both_growth_legs(a):
    res = buffetology(5.0, 30.0, 60.0, 10.0, 20.0, 40.0, 1.0, a)
    assert res.expected_return["historical"] is not None
    assert res.expected_return["sustainable"] is not None
    # sustainable growth = RoE x retention = 20% x 0.6 = 12%
    assert res.inputs["sustainable_growth"] == pytest.approx(0.12)


def test_buffetology_dividends_raise_the_expected_return(a):
    without = buffetology(5.0, 30.0, 60.0, 10.0, None, None, None, a)
    with_div = buffetology(5.0, 30.0, 60.0, 10.0, None, None, 2.0, a)
    assert with_div.expected_return["historical"] > without.expected_return["historical"]


def test_buffetology_needs_a_price(a):
    res = buffetology(5.0, 30.0, None, 10.0, None, None, None, a)
    assert res.expected_return is None
    assert "price" in res.unavailable_reason
