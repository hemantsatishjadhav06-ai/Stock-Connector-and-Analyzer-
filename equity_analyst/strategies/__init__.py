"""§S Strategy library and signal generation.

Ported from `indian-stock-signal-ai`: a JSON-defined library of nine strategies,
each with a fundamental block and a technical block kept deliberately separate,
regime fit, score-fusion weights, hard gates, and entry/stop/target logic.

The library file is data, not code — edit ``strategies.json`` to retune weights
or add a strategy without touching the engine.
"""

from .engine import (
    Signal,
    SignalResult,
    STRATEGIES,
    evaluate,
    load_strategies,
    strategy_by_id,
)

__all__ = [
    "Signal",
    "SignalResult",
    "STRATEGIES",
    "evaluate",
    "load_strategies",
    "strategy_by_id",
]
