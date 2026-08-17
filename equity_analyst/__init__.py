"""Autonomous equity research, forensic and valuation engine.

Implements the master analyst brief end to end:

    §1 collect  ->  §2 SQL  ->  §3 fundamentals  ->  §4 forensics
    §5 technicals + news/commodity  ->  §6 four valuation models + MoS
    §7 chart-first HTML report  ->  §8 verification score

Every figure is traceable to a provider call and a retrieval timestamp; missing
data lowers the verification score and is never filled with a guess.
"""

from .config import Assumptions, RunConfig
from .pipeline import Report, run

__version__ = "0.1.0"
__all__ = ["Assumptions", "RunConfig", "Report", "run", "__version__"]
