"""Paper trading and the risk model.

Ported from `indian-stock-signal-ai`. Paper-only by design: nothing here can
reach a broker. The risk manager has the final say — a setup that clears every
score and gate is still refused if sizing it would breach the risk model.
"""

from .paper import PaperBook, portfolio_snapshot
from .risk import RiskDecision, size_position

__all__ = ["PaperBook", "portfolio_snapshot", "RiskDecision", "size_position"]
