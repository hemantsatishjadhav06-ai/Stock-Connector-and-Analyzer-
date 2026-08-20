"""Run configuration and the default valuation assumptions.

Every default here comes from the reference valuation workbook encoded in the
master prompt (§6). Anything a run changes is recorded as an *override* in the
``assumption`` table with a rationale, so a subscriber reading the appendix can
see exactly which levers were moved and why.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
    adx_period: int = 14
    adx_trend_floor: float = 20.0     # ADX above this = trending, below = range

    # --- Signals / strategies (§S) --------------------------------------
    min_fused_score: float = 65.0     # fused score needed before a setup is actionable
    risk_per_trade_pct: float = 0.5   # paper-trading position sizing
    max_open_positions: int = 5
    starting_cash: float = 1_000_000.0
    round_trip_cost_bps: float = 30.0 # brokerage + STT + exchange + GST + stamp + slippage

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
    "adx_trend_floor": "ADX",
    "min_fused_score": "0-100",
    "risk_per_trade_pct": "% of equity",
    "round_trip_cost_bps": "bps",
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
class DataSettings:
    """Credentials and endpoints for the data layer, read from the environment.

    Keys are never hardcoded and never written into a report. A provider with
    no key simply does not join the chain, which shows up as a coverage gap
    rather than a crash.
    """

    twelvedata_api_key: str = ""
    indian_api_base_url: str = ""
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://data.alpaca.markets"
    benchmark: str = ""               # resolved per market when blank
    watchlist: List[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "DataSettings":
        import os

        e = env if env is not None else os.environ
        watchlist = [
            t.strip() for t in (e.get("WATCHLIST", "") or "").split(",") if t.strip()
        ]
        return cls(
            twelvedata_api_key=e.get("TWELVEDATA_API_KEY", "") or "",
            indian_api_base_url=e.get("INDIAN_API_BASE_URL", "") or "",
            alpaca_key_id=e.get("ALPACA_KEY_ID", "") or "",
            alpaca_secret_key=e.get("ALPACA_SECRET_KEY", "") or "",
            alpaca_base_url=e.get("ALPACA_BASE_URL", "") or "https://data.alpaca.markets",
            benchmark=e.get("BENCHMARK", "") or "",
            watchlist=watchlist,
        )

    @property
    def configured(self) -> List[str]:
        names = []
        if self.twelvedata_api_key:
            names.append("twelvedata")
        if self.indian_api_base_url:
            names.append("indian_api")
        if self.alpaca_key_id and self.alpaca_secret_key:
            names.append("alpaca")
        return names


#: Benchmark index per market, for §S regime detection.
BENCHMARKS = {
    "IN": "^NSEI", "INDIA": "^NSEI", "NSE": "^NSEI", "BSE": "^BSESN",
    "US": "^GSPC", "UK": "^FTSE", "EUROPE": "^STOXX50E", "JP": "^N225",
}


def benchmark_for(market: str, override: str = "") -> str:
    if override:
        return override
    return BENCHMARKS.get((market or "").strip().upper(), "^GSPC")


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
    #: Whole-run ceiling on network time. Retry budgets compound across the
    #: ~7 URLs a full run fetches; without a ceiling a throttled source can
    #: keep the CLI silent for minutes. 0 disables the ceiling.
    network_budget_seconds: float = 120.0
    data: DataSettings = field(default_factory=DataSettings)
    #: An already-open warehouse Database. When set, the pipeline stages into
    #: it (replacing just this company's rows) instead of creating its own,
    #: which is what lets many companies share one cached SQL file.
    db_handle: Any = None
    #: §S extras. Off by default so a plain valuation run stays fast.
    with_signals: bool = False
    with_backtest: bool = False
    watchlist: List[str] = field(default_factory=list)
    benchmark: str = ""

    def resolved_benchmark(self) -> str:
        return benchmark_for(self.market, self.benchmark or self.data.benchmark)

    @property
    def run_id(self) -> str:
        return f"{self.ticker}:{self.market}"


#: Markets whose primary screener is Screener.in (fiscal years labelled Mar-YY).
INDIA_MARKETS = {"IN", "INDIA", "NSE", "BSE"}


def market_is_india(market: str) -> bool:
    return (market or "").strip().upper() in INDIA_MARKETS
