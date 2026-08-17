"""Market-regime detection and the 0-100 scoring layer.

Ported from `indian-stock-signal-ai` and reimplemented on the stdlib so the
engine keeps its zero-dependency core. Two jobs:

* **Regime** — classify the benchmark as bullish / bearish / range / volatile.
  Strategies are gated to the regimes they actually fit, so a mean-reversion
  setup cannot fire in a runaway trend.
* **Scores** — transparent 0-100 technical and fundamental scores that the
  strategy engine fuses. Every point is attributable to a named reason; the
  scores are heuristics to be tuned in backtesting, and they say so.

The scoring is deliberately separate from §3/§6 valuation. Those answer *what
is this business worth*; these answer *is this a tradeable setup right now*.
Keeping the two lenses apart is the design, not an accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import QueryLog, mean
from .technicals import (
    adx_series,
    atr,
    bollinger,
    ema_series,
    macd,
    relative_volume,
    rsi,
    sma_series,
    vwap_series,
)


@dataclass
class Regime:
    regime: str = "unknown"          # bullish | bearish | range | volatile | unknown
    confidence: float = 0.0
    drivers: List[str] = field(default_factory=list)
    atr_pct: Optional[float] = None
    benchmark: Optional[str] = None
    as_of: Optional[str] = None


def snapshot(rows: List[Dict[str, Any]], a: Any) -> Dict[str, Any]:
    """Latest indicator values plus the derived booleans strategies gate on."""
    if not rows or len(rows) < 30:
        return {"ok": False, "reason": f"insufficient history ({len(rows or [])} bars)"}

    closes = [r["close"] for r in rows]
    highs = [r.get("high") or r["close"] for r in rows]
    lows = [r.get("low") or r["close"] for r in rows]
    volumes = [r.get("volume") or 0.0 for r in rows]

    macd_line, macd_sig, macd_hist = macd(closes)
    adx_s, plus_s, minus_s = adx_series(highs, lows, closes, a.adx_period)
    bb_up, bb_mid, bb_low = bollinger(closes, a.bollinger_period, a.bollinger_sigma)

    snap: Dict[str, Any] = {
        "ok": True,
        "close": closes[-1],
        "ema20": ema_series(closes, 20)[-1],
        "ema50": ema_series(closes, 50)[-1],
        "sma50": sma_series(closes, 50)[-1],
        "sma200": sma_series(closes, 200)[-1],
        "rsi14": rsi(closes, a.rsi_period),
        "macd": macd_line,
        "macd_signal": macd_sig,
        "macd_hist": macd_hist,
        "atr14": atr(highs, lows, closes, a.atr_period),
        "adx14": adx_s[-1],
        "plus_di": plus_s[-1],
        "minus_di": minus_s[-1],
        "bb_upper": bb_up,
        "bb_mid": bb_mid,
        "bb_lower": bb_low,
        "vwap": vwap_series(highs, lows, closes, volumes)[-1],
        "rvol": relative_volume(volumes),
    }

    c = snap["close"]
    snap["above_50dma"] = bool(c and snap["sma50"] and c > snap["sma50"])
    snap["above_200dma"] = bool(c and snap["sma200"] and c > snap["sma200"])
    snap["ema_stack_up"] = bool(
        snap["ema20"] and snap["ema50"] and snap["ema20"] > snap["ema50"]
    )
    snap["macd_bull"] = bool(
        snap["macd"] is not None and snap["macd_signal"] is not None
        and snap["macd"] > snap["macd_signal"]
    )
    snap["trending"] = bool(
        snap["adx14"] is not None and snap["adx14"] > a.adx_trend_floor
    )
    snap["di_bull"] = bool(
        snap["plus_di"] is not None and snap["minus_di"] is not None
        and snap["plus_di"] > snap["minus_di"]
    )
    snap["above_vwap"] = bool(c and snap["vwap"] and c > snap["vwap"])
    return snap


def detect_regime(rows: List[Dict[str, Any]], a: Any, benchmark: str = "") -> Regime:
    """Classify the market from the benchmark's own price action."""
    snap = snapshot(rows, a)
    if not snap.get("ok"):
        return Regime(
            regime="unknown", confidence=0.0,
            drivers=[f"benchmark unavailable: {snap.get('reason', 'no data')}"],
            benchmark=benchmark,
        )

    close, sma50, sma200 = snap["close"], snap["sma50"], snap["sma200"]
    atr14 = snap["atr14"]
    drivers: List[str] = []
    atr_pct = (atr14 / close * 100.0) if (atr14 and close) else None

    closes = [r["close"] for r in rows]
    ret5 = (
        (closes[-1] - closes[-6]) / closes[-6] * 100.0
        if len(closes) > 6 and closes[-6] else 0.0
    )

    if sma50 and sma200 and close > sma50 > sma200:
        regime, confidence = "bullish", 70.0
        drivers.append("price > 50DMA > 200DMA")
    elif sma50 and sma200 and close < sma50 < sma200:
        regime, confidence = "bearish", 70.0
        drivers.append("price < 50DMA < 200DMA")
    else:
        regime, confidence = "range", 55.0
        drivers.append("moving averages intertwined")

    # Volatility overrides direction: a 6% weekly swing is its own regime, and
    # position sizing should respond to it before trend-following does.
    if (atr_pct is not None and atr_pct > 3.5) or abs(ret5) > 6.0:
        regime, confidence = "volatile", 60.0
        drivers.append(
            f"elevated volatility (ATR {atr_pct:.1f}%/day, 5-day move {ret5:+.1f}%)"
            if atr_pct is not None else f"5-day move {ret5:+.1f}%"
        )

    return Regime(
        regime=regime,
        confidence=confidence,
        drivers=drivers,
        atr_pct=round(atr_pct, 2) if atr_pct is not None else None,
        benchmark=benchmark,
        as_of=rows[-1].get("date"),
    )


