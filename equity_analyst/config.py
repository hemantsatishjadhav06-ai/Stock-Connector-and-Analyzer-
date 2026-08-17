"""Run configuration and the default valuation assumptions.

Every default here comes from the reference valuation workbook encoded in the
master prompt (§6). Anything a run changes is recorded as an *override* in the
``assumption`` table with a rationale, so a subscriber reading the appendix can
see exactly which levers were moved and why.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Dict, List, Optional

ENGINE_VERSION = "0.1.0"


@dataclass
class Assumptions:
    """§6 defaults. Override with company-specific data where justified."""

    # --- Warren Buffett Way (10-year DCF) --------------------------------
    fcf_growth: float = 0.20          # years 1-10
    discount_rate: float = 0.07
    terminal_growth: float = 0.02
    forecast_years: int = 10

    # --- Buffetology -----------------------------------------------------
    avg_sustainable_pe: float = 20.0
    # The reference workbook projects EPS 10 years out but annualises the
    # total gain over 9 compounding intervals. We follow the workbook exactly
    # (see docs/METHODOLOGY.md, "Buffetology exponent") rather than silently
    # "correcting" it; set to 10 to annualise over the full decade instead.
    return_annualisation_years: int = 9

    # --- Graham ----------------------------------------------------------
    graham_multiplier: float = 22.5   # 15 P/E x 1.5 P/B
    graham_base_pe: float = 8.5       # no-growth P/E in EPS x (8.5 + 2g)
    graham_growth_cap: float = 0.15   # cap g conservatively at 15%

    # --- EVA / quality gates --------------------------------------------
    cost_of_capital: float = 0.10
    quality_return_gate: float = 15.0 # ROE/ROCE/ROIC must clear 15%

    # --- Margin of safety ------------------------------------------------
    margin_of_safety: float = 0.50    # buy-below = IV x (1 - 0.50)

    # --- Forensic thresholds (§4) ---------------------------------------
    cfo_to_ebitda_min: float = 0.70
    cash_yield_min: float = 0.05
    contingent_liab_max_pct: float = 5.0    # % of net worth
    intangibles_max_pct: float = 10.0       # % of net worth -> avoid
    receivable_provision_max_pct: float = 10.0
    risk_free_rate: float = 0.045

    # --- Technical (§5) --------------------------------------------------
    sma_fast: int = 50
    sma_slow: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    bollinger_period: int = 20
    bollinger_sigma: float = 2.0

    def as_rows(self, defaults: Optional["Assumptions"] = None) -> List[dict]:
        """Flatten to ``assumption`` table rows, marking overrides."""
        base = defaults or Assumptions()
        rows = []
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            rows.append(
                {
                    "key": f.name,
                    "value": float(value),
                    "unit": _UNITS.get(f.name, ""),
                    "rationale": _RATIONALE.get(f.name, ""),
                    "overridden": int(value != getattr(base, f.name)),
                }
            )
        return rows


_UNITS: Dict[str, str] = {
    "fcf_growth": "fraction/yr",
    "discount_rate": "fraction/yr",
    "terminal_growth": "fraction/yr",
    "forecast_years": "years",
    "avg_sustainable_pe": "x",
    "return_annualisation_years": "years",
    "graham_multiplier": "x",
    "graham_base_pe": "x",
    "graham_growth_cap": "fraction/yr",
    "cost_of_capital": "fraction/yr",
    "quality_return_gate": "%",
    "margin_of_safety": "fraction",
    "cfo_to_ebitda_min": "ratio",
    "cash_yield_min": "fraction",
    "contingent_liab_max_pct": "% of net worth",
    "intangibles_max_pct": "% of net worth",
    "receivable_provision_max_pct": "% of receivables",
    "risk_free_rate": "fraction/yr",
}

_RATIONALE: Dict[str, str] = {
    "fcf_growth": "Reference workbook default for years 1-10 FCF growth.",
    "discount_rate": "Reference workbook discount rate.",
    "terminal_growth": "Gordon terminal growth, ~long-run nominal GDP.",
    "avg_sustainable_pe": "Workbook default average sustainable exit multiple.",
    "return_annualisation_years": "Workbook annualises the 10-yr gain over 9 intervals.",
    "graham_multiplier": "Graham's 22.5 = 15x earnings x 1.5x book.",
    "graham_growth_cap": "Graham growth capped to keep the floor conservative.",
    "cost_of_capital": "Charge applied to capital employed in the EVA calc.",
    "quality_return_gate": "'Above 15% across the years' quality gate.",
    "margin_of_safety": "Halve intrinsic value to set the buy-below line.",
    "cfo_to_ebitda_min": "Cash must back accrual profit.",
    "contingent_liab_max_pct": "Breach above 5% of net worth.",
    "intangibles_max_pct": "Intangibles+goodwill above 10% of net worth -> avoid.",
}


@dataclass
class RunConfig:
    """Everything that identifies and parameterises one analysis run."""

    ticker: str
    market: str = "US"
    company_name: Optional[str] = None
    horizon_years: int = 10
    db_path: str = ":memory:"
    output_path: Optional[str] = None
    assumptions: Assumptions = field(default_factory=Assumptions)
    offline_file: Optional[str] = None
    commodities: List[str] = field(default_factory=list)
    price_years: int = 10
    allow_network: bool = True

    @property
    def run_id(self) -> str:
        return f"{self.ticker}:{self.market}"


#: Markets whose primary screener is Screener.in (fiscal years labelled Mar-YY).
INDIA_MARKETS = {"IN", "INDIA", "NSE", "BSE"}


def market_is_india(market: str) -> bool:
    return (market or "").strip().upper() in INDIA_MARKETS
