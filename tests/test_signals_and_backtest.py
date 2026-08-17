"""§S Regime, strategy gating, backtesting, risk sizing and the paper book.

Ported from `indian-stock-signal-ai`. The behaviour worth pinning is the
conservative side: a gate failure must beat a high score, the backtester must
not peek ahead, and the risk model must refuse rather than silently size zero.
"""

import math

import pytest

from equity_analyst.analysis.regime import (
    detect_regime,
    fundamental_score,
    snapshot,
    technical_score,
)
from equity_analyst.analysis.technicals import (
    adx_series,
    relative_volume,
    vwap_series,
)
from equity_analyst.config import Assumptions, benchmark_for
from equity_analyst.strategies import engine as eng
from equity_analyst.trading.risk import size_position


@pytest.fixture
def a():
    return Assumptions()


def _bars(closes, volume=1000.0):
    """Daily bars with a small symmetric range around each close."""
    return [
        {
            "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "open": c, "high": c * 1.01, "low": c * 0.99,
            "close": c, "volume": volume,
        }
        for i, c in enumerate(closes)
    ]


# -- indicators ------------------------------------------------------------


def test_adx_is_high_in_a_clean_trend():
    closes = [100 + i for i in range(80)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    adx, plus_di, minus_di = adx_series(highs, lows, closes, 14)
    assert adx[-1] > 40                 # a one-way ramp is maximally trending
    assert plus_di[-1] > minus_di[-1]


def test_adx_is_low_in_chop():
    closes = [100 + (2 if i % 2 else -2) for i in range(80)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    adx, _, _ = adx_series(highs, lows, closes, 14)
    assert adx[-1] < 25


def test_adx_returns_full_length_padded_series():
    closes = [100 + i * 0.5 for i in range(60)]
    adx, plus_di, minus_di = adx_series(closes, closes, closes, 14)
    assert len(adx) == len(plus_di) == len(minus_di) == 60


def test_adx_needs_history():
    adx, _, _ = adx_series([1, 2, 3], [1, 2, 3], [1, 2, 3], 14)
    assert all(v is None for v in adx)


def test_vwap_equals_price_when_price_is_flat():
    closes = [50.0] * 30
    out = vwap_series(closes, closes, closes, [100.0] * 30)
    assert out[-1] == pytest.approx(50.0)


def test_vwap_is_volume_weighted():
    """A huge-volume bar at 10 must drag VWAP toward 10, not to the midpoint."""
    closes = [10.0, 20.0]
    out = vwap_series(closes, closes, closes, [1_000_000.0, 1.0])
    assert out[-1] < 11.0


def test_relative_volume_spots_a_surge():
    volumes = [100.0] * 20 + [500.0]
    assert relative_volume(volumes, 20) == pytest.approx(5.0)


# -- regime ----------------------------------------------------------------


def test_regime_bullish_when_stacked_up(a):
    closes = [100 + i * 0.5 for i in range(260)]
    regime = detect_regime(_bars(closes), a, "^NSEI")
    assert regime.regime == "bullish"
    assert regime.benchmark == "^NSEI"


def test_regime_bearish_when_stacked_down(a):
    closes = [300 - i * 0.5 for i in range(260)]
    assert detect_regime(_bars(closes), a, "^NSEI").regime == "bearish"


def test_regime_volatile_overrides_direction(a):
    closes = [100 + i * 0.5 for i in range(255)]
    closes += [closes[-1] * 0.85]          # a sudden 15% drop
    assert detect_regime(_bars(closes), a, "^NSEI").regime == "volatile"


def test_regime_unknown_without_data(a):
    regime = detect_regime([], a, "^NSEI")
    assert regime.regime == "unknown"
    assert regime.drivers


def test_benchmark_defaults_by_market():
    assert benchmark_for("India") == "^NSEI"
    assert benchmark_for("US") == "^GSPC"
    assert benchmark_for("US", "^DJI") == "^DJI"     # explicit override wins


# -- scores ----------------------------------------------------------------


def test_technical_score_rewards_a_clean_uptrend(a):
    closes = [100 + i * 0.5 for i in range(260)]
    snap = snapshot(_bars(closes), a)
    score, reasons = technical_score(snap, "trend_momentum")
    assert score >= 60
    assert reasons


def test_mean_reversion_scores_an_oversold_pullback_higher(a):
    closes = [100 + i * 0.4 for i in range(240)] + [
        100 + 240 * 0.4 - i * 3 for i in range(20)
    ]
    snap = snapshot(_bars(closes), a)
    mr, _ = technical_score(snap, "mean_reversion")
    tm, _ = technical_score(snap, "trend_momentum")
    assert mr > tm


def test_scores_are_bounded_and_zero_without_data():
    score, reasons = technical_score({"ok": False}, "trend_momentum")
    assert score == 0.0
    assert reasons == ["insufficient data"]


def test_fundamental_score_flags_thin_data(a):
    class Empty:
        annual, growth, quality, margins, leverage = [], {}, {}, [], []

    out = fundamental_score(Empty(), a)
    assert out["fundamental_score"] is None
    assert out["data_complete"] is False


def test_fundamental_score_from_a_real_run(report_signals):
    out = report_signals.trade_scores
    assert 0 <= out["fundamental_score"] <= 100
    assert out["subscores"]


# -- strategy gating -------------------------------------------------------


def test_library_loads_nine_strategies():
    assert len(eng.STRATEGIES["strategies"]) == 9
    assert set(eng.ARCHETYPE) == {f"S{i}" for i in range(1, 10)}


def test_a_failed_gate_beats_a_high_score(a):
    """The library's own rule: any hard-gate failure is no_trade regardless."""
    snap = {
        "ok": True, "close": 100.0, "atr14": 2.0, "rsi14": 60.0,
        "above_50dma": False,          # the gate that must bite
        "above_200dma": True, "ema_stack_up": True, "macd_bull": True,
        "trending": True, "di_bull": True, "above_vwap": True, "adx14": 30.0,
        "rvol": 3.0, "bb_lower": 90.0,
    }

    class R:
        regime = "bullish"

    res = eng.evaluate(snap, {"fundamental_score": 95.0, "data_complete": True}, R(), a)
    trend = [s for s in res.signals if s.archetype == "trend_momentum"]
    assert trend, "expected trend strategies in the library"
    for s in trend:
        assert s.bias == "no_trade"
        assert any("50DMA" in g for g in s.gate_failures)


def test_regime_mismatch_blocks_a_strategy(a):
    snap = {
        "ok": True, "close": 100.0, "atr14": 2.0, "rsi14": 60.0,
        "above_50dma": True, "above_200dma": True, "ema_stack_up": True,
        "macd_bull": True, "trending": True, "di_bull": True,
        "above_vwap": True, "adx14": 30.0, "rvol": 3.0, "bb_lower": 90.0,
    }

    class R:
        regime = "bearish"

    res = eng.evaluate(snap, {"fundamental_score": 90.0}, R(), a)
    s5 = next(s for s in res.signals if s.strategy_id == "S5")   # bullish-only
    assert s5.bias == "no_trade"
    assert any("regime" in g for g in s5.gate_failures)


def test_unknown_regime_gates_everything_off(a):
    snap = {
        "ok": True, "close": 100.0, "atr14": 2.0, "rsi14": 60.0,
        "above_50dma": True, "above_200dma": True, "ema_stack_up": True,
        "macd_bull": True, "trending": True, "di_bull": True,
        "above_vwap": True, "adx14": 30.0, "rvol": 5.0, "bb_lower": 90.0,
    }

    class R:
        regime = "unknown"

    res = eng.evaluate(snap, {"fundamental_score": 99.0}, R(), a)
    assert not res.actionable


def test_missing_fundamentals_fuse_neutral_not_zero(a):
    snap = {"ok": False, "reason": "no data"}

    class R:
        regime = "bullish"

    res = eng.evaluate(snap, {"fundamental_score": None}, R(), a)
    assert res.signals == []
    # The note must echo the *actual* reason the snapshot failed, not a
    # generic message, so a reader can tell a data gap from a real no-setup.
    assert any("no data" in n for n in res.notes)


def test_absent_fundamental_score_fuses_at_fifty(a):
    """Missing fundamentals must read as neutral, never as bad."""
    snap = {
        "ok": True, "close": 100.0, "atr14": 2.0, "rsi14": 60.0,
        "above_50dma": True, "above_200dma": True, "ema_stack_up": True,
        "macd_bull": True, "trending": True, "di_bull": True,
        "above_vwap": True, "adx14": 30.0, "rvol": 3.0, "bb_lower": 90.0,
    }

    class R:
        regime = "bullish"

    res = eng.evaluate(snap, {"fundamental_score": None}, R(), a)
    assert all(s.fundamental_score == 50.0 for s in res.signals)
    assert any("neutral 50" in n for n in res.notes)


def test_entry_stop_target_respect_reward_risk(a):
    snap = {
        "ok": True, "close": 100.0, "atr14": 2.0, "rsi14": 60.0,
        "above_50dma": True, "above_200dma": True, "ema_stack_up": True,
        "macd_bull": True, "trending": True, "di_bull": True,
        "above_vwap": True, "adx14": 30.0, "rvol": 3.0, "bb_lower": 90.0,
    }

    class R:
        regime = "bullish"

    res = eng.evaluate(snap, {"fundamental_score": 80.0}, R(), a)
    for s in res.signals:
        if s.entry and s.stop and s.target:
            assert s.stop < s.entry < s.target
            achieved = (s.target - s.entry) / (s.entry - s.stop)
            assert achieved == pytest.approx(s.reward_risk, rel=0.02)


def test_intraday_strategies_are_labelled(a):
    snap = {
        "ok": True, "close": 100.0, "atr14": 2.0, "rsi14": 60.0,
        "above_50dma": True, "above_200dma": True, "ema_stack_up": True,
        "macd_bull": True, "trending": True, "di_bull": True,
        "above_vwap": True, "adx14": 30.0, "rvol": 3.0, "bb_lower": 90.0,
    }

    class R:
        regime = "bullish"

    res = eng.evaluate(snap, {"fundamental_score": 80.0}, R(), a)
    for s in res.signals:
        assert s.intraday_approximation == (s.strategy_id in eng.INTRADAY)
        if s.intraday_approximation:
            assert s.notes


# -- risk model ------------------------------------------------------------


def test_position_size_matches_the_risk_formula(a):
    d = size_position(equity=1_000_000, entry=100.0, stop=95.0, a=a)
    assert d.approved
    # floor((1,000,000 x 0.5%) / 5) = floor(5000/5) = 1000
    assert d.shares == 1000
    assert d.risk_amount == pytest.approx(5000.0)


def test_risk_refuses_an_inverted_trade(a):
    d = size_position(equity=1_000_000, entry=95.0, stop=100.0, a=a)
    assert not d.approved
    assert any("must exceed" in r for r in d.reasons)


def test_risk_caps_at_equity_no_leverage(a):
    d = size_position(equity=1000, entry=100.0, stop=99.99, a=a)
    if d.approved:
        assert d.notional <= 1000 + 1e-6


def test_risk_respects_the_open_position_cap(a):
    d = size_position(1_000_000, 100.0, 95.0, a, open_positions=a.max_open_positions)
    assert not d.approved
    assert any("cap" in r for r in d.reasons)


def test_risk_refuses_without_an_entry_or_stop(a):
    assert not size_position(1_000_000, None, 95.0, a).approved
    assert not size_position(1_000_000, 100.0, None, a).approved


# -- backtest --------------------------------------------------------------


def test_backtest_needs_history(a):
    from equity_analyst import backtest

    res = backtest.run(_bars([100.0] * 50), "S5", a)
    assert not res.ok
    assert "insufficient" in res.error


def test_backtest_costs_reduce_returns(a):
    from equity_analyst import backtest

    closes = [100 + 30 * math.sin(i / 18.0) + i * 0.12 for i in range(700)]
    bars = _bars(closes)
    cheap = backtest.run(bars, "S5", a, round_trip_bps=0.0)
    dear = backtest.run(bars, "S5", a, round_trip_bps=200.0)
    if cheap.ok and dear.ok and cheap.metrics["trades"]:
        assert dear.metrics["total_return_pct"] < cheap.metrics["total_return_pct"]


def test_backtest_trades_are_internally_consistent(a):
    from equity_analyst import backtest

    closes = [100 + 30 * math.sin(i / 18.0) + i * 0.12 for i in range(700)]
    res = backtest.run(_bars(closes), "S5", a)
    assert res.ok
    for t in res.trades:
        assert t.entry_date <= t.exit_date
        assert t.bars_held >= 0
        assert t.reason in {
            "stop", "target", "trend_break", "revert", "time", "open_end"
        }


def test_backtest_reports_buy_hold_for_comparison(a):
    from equity_analyst import backtest

    closes = [100 + i * 0.1 for i in range(600)]
    res = backtest.run(_bars(closes), "S5", a)
    assert res.ok
    m = res.metrics
    assert m["buy_hold_pct"] > 0
    assert m["excess_vs_buy_hold_pct"] == pytest.approx(
        m["total_return_pct"] - m["buy_hold_pct"], abs=0.15
    )


def test_run_all_covers_both_archetypes(a):
    from equity_analyst import backtest

    closes = [100 + 30 * math.sin(i / 18.0) + i * 0.12 for i in range(700)]
    results = backtest.run_all(_bars(closes), a)
    assert {r.archetype for r in results} == {"trend_momentum", "mean_reversion"}
    for r in results:
        assert any("archetype" in n for n in r.notes)


def test_equity_curve_is_downsampled_for_charting(a):
    from equity_analyst import backtest

    closes = [100 + 30 * math.sin(i / 18.0) + i * 0.12 for i in range(1500)]
    res = backtest.run(_bars(closes), "S5", a)
    assert res.ok
    assert len(res.equity_curve) <= 200
    assert res.equity_curve[-1]["date"] == res.metrics["to"]


# -- paper book ------------------------------------------------------------


def test_paper_book_round_trip():
    from equity_analyst.db import Database
    from equity_analyst.trading import PaperBook, portfolio_snapshot

    with Database(":memory:") as db:
        book = PaperBook(db, starting_cash=100_000.0)
        book.place("ACME", "buy", 100, 50.0, strategy="S5")
        assert book.cash == pytest.approx(95_000.0)

        snap = portfolio_snapshot(book, {"ACME": 60.0})
        assert snap["equity"] == pytest.approx(101_000.0)
        assert snap["unrealized_pnl"] == pytest.approx(1_000.0)

        book.place("ACME", "sell", 100, 60.0)
        assert book.cash == pytest.approx(101_000.0)
        assert not book.positions()


def test_paper_book_refuses_to_overspend():
    from equity_analyst.db import Database
    from equity_analyst.trading import PaperBook

    with Database(":memory:") as db:
        book = PaperBook(db, starting_cash=1_000.0)
        with pytest.raises(ValueError, match="insufficient cash"):
            book.place("ACME", "buy", 100, 50.0)


def test_paper_book_refuses_to_sell_what_it_lacks():
    from equity_analyst.db import Database
    from equity_analyst.trading import PaperBook

    with Database(":memory:") as db:
        book = PaperBook(db, starting_cash=10_000.0)
        with pytest.raises(ValueError, match="no position"):
            book.place("ACME", "sell", 1, 50.0)


def test_paper_book_averages_up_correctly():
    from equity_analyst.db import Database
    from equity_analyst.trading import PaperBook

    with Database(":memory:") as db:
        book = PaperBook(db, starting_cash=100_000.0)
        book.place("ACME", "buy", 100, 10.0)
        book.place("ACME", "buy", 100, 20.0)
        assert book.position("ACME")["avg_price"] == pytest.approx(15.0)


def test_unmarked_positions_are_flagged_not_dropped():
    from equity_analyst.db import Database
    from equity_analyst.trading import PaperBook, portfolio_snapshot

    with Database(":memory:") as db:
        book = PaperBook(db, starting_cash=100_000.0)
        book.place("ACME", "buy", 10, 50.0)
        snap = portfolio_snapshot(book, {})          # no marks supplied
        assert snap["stale_marks"] == ["ACME"]
        assert snap["equity"] == pytest.approx(100_000.0)   # held at cost


# -- end to end ------------------------------------------------------------


def test_signals_run_persists_to_sql(report_signals):
    rows = report_signals.db.dicts(
        "SELECT * FROM signal WHERE run_id = ? ORDER BY fused_score DESC",
        (report_signals.config.run_id,),
    )
    assert len(rows) == 9
    assert rows[0]["fused_score"] >= rows[-1]["fused_score"]


def test_backtest_run_persists_to_sql(report_signals):
    rows = report_signals.db.dicts(
        "SELECT * FROM backtest_result WHERE run_id = ?",
        (report_signals.config.run_id,),
    )
    assert rows
    assert all(r["round_trip_bps"] is not None for r in rows)


def test_regime_persists_to_sql(report_signals):
    rows = report_signals.db.dicts(
        "SELECT * FROM market_regime WHERE run_id = ?",
        (report_signals.config.run_id,),
    )
    assert rows and rows[0]["regime"]


def test_report_renders_signal_and_backtest_sections(report_signals):
    from equity_analyst.report import render

    html = render(report_signals)
    assert 'id="signals"' in html
    assert 'id="backtest"' in html
    assert "Market regime" in html
    # The honest framing must survive into the report.
    assert "not evidence that trading it pays" in html or "beat buy-and-hold" in html
