"""§5 technical indicators, checked against known values."""

import pytest

from equity_analyst.analysis import cagr, linear_slope, pearson
from equity_analyst.analysis.technicals import (
    atr,
    bollinger,
    ema_series,
    rsi,
    sma_series,
)


def test_sma_is_a_plain_rolling_mean():
    values = [1, 2, 3, 4, 5, 6]
    out = sma_series(values, 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[-1] == pytest.approx(5.0)


def test_ema_seeds_on_the_sma_then_smooths():
    values = [1, 2, 3, 4, 5]
    out = ema_series(values, 3)
    assert out[2] == pytest.approx(2.0)          # seed = mean(1,2,3)
    k = 2 / 4
    assert out[3] == pytest.approx(4 * k + 2.0 * (1 - k))


def test_rsi_is_100_when_price_only_rises():
    assert rsi(list(range(1, 40)), 14) == pytest.approx(100.0)


def test_rsi_is_zero_when_price_only_falls():
    assert rsi(list(range(40, 1, -1)), 14) == pytest.approx(0.0)


def test_rsi_sits_mid_range_on_an_alternating_series():
    values = [10 + (1 if i % 2 else -1) for i in range(60)]
    assert 35 < rsi(values, 14) < 65


def test_atr_on_a_constant_range():
    highs = [11.0] * 30
    lows = [9.0] * 30
    closes = [10.0] * 30
    assert atr(highs, lows, closes, 14) == pytest.approx(2.0)


def test_bollinger_bands_straddle_the_mean():
    values = [10.0, 12.0, 11.0, 13.0, 9.0] * 6
    upper, mid, lower = bollinger(values, 20, 2.0)
    assert lower < mid < upper
    assert mid == pytest.approx(sum(values[-20:]) / 20)


def test_indicators_return_none_when_history_is_too_short():
    assert atr([1.0], [1.0], [1.0], 14) is None
    assert bollinger([1.0, 2.0], 20)[0] is None
    assert sma_series([1.0, 2.0], 20)[-1] is None


# -- statistics used by the linkage ---------------------------------------


def test_pearson_detects_a_perfect_relationship():
    xs = [1, 2, 3, 4, 5]
    assert pearson(xs, [2, 4, 6, 8, 10]) == pytest.approx(1.0)
    assert pearson(xs, [10, 8, 6, 4, 2]) == pytest.approx(-1.0)


def test_linear_slope_recovers_the_coefficient():
    xs = [1, 2, 3, 4, 5]
    assert linear_slope(xs, [3 * x + 7 for x in xs]) == pytest.approx(3.0)


def test_correlation_needs_enough_points():
    assert pearson([1, 2], [1, 2]) is None


def test_cagr_refuses_a_non_positive_base():
    """You cannot compound out of a loss, so the rate is undefined, not 0."""
    assert cagr(-10.0, 100.0, 5) is None
    assert cagr(0.0, 100.0, 5) is None
    assert cagr(100.0, 200.0, 0) is None


def test_cagr_matches_the_formula():
    assert cagr(100.0, 200.0, 10) == pytest.approx((2 ** 0.1 - 1) * 100)


# -- integration against the sample run -----------------------------------


def test_technical_bias_is_reported_with_an_invalidation_level(report):
    tech = report.technicals
    assert tech.bias in {"bullish", "neutral", "bearish"}
    assert tech.rationale
    assert tech.invalidation.get("note")


def test_support_sits_below_and_resistance_above_the_price(report):
    tech = report.technicals
    assert all(level < tech.last_close for level in tech.support)
    assert all(level > tech.last_close for level in tech.resistance)