# -- 0-100 scores ----------------------------------------------------------


def technical_score(snap: Dict[str, Any], archetype: str = "trend_momentum"):
    """Transparent 0-100 technical score with the reasons that earned it."""
    if not snap.get("ok"):
        return 0.0, ["insufficient data"]

    points, reasons = 0.0, []
    rsi_v = snap.get("rsi14")

    if archetype == "mean_reversion":
        points = 20.0
        if snap.get("above_200dma"):
            points += 15; reasons.append("long-term uptrend intact")
        if rsi_v is not None and rsi_v < 35:
            points += 25; reasons.append(f"oversold (RSI {rsi_v:.0f})")
        elif rsi_v is not None and rsi_v < 45:
            points += 12; reasons.append(f"pulling back (RSI {rsi_v:.0f})")
        if snap.get("close") and snap.get("bb_lower") and snap["close"] <= snap["bb_lower"] * 1.01:
            points += 20; reasons.append("tagging the lower Bollinger band")
        if snap.get("adx14") is not None and snap["adx14"] < 20:
            points += 10; reasons.append("range conditions (ADX < 20)")
    else:  # trend_momentum -- also used by growth and rotation strategies
        points = 15.0
        if snap.get("above_200dma"):
            points += 15; reasons.append("above the 200DMA")
        if snap.get("above_50dma"):
            points += 10; reasons.append("above the 50DMA")
        if snap.get("ema_stack_up"):
            points += 10; reasons.append("EMA20 > EMA50")
        if rsi_v is not None and 50 <= rsi_v <= 70:
            points += 15; reasons.append(f"healthy momentum (RSI {rsi_v:.0f})")
        elif rsi_v is not None and rsi_v > 70:
            points += 5; reasons.append(f"strong but extended (RSI {rsi_v:.0f})")
        if snap.get("macd_bull"):
            points += 10; reasons.append("MACD above signal")
        if snap.get("trending") and snap.get("di_bull"):
            points += 15; reasons.append("trending up (ADX > 20, +DI > -DI)")
        if snap.get("above_vwap"):
            points += 10; reasons.append("above VWAP")

    return max(0.0, min(100.0, points)), reasons


