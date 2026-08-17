"""The margin-of-safety rule and model-disagreement handling (§6)."""

import pytest

from equity_analyst.config import Assumptions
from equity_analyst.valuation.models import (
    ModelResult,
    ValuationResult,
    _apply_margin_of_safety,
)


def _valuation(values, price, mos=0.50):
    res = ValuationResult(current_price=price, currency="USD", margin_of_safety=mos)
    names = ["Warren Buffett Way (DCF)", "Benjamin Graham Way", "Bharat Shah Way"]
    res.models = [ModelResult(name=n, value=v) for n, v in zip(names, values)]
    return res


def test_buy_below_is_half_the_selected_value():
    res = _valuation([100.0, 100.0, 100.0], price=40.0)
    _apply_margin_of_safety(res, Assumptions())
    assert res.selected_value == pytest.approx(100.0)
    assert res.buy_below == pytest.approx(50.0)


@pytest.mark.parametrize(
    "price,expected",
    [
        (40.0, "buy"),        # below the 50 line
        (50.0, "buy"),        # exactly on it counts as the buy zone
        (55.0, "near"),       # inside the 15% shoulder
        (90.0, "expensive"),  # no margin of safety
    ],
)
def test_zone_boundaries(price, expected):
    res = _valuation([100.0, 100.0, 100.0], price=price)
    _apply_margin_of_safety(res, Assumptions())
    assert res.zone == expected


def test_selected_value_is_the_median_not_the_mean():
    """One runaway model must not drag the central estimate up with it."""
    res = _valuation([1000.0, 50.0, 100.0], price=60.0)
    _apply_margin_of_safety(res, Assumptions())
    assert res.selected_value == pytest.approx(100.0)


def test_wide_dispersion_is_reported_and_explained():
    res = _valuation([1000.0, 50.0, 100.0], price=60.0)
    _apply_margin_of_safety(res, Assumptions())
    assert res.dispersion > 0.5
    assert "dispersion" in res.selected_basis
    assert res.divergence_note


def test_tight_cluster_scores_high_agreement():
    res = _valuation([100.0, 98.0, 102.0], price=40.0)
    _apply_margin_of_safety(res, Assumptions())
    assert res.agreement > 0.9


def test_no_intrinsic_value_publishes_no_buy_line():
    res = _valuation([None, None, None], price=60.0)
    _apply_margin_of_safety(res, Assumptions())
    assert res.buy_below is None
    assert res.zone == "unknown"
    assert any("no buy-below line" in n for n in res.notes)


def test_missing_price_leaves_zone_unknown_but_keeps_the_value():
    res = _valuation([100.0, 100.0, 100.0], price=None)
    _apply_margin_of_safety(res, Assumptions())
    assert res.buy_below == pytest.approx(50.0)
    assert res.zone == "unknown"


def test_custom_margin_of_safety_is_honoured():
    a = Assumptions()
    a.margin_of_safety = 0.30
    res = _valuation([100.0, 100.0, 100.0], price=60.0, mos=0.30)
    _apply_margin_of_safety(res, a)
    assert res.buy_below == pytest.approx(70.0)


def test_non_positive_values_are_excluded():
    res = _valuation([-10.0, 100.0, 120.0], price=40.0)
    _apply_margin_of_safety(res, Assumptions())
    assert res.selected_value == pytest.approx(110.0)
