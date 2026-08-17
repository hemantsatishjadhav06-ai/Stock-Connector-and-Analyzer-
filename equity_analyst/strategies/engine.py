"""Score fusion and gating: indicators + fundamentals + regime -> per-strategy signals.

Every number reaching this module was computed deterministically upstream. The
engine only weights, gates and formats — it never derives a price.

The rule from the library's own `score_fusion` block:

    Trade only when (a) the fused score clears the threshold AND (b) all of the
    strategy's required gates pass AND (c) the risk model approves. Any single
    hard-gate failure is a no-trade regardless of how high the score is.

A high score with a failed gate is therefore reported as `no_trade` **with the
failure named**, not silently dropped — the reason a setup was rejected is as
useful to a subscriber as the setups that passed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STRATEGIES_PATH = os.path.join(os.path.dirname(__file__), "strategies.json")

#: Which technical archetype scores each strategy.
ARCHETYPE = {
    "S1": "trend_momentum", "S2": "trend_momentum", "S4": "trend_momentum",
    "S5": "trend_momentum", "S7": "trend_momentum", "S8": "trend_momentum",
    "S3": "mean_reversion", "S6": "mean_reversion", "S9": "mean_reversion",
}

#: Strategies that genuinely need intraday bars. Scored on daily bars here and
#: labelled so nobody mistakes the approximation for the real thing.
INTRADAY = {"S1", "S2", "S3", "S4"}


def load_strategies(path: str = STRATEGIES_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


STRATEGIES = load_strategies()


def strategy_by_id(sid: str) -> Optional[Dict[str, Any]]:
    for s in STRATEGIES.get("strategies", []):
        if s["id"] == sid:
            return s
    return None


@dataclass
class Signal:
    strategy_id: str
    strategy: str
    category: str
    bias: str                       # long | no_trade
    fused_score: float
    technical_score: float
    fundamental_score: float
    regime_score: float
    regime_fit: bool
    archetype: str
    technical_reasons: List[str] = field(default_factory=list)
    gate_failures: List[str] = field(default_factory=list)
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    reward_risk: Optional[float] = None
    intraday_approximation: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.bias == "long"


@dataclass
class SignalResult:
    signals: List[Signal] = field(default_factory=list)
    regime: Any = None
    min_score: float = 65.0
    fundamental_block: Dict[str, Any] = field(default_factory=dict)
    snapshot: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def actionable(self) -> List[Signal]:
        return [s for s in self.signals if s.actionable]

    @property
    def best(self) -> Optional[Signal]:
        return self.signals[0] if self.signals else None


def evaluate(
    snapshot: Dict[str, Any],
    fundamental: Dict[str, Any],
    regime: Any,
    a: Any,
    news_sentiment: Optional[float] = None,
) -> SignalResult:
    """Score every strategy in the library against the current setup."""
    min_score = float(
        STRATEGIES.get("score_fusion", {}).get("min_total_score_to_trade", a.min_fused_score)
    )
    res = SignalResult(
        regime=regime, min_score=min_score,
        fundamental_block=fundamental, snapshot=snapshot,
    )

    if not snapshot.get("ok"):
        res.notes.append(
            f"No signals generated: {snapshot.get('reason', 'no price history')}."
        )
        return res

    regime_name = getattr(regime, "regime", "unknown")
    fscore = fundamental.get("fundamental_score")
    if fscore is None:
        # Neutral, not zero: absent fundamentals must not read as bad ones.
        fscore_eff = 50.0
        res.notes.append(
            "Fundamental score unavailable; fused at a neutral 50 so its absence "
            "neither rewards nor penalises a setup."
        )
    else:
        fscore_eff = float(fscore)
        if not fundamental.get("data_complete"):
            res.notes.append(
                "Fundamental score rests on fewer than three sub-scores — treat the "
                "fundamental leg of every fused score below as thin."
            )

    if news_sentiment is None:
        sentiment = 50.0
        res.notes.append(
            "No quantified news-sentiment feed is wired in; that leg is fused at a "
            "neutral 50 rather than being invented."
        )
    else:
        sentiment = float(news_sentiment)

    from ..analysis.regime import technical_score

    for spec in STRATEGIES.get("strategies", []):
        sid = spec["id"]
        archetype = ARCHETYPE.get(sid, "trend_momentum")
        tscore, treasons = technical_score(snapshot, archetype)

        best_regimes = spec.get("best_regimes", [])
        regime_fit = regime_name in best_regimes
        regime_score = 100.0 if regime_fit else 40.0

        weights = spec.get("score_weights", {})
        fused = (
            weights.get("technical", 0.0) * tscore
            + weights.get("fundamental", 0.0) * fscore_eff
            + weights.get("regime", 0.0) * regime_score
            + weights.get("news_sentiment", 0.0) * sentiment
        )

        failures = _gate_failures(sid, spec, snapshot, regime_name, best_regimes, a)
        actionable = fused >= min_score and not failures

        entry = snapshot.get("close")
        atr14 = snapshot.get("atr14")
        stop = target = rr = None
        if entry and atr14:
            stop_mult = 1.0 if archetype == "mean_reversion" else 1.5
            stop = round(entry - stop_mult * atr14, 2)
            rr = float(
                (spec.get("technical_block") or {}).get("min_reward_to_risk", 1.5)
            )
            target = round(entry + rr * (entry - stop), 2)

        signal = Signal(
            strategy_id=sid,
            strategy=spec["name"],
            category=spec.get("category", ""),
            bias="long" if actionable else "no_trade",
            fused_score=round(fused, 1),
            technical_score=round(tscore, 1),
            fundamental_score=round(fscore_eff, 1),
            regime_score=regime_score,
            regime_fit=regime_fit,
            archetype=archetype,
            technical_reasons=treasons,
            gate_failures=failures,
            entry=entry, stop=stop, target=target, reward_risk=rr,
            intraday_approximation=sid in INTRADAY,
        )
        if signal.intraday_approximation:
            signal.notes.append(
                f"{spec.get('signal_timeframe', 'intraday')} strategy scored on daily "
                f"bars — treat as an approximation, not an executable intraday setup."
            )
        res.signals.append(signal)

    res.signals.sort(key=lambda s: s.fused_score, reverse=True)
    return res


def _gate_failures(
    sid: str,
    spec: Dict[str, Any],
    snap: Dict[str, Any],
    regime: str,
    best_regimes: List[str],
    a: Any,
) -> List[str]:
    """Hard gates. Any one failing means no trade, whatever the score says."""
    fails: List[str] = []

    if regime == "unknown":
        fails.append("market regime could not be determined")
    elif best_regimes and regime not in best_regimes:
        fails.append(f"regime '{regime}' not in {best_regimes}")
    if regime in (spec.get("avoid_regimes") or []):
        fails.append(f"regime '{regime}' is on this strategy's avoid list")

    archetype = ARCHETYPE.get(sid, "trend_momentum")
    if archetype == "trend_momentum" and not snap.get("above_50dma"):
        fails.append("price below the 50DMA")
    if archetype == "mean_reversion":
        rsi_v = snap.get("rsi14")
        if rsi_v is None or rsi_v > 50:
            fails.append("not pulled back (RSI > 50)")

    # Strategy-specific gates drawn from each library entry's required_gates.
    if sid == "S6" and not snap.get("above_200dma"):
        fails.append("200DMA broken — no value-in-uptrend setup")
    if sid == "S9":
        adx = snap.get("adx14")
        if adx is None or adx >= a.adx_trend_floor:
            fails.append("ADX not in range conditions")
    if sid in ("S1", "S2") and not snap.get("above_vwap"):
        fails.append("price below VWAP")
    if sid == "S1":
        rvol = snap.get("rvol")
        if rvol is None or rvol < 2.0:
            fails.append(
                f"RVOL {rvol:.1f} below the 2.0 breakout gate" if rvol is not None
                else "RVOL unavailable"
            )

    return fails