def _band(value: Optional[float], lo: float, hi: float) -> Optional[float]:
    """Map value in [lo, hi] onto 0-100, clamped."""
    if value is None:
        return None
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))


def fundamental_score(fundamentals: Any, a: Any) -> Dict[str, Any]:
    """A 0-100 trading-lens quality score built from the SQL layer.

    Distinct from §3's valuation work: this compresses growth, profitability,
    leverage and cash generation into one number the strategy engine can fuse.
    Missing inputs are omitted from the average rather than defaulted, and
    ``data_complete`` reports whether enough sub-scores existed to trust it.
    """
    sub: Dict[str, float] = {}
    strengths: List[str] = []
    weaknesses: List[str] = []

    annual = getattr(fundamentals, "annual", []) or []
    growth = getattr(fundamentals, "growth", {}) or {}
    quality = getattr(fundamentals, "quality", {}) or {}
    leverage_rows = getattr(fundamentals, "leverage", []) or []

    sales_cagr = growth.get("blended_sales_cagr")
    eps_cagr = growth.get("blended_eps_cagr")
    growth_score = mean(
        [v for v in (_band(sales_cagr, 0.0, 25.0), _band(eps_cagr, 0.0, 25.0)) if v is not None]
    )
    if growth_score is not None:
        sub["growth"] = growth_score
        (strengths if growth_score >= 60 else weaknesses).append(
            f"growth {'strong' if growth_score >= 60 else 'soft'}"
            + (f" (sales CAGR {sales_cagr:.0f}%)" if sales_cagr is not None else "")
        )

    metrics = quality.get("metrics") or {}

    def median_of(key: str) -> Optional[float]:
        block = metrics.get(key)
        return block.get("median") if isinstance(block, dict) else None

    roe = median_of("roe")
    margins = getattr(fundamentals, "margins", []) or []
    opm = mean([m.get("opm") for m in margins[-5:]]) if margins else None
    gpm = mean([m.get("gp_margin") for m in margins[-5:]]) if margins else None
    prof = mean(
        [v for v in (_band(roe, 5.0, 25.0), _band(opm, 5.0, 30.0), _band(gpm, 15.0, 60.0))
         if v is not None]
    )
    if prof is not None:
        sub["profitability"] = prof
        (strengths if prof >= 60 else weaknesses).append(
            f"profitability {'high' if prof >= 60 else 'low'}"
            + (f" (ROE {roe:.0f}%)" if roe is not None else "")
        )

    d2e = leverage_rows[-1].get("debt_to_equity") if leverage_rows else None
    if d2e is not None:
        lev = max(0.0, min(100.0, (2.0 - d2e) / 2.0 * 100.0))
        sub["leverage"] = lev
        (strengths if lev >= 60 else weaknesses).append(
            f"leverage {'conservative' if lev >= 60 else 'elevated'} (D/E {d2e:.2f})"
        )

    recent_fcf = [r.get("fcf") for r in annual[-3:] if r.get("fcf") is not None]
    if recent_fcf:
        positive = sum(1 for v in recent_fcf if v > 0)
        cash = positive / len(recent_fcf) * 100.0
        sub["cash_flow"] = cash
        (strengths if cash >= 60 else weaknesses).append(
            f"free cash flow positive in {positive}/{len(recent_fcf)} recent years"
        )

    total = mean(list(sub.values()))
    return {
        "fundamental_score": round(total, 1) if total is not None else None,
        "subscores": {k: round(v, 1) for k, v in sub.items()},
        "strengths": strengths,
        "weaknesses": weaknesses,
        # Below three sub-scores the number is too thin to fuse with confidence.
        "data_complete": total is not None and len(sub) >= 3,
    }
