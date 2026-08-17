"""Position sizing and risk caps.

The risk model is the last gate in the flow: a setup that clears every score and
every strategy gate is still refused here if sizing it would breach a cap. Every
refusal returns a reason rather than a silent zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class RiskDecision:
    approved: bool
    shares: int = 0
    notional: float = 0.0
    risk_amount: float = 0.0
    per_share_risk: float = 0.0
    risk_pct: float = 0.0
    reasons: List[str] = field(default_factory=list)


def size_position(
    equity: float,
    entry: Optional[float],
    stop: Optional[float],
    a: Any,
    open_positions: int = 0,
    risk_pct: Optional[float] = None,
) -> RiskDecision:
    """``shares = floor((equity x risk%) / (entry - stop))``, then capped.

    Two caps beyond the risk budget: no leverage (notional cannot exceed
    available equity) and the library's maximum concurrent positions.
    """
    risk_pct = a.risk_per_trade_pct if risk_pct is None else risk_pct
    decision = RiskDecision(approved=False, risk_pct=risk_pct)

    if not entry or not stop:
        decision.reasons.append("no entry/stop available to size against")
        return decision
    if entry <= stop:
        decision.reasons.append(
            f"entry {entry:.2f} must exceed stop {stop:.2f} for a long — "
            f"refusing to size an inverted trade"
        )
        return decision
    if equity <= 0:
        decision.reasons.append("no equity available")
        return decision
    if open_positions >= a.max_open_positions:
        decision.reasons.append(
            f"already at the {a.max_open_positions}-position cap"
        )
        return decision

    per_share = entry - stop
    risk_amount = equity * risk_pct / 100.0
    shares = math.floor(risk_amount / per_share)

    if shares <= 0:
        decision.reasons.append(
            f"a {risk_pct}% risk budget ({risk_amount:,.2f}) will not buy one share "
            f"at {per_share:,.2f} of risk per share"
        )
        return decision

    notional = shares * entry
    if notional > equity:
        shares = math.floor(equity / entry)      # no leverage
        notional = shares * entry
        decision.reasons.append("size capped at available equity (no leverage)")
    if shares <= 0:
        decision.reasons.append("insufficient equity for a single share")
        return decision

    decision.approved = True
    decision.shares = int(shares)
    decision.notional = round(notional, 2)
    decision.risk_amount = round(risk_amount, 2)
    decision.per_share_risk = round(per_share, 2)
    return decision
