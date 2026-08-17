"""§8 Verification / confidence percentage.

    Verification = 0.30·completeness + 0.25·source + 0.20·model_agreement
                 + 0.15·forensic + 0.10·recency

Printed with its components so a subscriber can trust or discount the call.
The score is a *rigor* measure, not a conviction measure: a high number means
"this was built on complete, fresh, audited data whose models agree", not
"this stock will go up".
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .providers.base import SOURCE_TIERS

WEIGHTS = {
    "completeness": 0.30,
    "source": 0.25,
    "model_agreement": 0.20,
    "forensic": 0.15,
    "recency": 0.10,
}

#: Fields the engine needs for a full-strength report. Missing entries reduce
#: completeness proportionally -- this list *is* the definition of "complete".
REQUIRED_FIELDS = {
    "income": ["sales", "ebitda", "depreciation", "ebit", "pat", "eps"],
    "balance": ["net_worth", "borrowings", "total_assets", "cash"],
    "cashflow": ["cfo", "capex"],
    "price": ["close"],
    "company": ["shares_outstanding", "currency"],
    "market": ["price"],
}


@dataclass
class SubScore:
    key: str
    label: str
    score: float                  # 0-1
    weight: float
    detail: str
    evidence: List[str] = field(default_factory=list)

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass
class VerificationResult:
    total: float = 0.0            # 0-100
    subscores: List[SubScore] = field(default_factory=list)
    formula: str = ""
    caps_applied: List[str] = field(default_factory=list)
    band: str = "low"             # high | moderate | low

    def get(self, key: str) -> Optional[SubScore]:
        for s in self.subscores:
            if s.key == key:
                return s
        return None


def score(
    db: Any,
    config: Any,
    provider_result: Any,
    fundamentals: Any,
    forensic: Any,
    valuation: Any,
    technicals: Any,
) -> VerificationResult:
    res = VerificationResult()
    res.formula = " + ".join(f"{w:.2f}·{k}" for k, w in WEIGHTS.items())

    res.subscores = [
        _completeness(db, config, fundamentals, technicals),
        _source_quality(db, config, provider_result),
        _model_agreement(valuation),
        _forensic(forensic),
        _recency(db, config, provider_result),
    ]

    total = sum(s.contribution for s in res.subscores) * 100.0

    # A failed forensic check caps the headline regardless of everything else:
    # a confident-looking number on a business whose profits are not cash-backed
    # is worse than no number.
    if getattr(forensic, "flag", None) == "fail":
        if total > 60.0:
            res.caps_applied.append(
                "Capped at 60%: a forensic check failed, so the verdict cannot be "
                "presented as high-confidence however complete the data is."
            )
            total = 60.0
    elif getattr(forensic, "flag", None) == "unknown":
        if total > 70.0:
            res.caps_applied.append(
                "Capped at 70%: too few forensic checks could run to certify "
                "accounting integrity."
            )
            total = 70.0

    if getattr(valuation, "selected_value", None) is None and total > 55.0:
        res.caps_applied.append(
            "Capped at 55%: no intrinsic value could be computed, so there is no "
            "valuation call to be confident about."
        )
        total = 55.0

    res.total = round(total, 1)
    res.band = "high" if res.total >= 75 else "moderate" if res.total >= 55 else "low"
    return res


# -- sub-scores ------------------------------------------------------------


def _completeness(db: Any, config: Any, fundamentals: Any, technicals: Any) -> SubScore:
    """How many required fields actually arrived, weighted by how much each
    domain matters, plus a penalty for a short history."""
    ticker = config.ticker
    present, total = 0.0, 0.0
    evidence: List[str] = []

    def tally(ok: bool, label: str, weight: float = 1.0) -> None:
        nonlocal present, total
        total += weight
        if ok:
            present += weight
        else:
            evidence.append(f"missing: {label}")

    checks = (
        ("income_statement", REQUIRED_FIELDS["income"], "income"),
        ("balance_sheet", REQUIRED_FIELDS["balance"], "balance"),
        ("cash_flow", REQUIRED_FIELDS["cashflow"], "cashflow"),
    )
    for table, fields, label in checks:
        for fld in fields:
            count = db.scalar(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE ticker = ? AND period_type = 'annual' AND {fld} IS NOT NULL",
                (ticker,),
            ) or 0
            tally(bool(count), f"{label}.{fld}")

    # Price history is load-bearing far beyond one field: without it there is no
    # technical read, no historic multiple band and no Buffett $1 test. Weighted
    # accordingly so its absence cannot be diluted by a long list of ratios.
    bars = db.scalar("SELECT COUNT(*) FROM price_daily WHERE ticker = ?", (ticker,)) or 0
    tally(bars > 200, f"adequate daily price history (have {bars} sessions)", 4.0)

    for column in ("shares_outstanding", "currency"):
        tally(
            bool(db.scalar(f"SELECT {column} FROM company WHERE ticker = ?", (ticker,))),
            f"company.{column}",
            1.5,
        )

    tally(
        bool(db.scalar(
            "SELECT price FROM market_snapshot WHERE ticker = ? ORDER BY as_of DESC LIMIT 1",
            (ticker,),
        )),
        "current market price",
        2.0,
    )

    field_score = present / total if total else 0.0

    # History depth: ten years is the reference horizon; less is a real gap.
    years = len(getattr(fundamentals, "annual", []) or [])
    depth = min(1.0, years / 10.0)
    if years < 10:
        evidence.append(f"only {years} annual periods available (10 wanted)")

    combined = field_score * 0.75 + depth * 0.25
    return SubScore(
        "completeness",
        "Data completeness",
        combined,
        WEIGHTS["completeness"],
        f"{present:.0f}/{total:.0f} weighted required fields present; {years} annual "
        f"periods ({depth:.0%} of the 10-year reference horizon).",
        evidence[:12],
    )


def _source_quality(db: Any, config: Any, provider_result: Any) -> SubScore:
    """Weight each domain by the tier of the source that supplied it."""
    rows = db.dicts(
        """
        SELECT source_tier, COUNT(*) AS n FROM data_coverage
        WHERE run_id = ? AND present = 1 GROUP BY source_tier
        """,
        (config.run_id,),
    )
    evidence: List[str] = []
    if rows:
        weighted, count = 0.0, 0
        for row in rows:
            tier = row.get("source_tier") or "vendor"
            weight = SOURCE_TIERS.get(tier, 0.5)
            weighted += weight * row["n"]
            count += row["n"]
            evidence.append(f"{row['n']} domain(s) from '{tier}' (weight {weight:.2f})")
        value = weighted / count if count else 0.0
    else:
        tier = getattr(provider_result, "source_tier", "vendor")
        value = SOURCE_TIERS.get(tier, 0.5)
        evidence.append(f"single source tier '{tier}'")

    provider = getattr(provider_result, "provider", "unknown")
    return SubScore(
        "source", "Source quality", value, WEIGHTS["source"],
        f"Data supplied by {provider}.", evidence,
    )


def _model_agreement(valuation: Any) -> SubScore:
    """Tight cluster of intrinsic values -> high; wide dispersion -> low."""
    from .valuation.models import IV_MODELS

    models = getattr(valuation, "models", []) or []
    available = [m for m in models if m.name in IV_MODELS and m.value]
    evidence = [
        f"{m.name}: {'computed' if m.value else 'unavailable'}"
        for m in models
    ]

    if len(available) < 2:
        return SubScore(
            "model_agreement", "Model agreement", 0.25, WEIGHTS["model_agreement"],
            f"Only {len(available)} of {len(IV_MODELS)} intrinsic-value models "
            f"could be computed - there is nothing to cross-check.",
            evidence,
        )

    dispersion = getattr(valuation, "dispersion", None)
    if dispersion is None:
        return SubScore(
            "model_agreement", "Model agreement", 0.5, WEIGHTS["model_agreement"],
            "Dispersion could not be measured.", evidence,
        )

    value = max(0.0, 1.0 - dispersion)
    # A model that could not run is itself a disagreement signal.
    coverage_penalty = len(available) / len(IV_MODELS)
    value *= coverage_penalty

    return SubScore(
        "model_agreement", "Model agreement", value, WEIGHTS["model_agreement"],
        f"{len(available)}/{len(IV_MODELS)} intrinsic-value models computed, "
        f"spanning {dispersion:.0%} between the lowest and highest.",
        evidence,
    )


def _forensic(forensic: Any) -> SubScore:
    flag = getattr(forensic, "flag", "unknown")
    raw = getattr(forensic, "score", None)
    checks = getattr(forensic, "checks", []) or []
    ran = [c for c in checks if c.status != "skipped"]
    skipped = [c for c in checks if c.status == "skipped"]

    if raw is None:
        return SubScore(
            "forensic", "Forensic pass", 0.2, WEIGHTS["forensic"],
            "No forensic check could be run on the available data.",
            [f"skipped: {c.title}" for c in skipped][:8],
        )

    value = raw / 100.0
    if flag == "fail":
        value = min(value, 0.35)
    elif flag == "watch":
        value = min(value, 0.75)

    evidence = [f"{c.status}: {c.title}" for c in ran]
    if skipped:
        evidence.append(f"{len(skipped)} check(s) skipped for want of data")
    return SubScore(
        "forensic", "Forensic pass", value, WEIGHTS["forensic"],
        f"Forensic flag '{flag}' from {len(ran)} checks "
        f"({len(skipped)} skipped); raw score {raw:.0f}/100.",
        evidence,
    )


def _recency(db: Any, config: Any, provider_result: Any) -> SubScore:
    """Fresh data scores full marks; stale statements and stale prices decay."""
    evidence: List[str] = []
    now = _dt.datetime.now(_dt.timezone.utc)

    last_price = db.scalar(
        "SELECT MAX(date) FROM price_daily WHERE ticker = ?", (config.ticker,)
    )
    price_score = 0.0
    if last_price:
        age_days = _age_days(last_price, now)
        if age_days is not None:
            # Allow a long weekend plus a holiday before penalising.
            price_score = 1.0 if age_days <= 5 else max(0.0, 1.0 - (age_days - 5) / 60.0)
            evidence.append(f"latest close {last_price} ({age_days} days old)")
    else:
        evidence.append("no price history")

    last_period = db.scalar(
        "SELECT MAX(period_end) FROM income_statement "
        "WHERE ticker = ? AND period_type = 'annual'",
        (config.ticker,),
    )
    statement_score = 0.0
    if last_period:
        age_days = _age_days(last_period, now)
        if age_days is not None:
            # A fiscal year plus a reporting lag is normal; beyond ~18 months
            # the "latest" annual figures are genuinely stale.
            statement_score = (
                1.0 if age_days <= 400 else max(0.0, 1.0 - (age_days - 400) / 550.0)
            )
            evidence.append(f"latest fiscal close {last_period} ({age_days} days old)")
    else:
        evidence.append("no annual statements")

    value = price_score * 0.4 + statement_score * 0.6
    return SubScore(
        "recency", "Recency", value, WEIGHTS["recency"],
        f"Price freshness {price_score:.0%}, statement freshness {statement_score:.0%}.",
        evidence,
    )


def _age_days(date_text: str, now: _dt.datetime) -> Optional[int]:
    try:
        d = _dt.date.fromisoformat(str(date_text)[:10])
    except ValueError:
        return None
    return (now.date() - d).days
