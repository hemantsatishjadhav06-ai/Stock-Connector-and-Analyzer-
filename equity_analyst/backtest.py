"""Cost-aware backtester over daily bars.

Ported from `indian-stock-signal-ai` and reimplemented on the stdlib.

Execution model: entries fill at the signal bar's close; stops and targets are
checked against later bars' high/low and fill **gap-aware** — if a bar opens
through the stop, the fill is the open, not the stop, because that is what would
actually have happened. Costs are charged as one round trip in basis points,
which is how Indian frictions (brokerage + STT + exchange + GST + stamp +
slippage) are usually summarised.

Two honesty constraints, both enforced in code rather than in a footnote:

* **No look-ahead.** A bar's own close is used for the entry decision and the
  fill, and nothing after it is consulted.
* **Intraday strategies are labelled.** S1–S4 need intraday bars; run on daily
  bars they are an approximation, and every result says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .analysis import mean, stdev
from .analysis.technicals import (
    atr,
    bollinger,
    ema_series,
    macd,
    rsi_series,
    sma_series,
)
from .strategies.engine import ARCHETYPE, INTRADAY

TRADING_DAYS = 252


@dataclass
class Trade:
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    return_pct: float
    bars_held: int
    reason: str            # stop | target | trend_break | revert | time | open_end


@dataclass
class BacktestResult:
    strategy_id: str
    archetype: str = ""
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    intraday_approximation: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


def _indicators(rows: List[Dict[str, Any]], a: Any) -> List[Dict[str, Any]]:
    """Attach the indicator set to every bar, dropping the warm-up period."""
    closes = [r["close"] for r in rows]
    highs = [r.get("high") or r["close"] for r in rows]
    lows = [r.get("low") or r["close"] for r in rows]

    sma50, sma200 = sma_series(closes, 50), sma_series(closes, 200)
    ema20, ema50 = ema_series(closes, 20), ema_series(closes, 50)
    rsis = rsi_series(closes, a.rsi_period)

    # ATR and Bollinger are computed on expanding prefixes so each bar sees only
    # the data available at that bar -- the whole point of avoiding look-ahead.
    out: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        if i < 200 or sma200[i] is None or rsis[i] is None:
            continue
        window_hi, window_lo, window_cl = highs[: i + 1], lows[: i + 1], closes[: i + 1]
        atr_v = atr(window_hi, window_lo, window_cl, a.atr_period)
        if not atr_v:
            continue
        _, _, bb_low = bollinger(window_cl, a.bollinger_period, a.bollinger_sigma)
        macd_line, macd_sig, _ = macd(window_cl)
        out.append({
            "date": row["date"],
            "open": row.get("open") or row["close"],
            "high": highs[i], "low": lows[i], "close": closes[i],
            "sma50": sma50[i], "sma200": sma200[i],
            "ema20": ema20[i], "ema50": ema50[i],
            "rsi": rsis[i], "atr": atr_v, "bb_lower": bb_low,
            "macd": macd_line, "macd_signal": macd_sig,
        })
    return out


def _entry_signal(bar: Dict[str, Any], archetype: str) -> bool:
    if archetype == "mean_reversion":
        return bool(
            bar["sma200"] and bar["close"] > bar["sma200"]
            and bar["rsi"] < 35
            and bar["bb_lower"] and bar["close"] <= bar["bb_lower"] * 1.02
        )
    return bool(
        bar["sma50"] and bar["sma200"] and bar["ema20"] and bar["ema50"]
        and bar["close"] > bar["sma50"] > bar["sma200"]
        and bar["ema20"] > bar["ema50"]
        and 50 <= bar["rsi"] <= 72
        and bar["macd"] is not None and bar["macd_signal"] is not None
        and bar["macd"] > bar["macd_signal"]
    )


def run(
    rows: List[Dict[str, Any]],
    strategy_id: str,
    a: Any,
    round_trip_bps: Optional[float] = None,
    start_cash: float = 100_000.0,
) -> BacktestResult:
    archetype = ARCHETYPE.get(strategy_id, "trend_momentum")
    res = BacktestResult(
        strategy_id=strategy_id,
        archetype=archetype,
        intraday_approximation=strategy_id in INTRADAY,
    )
    if res.intraday_approximation:
        res.notes.append(
            "This is an intraday strategy approximated on daily bars. The result "
            "describes a daily-bar proxy, not the strategy as specified."
        )

    bars = _indicators(rows, a)
    if len(bars) < 60:
        res.error = (
            f"insufficient history after indicator warm-up "
            f"({len(bars)} usable bars, need 60+)"
        )
        return res

    cost = (round_trip_bps if round_trip_bps is not None else a.round_trip_cost_bps) / 10_000.0
    reward_risk = 1.5 if archetype == "mean_reversion" else 2.0
    stop_mult = 1.5 if archetype == "mean_reversion" else 2.0
    max_hold = 15 if archetype == "mean_reversion" else 40

    equity = start_cash
    first_close = bars[0]["close"]
    in_position = False
    entry = stop = target = 0.0
    entry_index = 0
    entry_date = ""
    exposed_bars = 0
    curve: List[Dict[str, Any]] = []

    for i, bar in enumerate(bars):
        if in_position:
            exit_price: Optional[float] = None
            reason = ""
            # Gap-aware: a bar opening through the level fills at the open.
            if bar["low"] <= stop:
                exit_price, reason = min(bar["open"], stop), "stop"
            elif bar["high"] >= target:
                exit_price, reason = max(bar["open"], target), "target"
            elif archetype == "trend_momentum" and bar["sma50"] and bar["close"] < bar["sma50"]:
                exit_price, reason = bar["close"], "trend_break"
            elif archetype == "mean_reversion" and bar["rsi"] > 55:
                exit_price, reason = bar["close"], "revert"
            elif (i - entry_index) >= max_hold:
                exit_price, reason = bar["close"], "time"

            if exit_price is not None:
                net = (exit_price / entry - 1.0) - cost
                equity *= 1.0 + net
                res.trades.append(Trade(
                    entry_date=entry_date, exit_date=bar["date"],
                    entry=round(entry, 2), exit=round(exit_price, 2),
                    return_pct=round(net * 100, 2),
                    bars_held=i - entry_index, reason=reason,
                ))
                in_position = False

        elif _entry_signal(bar, archetype) and bar["atr"] > 0 and i < len(bars) - 1:
            entry, entry_index, entry_date = bar["close"], i, bar["date"]
            stop = entry - stop_mult * bar["atr"]
            target = entry + reward_risk * (entry - stop)
            in_position = True

        if in_position:
            exposed_bars += 1
            marked = equity * (bar["close"] / entry)
        else:
            marked = equity
        curve.append({
            "date": bar["date"],
            "strategy": marked,
            "buy_hold": start_cash * bar["close"] / first_close,
        })

    if in_position:
        last = bars[-1]
        net = (last["close"] / entry - 1.0) - cost
        equity *= 1.0 + net
        res.trades.append(Trade(
            entry_date=entry_date, exit_date=last["date"],
            entry=round(entry, 2), exit=round(last["close"], 2),
            return_pct=round(net * 100, 2),
            bars_held=len(bars) - 1 - entry_index, reason="open_end",
        ))
        curve[-1]["strategy"] = equity
        res.notes.append(
            "The final trade was still open at the end of the window and is marked "
            "to the last close, so its result is provisional."
        )

    res.metrics = _metrics(res, bars, curve, equity, start_cash, exposed_bars, cost)
    res.equity_curve = _downsample(curve)
    return res


def _metrics(
    res: BacktestResult,
    bars: List[Dict[str, Any]],
    curve: List[Dict[str, Any]],
    equity: float,
    start_cash: float,
    exposed_bars: int,
    cost: float,
) -> Dict[str, Any]:
    returns = [t.return_pct for t in res.trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    values = [p["strategy"] for p in curve]
    peak, max_dd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        if peak:
            max_dd = min(max_dd, v / peak - 1.0)

    daily = [
        values[i] / values[i - 1] - 1.0
        for i in range(1, len(values))
        if values[i - 1]
    ]
    sd = stdev(daily)
    sharpe = (
        (mean(daily) / sd) * math.sqrt(TRADING_DAYS) if sd and mean(daily) is not None else None
    )

    years = len(bars) / TRADING_DAYS
    total = equity / start_cash - 1.0
    cagr = ((equity / start_cash) ** (1.0 / years) - 1.0) if years > 0 and equity > 0 else None
    buy_hold = bars[-1]["close"] / bars[0]["close"] - 1.0

    return {
        "strategy_id": res.strategy_id,
        "archetype": res.archetype,
        "bars": len(bars),
        "from": bars[0]["date"],
        "to": bars[-1]["date"],
        "trades": len(res.trades),
        "win_rate_pct": round(len(wins) / len(returns) * 100, 1) if returns else 0.0,
        "avg_win_pct": round(mean(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(mean(losses), 2) if losses else 0.0,
        "expectancy_pct": round(mean(returns), 2) if returns else 0.0,
        "profit_factor": (
            round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None
        ),
        "total_return_pct": round(total * 100, 1),
        "cagr_pct": round(cagr * 100, 1) if cagr is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 1),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "exposure_pct": round(exposed_bars / len(bars) * 100, 1),
        "buy_hold_pct": round(buy_hold * 100, 1),
        "excess_vs_buy_hold_pct": round((total - buy_hold) * 100, 1),
        "ending_equity": round(equity, 0),
        "start_cash": start_cash,
        "round_trip_bps": round(cost * 10_000, 1),
        "intraday_approximation": res.intraday_approximation,
    }


def _downsample(curve: List[Dict[str, Any]], target: int = 180) -> List[Dict[str, Any]]:
    if not curve:
        return []
    step = max(1, len(curve) // target)
    out = [curve[i] for i in range(0, len(curve), step)]
    if out[-1]["date"] != curve[-1]["date"]:
        out.append(curve[-1])
    return out


def run_all(
    rows: List[Dict[str, Any]], a: Any, start_cash: float = 100_000.0
) -> List[BacktestResult]:
    """Backtest every strategy archetype once.

    The library's nine strategies collapse to two distinct daily-bar rule sets,
    so running all nine would print the same two results four and five times
    over. One result per archetype is reported instead, labelled with the
    strategies it stands for.
    """
    out = []
    for representative in ("S5", "S9"):          # trend_momentum, mean_reversion
        result = run(rows, representative, a, start_cash=start_cash)
        archetype = ARCHETYPE[representative]
        covered = [sid for sid, arc in ARCHETYPE.items() if arc == archetype]
        result.notes.append(
            f"Daily-bar rules for the '{archetype}' archetype, which covers "
            f"{', '.join(covered)}."
        )
        out.append(result)
    return out
