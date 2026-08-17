"""Analysis layer (§3-§5).

Every module here reads from the SQL views defined in ``sql/schema.sql`` and
records the exact query it ran in a :class:`QueryLog`, so the report appendix
can print the SQL back. Reproducibility is the product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class QueryLog:
    """Captures the SQL a run actually executed, for the appendix."""

    entries: List[Dict[str, str]] = field(default_factory=list)

    def record(self, name: str, sql: str, purpose: str = "") -> None:
        self.entries.append(
            {"name": name, "sql": sql.strip(), "purpose": purpose}
        )

    def run(
        self,
        db: Any,
        name: str,
        sql: str,
        params: Sequence[Any] = (),
        purpose: str = "",
    ) -> List[Dict[str, Any]]:
        self.record(name, sql, purpose)
        return db.dicts(sql, params)


def cagr(begin: Optional[float], end: Optional[float], years: int) -> Optional[float]:
    """Compound annual growth rate as a percentage.

    Returns ``None`` when the maths is undefined rather than a misleading
    number: a negative or zero starting base makes a growth *rate* meaningless
    (you cannot compound out of a loss), and reporting one would be fabrication.
    """
    if begin is None or end is None or years <= 0:
        return None
    if begin <= 0:
        return None
    if end <= 0:
        return -100.0
    return ((end / begin) ** (1.0 / years) - 1.0) * 100.0


def safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return a / b


def mean(values: Sequence[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def median(values: Sequence[Optional[float]]) -> Optional[float]:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def stdev(values: Sequence[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    mu = sum(clean) / len(clean)
    var = sum((v - mu) ** 2 for v in clean) / (len(clean) - 1)
    return var ** 0.5


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson correlation, used for the §5 commodity linkage."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x, y in pairs)
    syy = sum((y - my) ** 2 for x, y in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / ((sxx * syy) ** 0.5)


def linear_slope(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """OLS slope of y on x -- the sensitivity coefficient in §5."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((x - mx) ** 2 for x, y in pairs)
    if sxx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in pairs) / sxx
