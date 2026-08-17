"""OpenBB Platform provider (optional).

The master prompt names OpenBB as the analytical engine. It is a heavy,
optional dependency, so it is imported lazily: if ``openbb`` is not installed
or its configured data vendor errors, the run continues on the other providers
and the substitution is recorded as a coverage note -- which is what §8 wants,
rather than a hard failure.

Mapped calls: ``obb.equity.price.historical``, ``obb.equity.fundamental.income``
/ ``.balance`` / ``.cash``, ``obb.equity.profile`` and ``obb.news.company``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import Provider, ProviderResult


def openbb_available() -> bool:
    try:
        import openbb  # noqa: F401
    except Exception:
        return False
    return True


class OpenBBProvider(Provider):
    name = "openbb"
    source_tier = "vendor"

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        res = ProviderResult(provider=self.name, source_tier=self.source_tier)
        try:
            from openbb import obb  # type: ignore
        except Exception as exc:
            res.note_gap(
                "openbb",
                f"OpenBB unavailable ({exc}); substituted with direct providers",
            )
            return res

        limit = max(10, int(getattr(config, "horizon_years", 10)))

        res.company = self._profile(obb, ticker, res) or {}
        self._statements(obb, ticker, res, limit)
        self._prices(obb, ticker, res, config)
        self._news(obb, ticker, res)
        return res

    # -- individual calls, each independently degradable -----------------
    def _profile(self, obb: Any, ticker: str, res: ProviderResult) -> Optional[Dict[str, Any]]:
        try:
            rows = _to_records(obb.equity.profile(symbol=ticker))
        except Exception as exc:
            res.note_gap("company", f"obb.equity.profile failed: {exc}")
            return None
        if not rows:
            return None
        p = rows[0]
        return {
            "ticker": ticker,
            "name": p.get("name") or p.get("long_name"),
            "exchange": p.get("exchange") or p.get("exchange_name"),
            "currency": p.get("currency"),
            "unit_scale": 1.0,
            "unit_label": p.get("currency"),
            "sector": p.get("sector"),
            "industry": p.get("industry"),
            "shares_outstanding": p.get("shares_outstanding"),
            "source": "obb.equity.profile",
            "as_of": res.as_of,
        }

    def _statements(self, obb: Any, ticker: str, res: ProviderResult, limit: int) -> None:
        calls = (
            ("income", obb.equity.fundamental.income, _INCOME_MAP),
            ("balance", obb.equity.fundamental.balance, _BALANCE_MAP),
            ("cashflow", obb.equity.fundamental.cash, _CASHFLOW_MAP),
        )
        for domain, fn, mapping in calls:
            try:
                rows = _to_records(fn(symbol=ticker, period="annual", limit=limit))
            except Exception as exc:
                res.note_gap(domain, f"obb.equity.fundamental.{domain} failed: {exc}")
                continue
            staged = [
                _stage_row(ticker, r, mapping, f"obb.equity.fundamental.{domain}", res.as_of)
                for r in rows
            ]
            setattr(res, domain, [r for r in staged if r])

    def _prices(self, obb: Any, ticker: str, res: ProviderResult, config: Any) -> None:
        years = max(1, int(getattr(config, "price_years", 10)))
        try:
            rows = _to_records(
                obb.equity.price.historical(symbol=ticker, interval="1d", start_date=None)
            )
        except Exception as exc:
            res.note_gap("price", f"obb.equity.price.historical failed: {exc}")
            return
        res.prices = [
            {
                "ticker": ticker,
                "date": str(r.get("date"))[:10],
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "adj_close": r.get("adj_close", r.get("close")),
                "volume": r.get("volume"),
                "source": "obb.equity.price.historical",
                "as_of": res.as_of,
            }
            for r in rows
            if r.get("close") is not None
        ][-(years * 260):]

    def _news(self, obb: Any, ticker: str, res: ProviderResult) -> None:
        try:
            rows = _to_records(obb.news.company(symbol=ticker, limit=50))
        except Exception as exc:
            res.note_gap("news", f"obb.news.company failed: {exc}")
            return
        res.news = [
            {
                "ticker": ticker,
                "published_at": str(r.get("date"))[:19],
                "headline": r.get("title"),
                "url": r.get("url"),
                "publisher": r.get("provider") or r.get("source"),
                "source": "obb.news.company",
                "as_of": res.as_of,
            }
            for r in rows
            if r.get("title")
        ]


# -- OpenBB field mappings -------------------------------------------------

_INCOME_MAP = {
    "revenue": "sales",
    "cost_of_revenue": "cogs",
    "ebitda": "ebitda",
    "depreciation_and_amortization": "depreciation",
    "operating_income": "ebit",
    "interest_expense": "interest",
    "income_before_tax": "pbt",
    "income_tax_expense": "tax",
    "consolidated_net_income": "pat",
    "net_income": "pat",
    "diluted_earnings_per_share": "eps",
    "total_other_income_expenses_net": "other_income",
}
_BALANCE_MAP = {
    "total_equity": "net_worth",
    "total_shareholders_equity": "net_worth",
    "common_stock": "equity_capital",
    "retained_earnings": "reserves",
    "total_debt": "borrowings",
    "total_current_liabilities": "current_liabilities",
    "total_liabilities": "total_liabilities",
    "plant_property_equipment_gross": "gross_block",
    "plant_property_equipment_net": "net_block",
    "long_term_investments": "investments",
    "goodwill": "goodwill",
    "intangible_assets": "intangibles",
    "accounts_receivable": "receivables",
    "inventory": "inventory",
    "cash_and_short_term_investments": "cash",
    "total_assets": "total_assets",
}
_CASHFLOW_MAP = {
    "net_cash_flow_from_operating_activities": "cfo",
    "net_cash_flow_from_investing_activities": "cfi",
    "net_cash_flow_from_financing_activities": "cff",
    "purchase_of_property_plant_and_equipment": "capex",
    "capital_expenditure": "capex",
    "payment_of_dividends": "dividends_paid",
    "net_change_in_cash": "net_cash_flow",
}


def _to_records(obb_result: Any) -> List[Dict[str, Any]]:
    """Normalise an OBBject into plain dicts across OpenBB versions."""
    for attr in ("to_dict",):
        fn = getattr(obb_result, attr, None)
        if callable(fn):
            try:
                out = fn(orient="records")
                if isinstance(out, list):
                    return out
            except TypeError:
                pass
    results = getattr(obb_result, "results", obb_result)
    if isinstance(results, dict):
        results = [results]
    records = []
    for item in results or []:
        if isinstance(item, dict):
            records.append(item)
        elif hasattr(item, "model_dump"):
            records.append(item.model_dump())
        elif hasattr(item, "dict"):
            records.append(item.dict())
    return records


def _stage_row(
    ticker: str, raw: Dict[str, Any], mapping: Dict[str, str], source: str, as_of: str
) -> Optional[Dict[str, Any]]:
    picked = {
        col: raw[key]
        for key, col in mapping.items()
        if raw.get(key) is not None
    }
    if not picked:
        return None
    period_end = str(raw.get("period_ending") or raw.get("date") or "")[:10]
    if not period_end:
        return None
    from .yahoo import _fiscal_label

    if picked.get("capex") is not None:
        picked["capex"] = abs(picked["capex"])
    if picked.get("dividends_paid") is not None:
        picked["dividends_paid"] = abs(picked["dividends_paid"])
    if picked.get("interest") is not None:
        picked["interest"] = abs(picked["interest"])
    return {
        "ticker": ticker,
        "period_label": _fiscal_label(period_end),
        "period_end": period_end,
        "period_type": "annual",
        "source": source,
        "as_of": as_of,
        **picked,
    }
