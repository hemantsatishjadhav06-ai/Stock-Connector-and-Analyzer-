"""§6 The four valuation models, plus the margin-of-safety rule.

Each model returns a :class:`ModelResult` carrying the intrinsic value, the
inputs it used, the formula as written, and any caveat that should travel with
the number. A model that cannot be computed returns ``value=None`` with a
reason -- it is never quietly dropped, because a missing model widens model
dispersion and must show up in the §8 agreement sub-score.

Conventions
    * ``growth`` inputs are fractions (0.20), not percentages.
    * per-share outputs are in the reporting currency, using the schema
      invariant that shares are stored in the same scale as the money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..analysis import mean, median


@dataclass
class ModelResult:
    name: str
    value: Optional[float] = None          # intrinsic value per share
    formula: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    workings: List[Dict[str, Any]] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    unavailable_reason: Optional[str] = None
    # Buffetology reports expected annual return rather than an IV.
    expected_return: Optional[Dict[str, Optional[float]]] = None

    @property
    def available(self) -> bool:
        return self.value is not None or self.expected_return is not None


@dataclass
class ValuationResult:
    models: List[ModelResult] = field(default_factory=list)
    current_price: Optional[float] = None
    currency: str = ""
    selected_value: Optional[float] = None
    selected_basis: str = ""
    buy_below: Optional[float] = None
    margin_of_safety: float = 0.50
    zone: str = "unknown"          # buy | near | expensive | unknown
    upside_pct: Optional[float] = None
    dispersion: Optional[float] = None
    agreement: Optional[float] = None   # 0-1, feeds §8
    divergence_note: str = ""
    notes: List[str] = field(default_factory=list)

    def by_name(self, name: str) -> Optional[ModelResult]:
        for m in self.models:
            if m.name == name:
                return m
        return None


# =====================================================================
# 1. Warren Buffett Way -- 10-year DCF of free cash flow
# =====================================================================


def buffett_dcf(
    fcf_base: Optional[float],
    shares: Optional[float],
    net_cash: float,
    a: Any,
    growth: Optional[float] = None,
) -> ModelResult:
    g = a.fcf_growth if growth is None else growth
    res = ModelResult(
        name="Warren Buffett Way (DCF)",
        formula=(
            "PV = Σ[t=1..N] FCF₀(1+g)ᵗ / (1+d)ᵗ  +  "
            "[FCF_N(1+g_term) / (d − g_term)] / (1+d)^N ; "
            "then + net cash, ÷ shares"
        ),
        inputs={
            "fcf_base": fcf_base,
            "growth": g,
            "discount_rate": a.discount_rate,
            "terminal_growth": a.terminal_growth,
            "years": a.forecast_years,
            "net_cash": net_cash,
            "shares": shares,
        },
    )
    if not fcf_base or fcf_base <= 0:
        res.unavailable_reason = (
            "Base free cash flow is missing or negative - a DCF on a negative "
            "cash flow would compound a loss into a fictitious value."
        )
        return res
    if not shares or shares <= 0:
        res.unavailable_reason = "Share count unavailable, cannot express value per share."
        return res
    if a.discount_rate <= a.terminal_growth:
        res.unavailable_reason = (
            f"Discount rate ({a.discount_rate:.1%}) must exceed terminal growth "
            f"({a.terminal_growth:.1%}); the Gordon formula diverges otherwise."
        )
        return res

    pv_sum = 0.0
    fcf = fcf_base
    for year in range(1, a.forecast_years + 1):
        fcf = fcf * (1 + g)
        discount = (1 + a.discount_rate) ** year
        pv = fcf / discount
        pv_sum += pv
        res.workings.append(
            {"year": year, "fcf": fcf, "discount_factor": 1 / discount, "pv": pv}
        )

    terminal = fcf * (1 + a.terminal_growth) / (a.discount_rate - a.terminal_growth)
    terminal_pv = terminal / ((1 + a.discount_rate) ** a.forecast_years)
    equity_value = pv_sum + terminal_pv + net_cash

    res.inputs.update(
        {
            "pv_explicit": pv_sum,
            "terminal_value": terminal,
            "terminal_pv": terminal_pv,
            "equity_value": equity_value,
            "terminal_share_of_value": terminal_pv / (pv_sum + terminal_pv)
            if (pv_sum + terminal_pv)
            else None,
        }
    )
    res.value = equity_value / shares

    share = res.inputs["terminal_share_of_value"]
    if share and share > 0.75:
        res.caveats.append(
            f"{share:.0%} of the value sits in the terminal value - the answer is "
            f"driven by the perpetuity assumption more than by the forecast."
        )
    if g > 0.15:
        res.caveats.append(
            f"A {g:.0%} FCF growth rate sustained for {a.forecast_years} years is "
            f"an aggressive assumption; few businesses compound that long."
        )
    return res


# =====================================================================
# 2. Benjamin Graham -- the conservative floor
# =====================================================================


def graham(
    eps: Optional[float],
    bvps: Optional[float],
    growth_pct: Optional[float],
    a: Any,
) -> ModelResult:
    res = ModelResult(
        name="Benjamin Graham Way",
        formula=(
            "Graham number = √(22.5 × EPS × BVPS); "
            "cross-check: IV = EPS × (8.5 + 2g), g capped"
        ),
        inputs={"eps": eps, "bvps": bvps, "growth_pct": growth_pct,
                "multiplier": a.graham_multiplier, "growth_cap": a.graham_growth_cap},
    )
    if not eps or eps <= 0:
        res.unavailable_reason = (
            "EPS is missing or negative - Graham's method is undefined for a "
            "loss-making year."
        )
        return res
    if not bvps or bvps <= 0:
        res.unavailable_reason = "Book value per share is missing or negative."
        return res

    graham_number = (a.graham_multiplier * eps * bvps) ** 0.5
    res.inputs["graham_number"] = graham_number

    earnings_form = None
    if growth_pct is not None:
        capped = min(max(growth_pct, 0.0), a.graham_growth_cap * 100.0)
        earnings_form = eps * (a.graham_base_pe + 2 * capped)
        res.inputs["capped_growth_pct"] = capped
        res.inputs["earnings_growth_form"] = earnings_form

    # The floor is the point of this model, so take the lower of the two reads.
    candidates = [v for v in (graham_number, earnings_form) if v]
    res.value = min(candidates) if candidates else graham_number
    res.inputs["selected"] = (
        "graham_number" if res.value == graham_number else "earnings_growth_form"
    )
    res.caveats.append(
        "Graham's method is a conservative floor, not a target: it ignores "
        "franchise value and should read lowest of the three."
    )
    if earnings_form and graham_number and max(earnings_form, graham_number) > 0:
        spread = abs(earnings_form - graham_number) / max(earnings_form, graham_number)
        if spread > 0.5:
            res.caveats.append(
                f"The two Graham forms disagree by {spread:.0%}; the lower was taken."
            )
    return res


# =====================================================================
# 3. Bharat Shah -- earnings power x intrinsic compounding
# =====================================================================


def bharat_shah(
    eps: Optional[float],
    roiic_pct: Optional[float],
    reinvestment_rate: Optional[float],
    quality: Dict[str, Any],
    a: Any,
    horizon: int = 10,
) -> ModelResult:
    """Value durable earnings power, compounded at the rate the business has
    actually earned on incremental capital.

    Intrinsic compounding rate = ROIIC × reinvestment rate. A business earning
    25% on incremental capital and reinvesting 60% of its cash compounds
    earnings power at ~15% -- and quality justifies a higher exit multiple.
    """
    res = ModelResult(
        name="Bharat Shah Way",
        formula=(
            "g_intrinsic = ROIIC × reinvestment rate; "
            "IV = EPS × (1+g_intrinsic)^N × quality-adjusted P/E, discounted at d"
        ),
        inputs={
            "eps": eps,
            "roiic_pct": roiic_pct,
            "reinvestment_rate": reinvestment_rate,
            "horizon": horizon,
            "discount_rate": a.discount_rate,
        },
    )
    if not eps or eps <= 0:
        res.unavailable_reason = "EPS is missing or negative - no earnings power to value."
        return res
    if roiic_pct is None or reinvestment_rate is None:
        res.unavailable_reason = (
            "ROIIC or reinvestment rate could not be measured, so the intrinsic "
            "compounding rate is unknown."
        )
        return res

    g = (roiic_pct / 100.0) * reinvestment_rate
    # Cap at the DCF growth assumption: no business out-compounds its own
    # incremental economics forever, and an uncapped ROIIC x reinvestment on a
    # single good stretch produces absurd numbers.
    capped_g = max(0.0, min(g, a.fcf_growth))
    if capped_g != g:
        res.caveats.append(
            f"Intrinsic compounding rate capped from {g:.1%} to {capped_g:.1%} "
            f"(the DCF growth ceiling)."
        )

    exit_pe = a.avg_sustainable_pe
    metrics = quality.get("metrics") or {}
    roe_block = metrics.get("roe") if isinstance(metrics.get("roe"), dict) else None
    if quality.get("clears_all_three"):
        exit_pe *= 1.25
        res.caveats.append(
            "Exit multiple lifted 25% for clearing the 15% ROE/ROCE/ROIC gate."
        )
    elif roe_block and not roe_block.get("clears_gate"):
        exit_pe *= 0.80
        res.caveats.append("Exit multiple cut 20%: returns do not clear the quality gate.")

    future_eps = eps * ((1 + capped_g) ** horizon)
    future_price = future_eps * exit_pe
    present = future_price / ((1 + a.discount_rate) ** horizon)

    res.inputs.update(
        {
            "intrinsic_compounding_rate": capped_g,
            "exit_pe": exit_pe,
            "future_eps": future_eps,
            "future_price": future_price,
        }
    )
    res.value = present
    res.caveats.append(
        "Quality-adjusted and typically the highest of the three intrinsic values; "
        "it assumes the franchise and its reinvestment runway both persist."
    )
    return res


# =====================================================================
# 4. Buffetology (Mary Buffett) -- expected annual return
# =====================================================================


def buffetology(
    eps: Optional[float],
    bvps: Optional[float],
    current_price: Optional[float],
    historical_growth_pct: Optional[float],
    roe_pct: Optional[float],
    payout_ratio: Optional[float],
    dividend_per_share: Optional[float],
    a: Any,
) -> ModelResult:
    """Project EPS/BVPS 10 years two ways and annualise the total gain.

    (a) historical/visibility growth, (b) sustainable growth = RoE × retention.
    """
    res = ModelResult(
        name="Buffetology (Mary Buffett)",
        formula=(
            "EPS₁₀ = EPS₀(1+g)^10 ; Projected price = avg sustainable P/E × EPS₁₀ ; "
            "Total gain = projected price + Σ dividends ; "
            "Annual return = (Total gain / Current price)^(1/n) − 1"
        ),
        inputs={
            "eps": eps,
            "bvps": bvps,
            "current_price": current_price,
            "historical_growth_pct": historical_growth_pct,
            "roe_pct": roe_pct,
            "payout_ratio": payout_ratio,
            "avg_sustainable_pe": a.avg_sustainable_pe,
            "annualisation_years": a.return_annualisation_years,
        },
    )
    if not eps or eps <= 0:
        res.unavailable_reason = "EPS is missing or negative."
        return res
    if not current_price or current_price <= 0:
        res.unavailable_reason = "Current price unavailable, so a return cannot be computed."
        return res

    retention = None
    if payout_ratio is not None:
        retention = max(0.0, min(1.0, 1.0 - payout_ratio / 100.0))
    sustainable_growth = (
        (roe_pct / 100.0) * retention
        if roe_pct is not None and retention is not None
        else None
    )

    projections: Dict[str, Optional[float]] = {}
    workings: List[Dict[str, Any]] = []

    for label, growth_pct in (
        ("historical", historical_growth_pct),
        (
            "sustainable",
            sustainable_growth * 100.0 if sustainable_growth is not None else None,
        ),
    ):
        if growth_pct is None:
            projections[label] = None
            continue
        g = growth_pct / 100.0
        eps10 = eps * ((1 + g) ** 10)
        projected_price = a.avg_sustainable_pe * eps10

        # Cumulative dividends over the projection, grown with earnings.
        cumulative_dividends = 0.0
        if dividend_per_share:
            d = dividend_per_share
            for _ in range(10):
                d = d * (1 + g)
                cumulative_dividends += d
        elif payout_ratio:
            e = eps
            for _ in range(10):
                e = e * (1 + g)
                cumulative_dividends += e * payout_ratio / 100.0

        total_gain = projected_price + cumulative_dividends
        n = max(1, a.return_annualisation_years)
        annual_return = (total_gain / current_price) ** (1.0 / n) - 1.0

        projections[label] = annual_return
        workings.append(
            {
                "basis": label,
                "growth_pct": growth_pct,
                "eps_year10": eps10,
                "projected_price": projected_price,
                "cumulative_dividends": cumulative_dividends,
                "total_gain": total_gain,
                "annual_return": annual_return,
            }
        )
        # BVPS projection is reported for context (Mary Buffett's second table).
        if bvps and label == "sustainable" and sustainable_growth is not None:
            workings[-1]["bvps_year10"] = bvps * ((1 + sustainable_growth) ** 10)

    res.workings = workings
    res.expected_return = projections
    res.inputs["sustainable_growth"] = sustainable_growth
    res.inputs["retention"] = retention

    available = [v for v in projections.values() if v is not None]
    if not available:
        res.unavailable_reason = (
            "Neither a historical growth rate nor RoE x retention could be "
            "established."
        )
        return res

    # Express the conservative leg as an implied per-share value so this model
    # can join the football field: discount the projected outcome back at the
    # required rate. It stays a *derived* view -- the headline output is the
    # expected annual return.
    conservative = min(available)
    matching = min(workings, key=lambda w: w["annual_return"])
    res.value = matching["total_gain"] / (
        (1 + a.discount_rate) ** a.return_annualisation_years
    )
    res.inputs["implied_value_basis"] = matching["basis"]
    res.caveats.append(
        f"Headline output is an expected annual return, not an intrinsic value. "
        f"The per-share figure shown is the conservative ({matching['basis']}) leg "
        f"discounted back at {a.discount_rate:.0%}."
    )
    if a.return_annualisation_years != 10:
        res.caveats.append(
            f"Gain annualised over {a.return_annualisation_years} years against a "
            f"10-year projection, per the reference workbook."
        )
    if conservative is not None and conservative < a.discount_rate:
        res.caveats.append(
            f"Expected return ({conservative:.1%}) is below the {a.discount_rate:.0%} "
            f"discount rate - the projection does not clear its own hurdle."
        )
    return res


# =====================================================================
# Assembly + margin of safety
# =====================================================================

#: Which models are true intrinsic-value estimates for the football field.
IV_MODELS = ("Warren Buffett Way (DCF)", "Benjamin Graham Way", "Bharat Shah Way")


def value(
    fundamentals: Any,
    current_price: Optional[float],
    shares: Optional[float],
    currency: str,
    a: Any,
) -> ValuationResult:
    """Run all four models off the fundamentals and apply the MoS rule."""
    res = ValuationResult(current_price=current_price, currency=currency,
                          margin_of_safety=a.margin_of_safety)
    latest = fundamentals.latest or {}
    annual = fundamentals.annual or []

    def latest_of(field: str) -> Optional[Any]:
        """Most recent reported value for a field.

        Statements do not all land at once: a company can have filed its P&L
        for the newest year while the balance sheet still shows the prior one.
        Reading the newest *row* would then silently drop book value and kill
        Graham's model, so each input is resolved on its own timeline and the
        period it came from is reported alongside it.
        """
        for row in reversed(annual):
            if row.get(field) is not None:
                return row[field]
        return None

    eps = latest_of("eps")
    net_worth = latest_of("net_worth")
    bvps = (net_worth / shares) if (net_worth and shares) else None
    res.notes.append(
        "Model inputs are resolved field-by-field from the most recent year that "
        "reports each one, so a partially-filed year does not blank out a model."
    )

    # Base FCF: median of the last three years, so one capex-light or
    # capex-heavy year cannot set a ten-year forecast on its own.
    recent_fcf = [r.get("fcf") for r in annual[-3:] if r.get("fcf") is not None]
    fcf_base = median(recent_fcf) if recent_fcf else None
    if fcf_base is not None and recent_fcf and len(recent_fcf) > 1:
        res.notes.append(
            f"DCF base FCF is the median of the last {len(recent_fcf)} years, "
            f"not the latest year alone."
        )

    cash, borrowings = latest_of("cash"), latest_of("borrowings")
    net_cash = 0.0
    if cash is not None or borrowings is not None:
        net_cash = (cash or 0.0) - (borrowings or 0.0)
    if cash is None and borrowings is not None:
        res.notes.append(
            "Cash is not separately reported by this source, so the DCF's net-debt "
            "bridge subtracts gross borrowings. That understates equity value; the "
            "error is conservative."
        )

    growth = fundamentals.growth or {}
    sales_cagr = growth.get("blended_sales_cagr")
    eps_cagr = growth.get("blended_eps_cagr")
    # Prefer earnings growth for earnings-based models; fall back to sales.
    growth_pct = eps_cagr if eps_cagr is not None else sales_cagr

    quality = fundamentals.quality or {}
    roe_metric = (quality.get("metrics") or {}).get("roe")
    roe_pct = roe_metric.get("median") if isinstance(roe_metric, dict) else None

    payout = mean([r.get("dividend_payout") for r in annual[-5:]])
    dps = latest_of("dividend_per_share")

    roiic = fundamentals.roiic or {}

    res.models = [
        buffett_dcf(fcf_base, shares, net_cash, a),
        graham(eps, bvps, growth_pct, a),
        bharat_shah(
            eps, roiic.get("roiic"), roiic.get("avg_reinvestment_rate"), quality, a
        ),
        buffetology(eps, bvps, current_price, growth_pct, roe_pct, payout, dps, a),
    ]

    _apply_margin_of_safety(res, a)
    return res


def _apply_margin_of_safety(res: ValuationResult, a: Any) -> None:
    ivs = [
        m.value
        for m in res.models
        if m.name in IV_MODELS and m.value is not None and m.value > 0
    ]
    if not ivs:
        res.notes.append(
            "No intrinsic value could be computed from any model - no buy-below "
            "line is published."
        )
        res.zone = "unknown"
        return

    lo, hi = min(ivs), max(ivs)
    res.dispersion = (hi - lo) / hi if hi > 0 else None
    # Tight cluster -> high agreement. Feeds the §8 model-agreement sub-score.
    res.agreement = max(0.0, 1.0 - (res.dispersion or 0.0)) if res.dispersion is not None else None

    # "When models disagree, lean conservative and say why."
    if len(ivs) >= 2 and (res.dispersion or 0) > 0.5:
        res.selected_value = median(ivs)
        res.selected_basis = (
            f"median of {len(ivs)} intrinsic values (dispersion "
            f"{res.dispersion:.0%} is wide, so the middle read is taken)"
        )
        res.divergence_note = (
            f"The models span {lo:,.2f}-{hi:,.2f} {res.currency}. Wide dispersion "
            f"usually means the DCF's growth assumption and Graham's asset floor "
            f"are describing different businesses; the median is used and the "
            f"buy-below line halves it again."
        )
    else:
        res.selected_value = median(ivs)
        res.selected_basis = f"median of {len(ivs)} intrinsic values"
        res.divergence_note = (
            f"The models cluster within {res.dispersion:.0%} of each other, so the "
            f"central estimate is well supported."
            if res.dispersion is not None
            else ""
        )

    res.buy_below = res.selected_value * (1.0 - a.margin_of_safety)

    if res.current_price:
        res.upside_pct = (
            (res.selected_value - res.current_price) / res.current_price * 100.0
        )
        if res.current_price <= res.buy_below:
            res.zone = "buy"
        elif res.current_price <= res.buy_below * 1.15:
            res.zone = "near"
        else:
            res.zone = "expensive"
    else:
        res.zone = "unknown"
        res.notes.append("Current price unavailable - the price zone cannot be set.")
