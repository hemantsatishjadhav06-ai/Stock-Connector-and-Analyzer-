"""§3 Fundamental analysis.

Growth, margins, returns, DuPont, capital allocation, EVA/ROIIC, leverage and
valuation ratios. The arithmetic lives in the SQL views; this module drives
them, computes the multi-window CAGRs, and applies the 15% quality gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import QueryLog, cagr, mean, median, safe_div, stdev

WINDOWS = (10, 7, 5, 3)


@dataclass
class FundamentalResult:
    annual: List[Dict[str, Any]] = field(default_factory=list)
    margins: List[Dict[str, Any]] = field(default_factory=list)
    returns: List[Dict[str, Any]] = field(default_factory=list)
    dupont: List[Dict[str, Any]] = field(default_factory=list)
    leverage: List[Dict[str, Any]] = field(default_factory=list)
    capital_allocation: List[Dict[str, Any]] = field(default_factory=list)
    historic_valuation: List[Dict[str, Any]] = field(default_factory=list)

    growth: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)
    eva: List[Dict[str, Any]] = field(default_factory=list)
    roiic: Dict[str, Any] = field(default_factory=dict)
    buffett_dollar_test: Dict[str, Any] = field(default_factory=dict)
    valuation_ratios: Dict[str, Any] = field(default_factory=dict)
    latest: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def analyse(db: Any, ticker: str, config: Any, log: QueryLog) -> FundamentalResult:
    res = FundamentalResult()
    a = config.assumptions

    res.annual = log.run(
        db,
        "annual_spine",
        "SELECT * FROM v_annual WHERE ticker = ? ORDER BY yr_idx",
        (ticker,),
        "One row per reported fiscal year: P&L, balance sheet and cash flow joined.",
    )
    if not res.annual:
        res.notes.append("No annual statements loaded - §3 could not be computed.")
        return res

    res.margins = log.run(
        db, "margins",
        "SELECT * FROM v_margins WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "Gross / EBITDA / operating / net margin per year (pricing-power trend).",
    )
    res.returns = log.run(
        db, "returns",
        "SELECT * FROM v_returns WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "ROE, ROCE, ROIC plus NOPAT and capital employed for the EVA charge.",
    )
    res.dupont = log.run(
        db, "dupont",
        "SELECT * FROM v_dupont WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "ROE = NPM x asset turnover x leverage; ROA = NPM x asset turnover.",
    )
    res.leverage = log.run(
        db, "leverage",
        "SELECT * FROM v_leverage WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "D/E, Debt/EBITDA, interest coverage, net debt.",
    )
    res.capital_allocation = log.run(
        db, "capital_allocation",
        "SELECT * FROM v_capital_allocation WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "Capex intensity, reinvestment rate and retained earnings.",
    )
    res.historic_valuation = log.run(
        db, "historic_valuation",
        "SELECT * FROM v_historic_valuation WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "P/E, P/B, P/S, P/CF and EV/EBITDA marked at each fiscal close.",
    )

    res.latest = res.annual[-1]
    res.growth = _growth(res.annual)
    res.quality = _quality_gate(res.returns, res.margins, a.quality_return_gate)
    res.eva = _eva(res.returns, a.cost_of_capital)
    res.roiic = _roiic(res.annual)
    res.buffett_dollar_test = _buffett_dollar_test(db, ticker, res.annual, log)
    res.valuation_ratios = _valuation_context(db, ticker, res.historic_valuation, log)
    return res


# -- growth ----------------------------------------------------------------


def _growth(annual: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sales/EPS/PAT CAGR over each window, plus a best/worst-case band."""
    out: Dict[str, Any] = {"windows": {}, "available_years": len(annual)}
    for window in WINDOWS:
        if len(annual) < window + 1:
            out["windows"][window] = None
            continue
        begin, end = annual[-(window + 1)], annual[-1]
        out["windows"][window] = {
            "sales_cagr": cagr(begin.get("sales"), end.get("sales"), window),
            "eps_cagr": cagr(begin.get("eps"), end.get("eps"), window),
            "pat_cagr": cagr(begin.get("pat"), end.get("pat"), window),
            "from": begin.get("period_label"),
            "to": end.get("period_label"),
        }

    # Year-on-year distribution drives the best/worst case, so a single
    # freak year cannot set the forecast on its own.
    yoy: List[float] = []
    for prev, cur in zip(annual, annual[1:]):
        if prev.get("sales") and cur.get("sales") and prev["sales"] > 0:
            yoy.append((cur["sales"] - prev["sales"]) / prev["sales"] * 100.0)
    out["yoy_sales_growth"] = yoy
    if yoy:
        out["best_case_growth"] = median(sorted(yoy)[len(yoy) // 2:])
        out["worst_case_growth"] = median(sorted(yoy)[: max(1, len(yoy) // 2)])
        out["median_growth"] = median(yoy)
        out["growth_volatility"] = stdev(yoy)

    realised = [
        out["windows"][w]["sales_cagr"]
        for w in WINDOWS
        if out["windows"].get(w) and out["windows"][w]["sales_cagr"] is not None
    ]
    out["blended_sales_cagr"] = mean(realised)
    eps_realised = [
        out["windows"][w]["eps_cagr"]
        for w in WINDOWS
        if out["windows"].get(w) and out["windows"][w]["eps_cagr"] is not None
    ]
    out["blended_eps_cagr"] = mean(eps_realised)
    return out


# -- quality gate ----------------------------------------------------------


def _quality_gate(
    returns: List[Dict[str, Any]], margins: List[Dict[str, Any]], gate: float
) -> Dict[str, Any]:
    """'Above 15% across the years' applied to ROE, ROCE and ROIC."""
    out: Dict[str, Any] = {"gate": gate, "metrics": {}}
    for metric in ("roe", "roce", "roic"):
        series = [r.get(metric) for r in returns if r.get(metric) is not None]
        if not series:
            out["metrics"][metric] = None
            continue
        above = [v for v in series if v >= gate]
        out["metrics"][metric] = {
            "median": median(series),
            "mean": mean(series),
            "latest": series[-1],
            "years": len(series),
            "years_above_gate": len(above),
            "share_above_gate": len(above) / len(series) * 100.0,
            "clears_gate": len(above) / len(series) >= 0.7,
        }

    cleared = [
        m["clears_gate"] for m in out["metrics"].values() if isinstance(m, dict)
    ]
    out["clears_all_three"] = bool(cleared) and all(cleared)
    out["high_quality"] = out["clears_all_three"]

    opm = [m.get("opm") for m in margins if m.get("opm") is not None]
    if len(opm) >= 3:
        first_half, second_half = opm[: len(opm) // 2], opm[len(opm) // 2:]
        delta = (mean(second_half) or 0) - (mean(first_half) or 0)
        out["margin_trend"] = (
            "expanding" if delta > 1.0 else "compressing" if delta < -1.0 else "stable"
        )
        out["margin_trend_bps"] = delta * 100.0
        out["margin_volatility"] = stdev(opm)
        # Stable-or-expanding margins across a decade is the observable
        # footprint of pricing power.
        out["pricing_power"] = out["margin_trend"] in ("expanding", "stable")
    else:
        out["margin_trend"] = None
        out["pricing_power"] = None
    return out


# -- EVA / ROIIC -----------------------------------------------------------


def _eva(returns: List[Dict[str, Any]], cost_of_capital: float) -> List[Dict[str, Any]]:
    """EVA = NOPAT - (capital employed x cost of capital)."""
    out = []
    for row in returns:
        nopat, capital = row.get("nopat"), row.get("capital_employed")
        if nopat is None or not capital:
            continue
        charge = capital * cost_of_capital
        out.append(
            {
                "period_label": row["period_label"],
                "yr_idx": row["yr_idx"],
                "nopat": nopat,
                "capital_employed": capital,
                "capital_charge": charge,
                "eva": nopat - charge,
                "eva_spread_pct": (nopat / capital - cost_of_capital) * 100.0,
            }
        )
    return out


def _roiic(annual: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return on *incremental* invested capital.

    Measured over a multi-year span, not year-on-year: a single year's capex
    rarely produces that same year's profit, so the annual ratio is noise.
    """
    usable = [
        r for r in annual
        if r.get("ebit") is not None and r.get("net_worth") is not None
    ]
    if len(usable) < 4:
        return {"roiic": None, "note": "needs at least 4 years of data"}

    span = min(5, len(usable) - 1)
    begin, end = usable[-(span + 1)], usable[-1]

    def invested(row: Dict[str, Any]) -> float:
        return (
            (row.get("net_worth") or 0.0)
            + (row.get("borrowings") or 0.0)
            - (row.get("cash") or 0.0)
        )

    tax_rate = end.get("tax_rate") or 0.25
    d_nopat = ((end.get("ebit") or 0.0) - (begin.get("ebit") or 0.0)) * (1 - tax_rate)
    d_capital = invested(end) - invested(begin)
    roiic = None
    if d_capital > 0:
        roiic = d_nopat / d_capital * 100.0

    reinv = [r.get("reinvestment_rate") for r in annual if r.get("reinvestment_rate") is not None]
    avg_reinv = mean(reinv)
    return {
        "roiic": roiic,
        "span_years": span,
        "from": begin.get("period_label"),
        "to": end.get("period_label"),
        "delta_nopat": d_nopat,
        "delta_invested_capital": d_capital,
        "avg_reinvestment_rate": avg_reinv,
        # Intrinsic compounding rate: what the business grows at if it keeps
        # reinvesting at the incremental return it has actually earned.
        "intrinsic_compounding_rate": (
            roiic * avg_reinv if roiic is not None and avg_reinv is not None else None
        ),
        "note": None if roiic is not None else "invested capital did not increase",
    }


# -- Buffett's one-dollar test --------------------------------------------


def _buffett_dollar_test(
    db: Any, ticker: str, annual: List[Dict[str, Any]], log: QueryLog
) -> Dict[str, Any]:
    """Has each $1 retained created at least $1 of market value?"""
    retained_rows = log.run(
        db,
        "retained_earnings",
        """
        SELECT period_label, period_end, yr_idx, retained_earnings
        FROM v_capital_allocation
        WHERE ticker = ? AND retained_earnings IS NOT NULL
        ORDER BY yr_idx
        """,
        (ticker,),
        "Cumulative retained earnings for the Buffett $1-retained test.",
    )
    if len(retained_rows) < 3:
        return {"applicable": False, "reason": "insufficient retained-earnings history"}

    cumulative = sum(r["retained_earnings"] for r in retained_rows)

    cap = log.run(
        db,
        "market_cap_endpoints",
        """
        WITH bounds AS (
            SELECT MIN(period_end) AS first_end, MAX(period_end) AS last_end
            FROM v_annual WHERE ticker = ?
        )
        SELECT
            (SELECT close FROM price_daily p, bounds b
              WHERE p.ticker = ? AND p.date <= b.first_end
              ORDER BY p.date DESC LIMIT 1)                     AS first_price,
            (SELECT close FROM price_daily p, bounds b
              WHERE p.ticker = ? AND p.date <= b.last_end
              ORDER BY p.date DESC LIMIT 1)                     AS last_price,
            (SELECT shares_outstanding FROM company WHERE ticker = ?) AS shares
        """,
        (ticker, ticker, ticker, ticker),
        "Market value at the first and last fiscal close, for the $1 test.",
    )
    row = cap[0] if cap else {}
    first_price, last_price, shares = (
        row.get("first_price"), row.get("last_price"), row.get("shares")
    )
    if not (first_price and last_price and shares):
        return {
            "applicable": False,
            "reason": "price history does not span the statement history",
            "cumulative_retained": cumulative,
        }

    delta_value = (last_price - first_price) * shares
    ratio = safe_div(delta_value, cumulative)
    return {
        "applicable": True,
        "cumulative_retained": cumulative,
        "market_value_added": delta_value,
        "value_per_dollar_retained": ratio,
        "passes": bool(ratio is not None and ratio >= 1.0),
        "from": retained_rows[0]["period_label"],
        "to": retained_rows[-1]["period_label"],
    }


# -- valuation context -----------------------------------------------------


def _valuation_context(
    db: Any, ticker: str, historic: List[Dict[str, Any]], log: QueryLog
) -> Dict[str, Any]:
    stats = log.run(
        db,
        "valuation_bands",
        """
        SELECT
            MAX(pe)        AS max_pe,
            MIN(pe)        AS min_pe,
            AVG(pe)        AS avg_pe,
            MAX(ev_ebitda) AS max_ev_ebitda,
            MIN(ev_ebitda) AS min_ev_ebitda,
            AVG(ev_ebitda) AS avg_ev_ebitda,
            AVG(pb)        AS avg_pb,
            AVG(ps)        AS avg_ps,
            AVG(pcf)       AS avg_pcf
        FROM v_historic_valuation
        WHERE ticker = ?
        """,
        (ticker,),
        "10-year max/min/average multiples, the context band for today's price.",
    )
    out: Dict[str, Any] = dict(stats[0]) if stats else {}
    if historic:
        out["latest"] = historic[-1]
    out["median_pe"] = median([r.get("pe") for r in historic])
    out["median_ev_ebitda"] = median([r.get("ev_ebitda") for r in historic])
    return out
