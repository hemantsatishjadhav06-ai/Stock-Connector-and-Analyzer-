"""Yahoo Finance provider -- global coverage, no API key, stdlib only.

Two public endpoints are used:

``/v8/finance/chart/{symbol}``
    OHLCV history plus a ``meta`` block carrying live price, currency and
    exchange. Also used for commodity futures (CL=F, HG=F, ...).

``/ws/fundamentals-timeseries/v1/finance/timeseries/{symbol}``
    Annual P&L / balance sheet / cash flow line items, keyed by fiscal period
    end date. Values arrive in absolute reporting-currency units, so
    ``unit_scale`` is 1.0 and shares are stored absolute (schema invariant).

Both are best-effort. A 429 from the shared egress IP degrades the run into a
coverage gap, which lowers the §8 verification score -- it never invents data.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from .base import FetchError, Provider, ProviderResult, http_get_json

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
TIMESERIES_URL = (
    "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/"
    "timeseries/{sym}"
)

# Yahoo line item -> our column. Prefixed with 'annual' when requested.
INCOME_FIELDS = {
    "TotalRevenue": "sales",
    "CostOfRevenue": "cogs",
    "EBITDA": "ebitda",
    "ReconciledDepreciation": "depreciation",
    "EBIT": "ebit",
    "InterestExpense": "interest",
    "PretaxIncome": "pbt",
    "TaxProvision": "tax",
    "NetIncome": "pat",
    "DilutedEPS": "eps",
    "OtherIncomeExpense": "other_income",
}
BALANCE_FIELDS = {
    "StockholdersEquity": "net_worth",
    "CommonStock": "equity_capital",
    "RetainedEarnings": "reserves",
    "TotalDebt": "borrowings",
    "CurrentLiabilities": "current_liabilities",
    "TotalLiabilitiesNetMinorityInterest": "total_liabilities",
    "GrossPPE": "gross_block",
    "AccumulatedDepreciation": "accumulated_dep",
    "NetPPE": "net_block",
    "ConstructionInProgress": "cwip",
    "InvestmentinFinancialAssets": "investments",
    "GoodwillAndOtherIntangibleAssets": "intangibles",
    "Goodwill": "goodwill",
    "AccountsReceivable": "receivables",
    "Inventory": "inventory",
    "CashCashEquivalentsAndShortTermInvestments": "cash",
    "TotalAssets": "total_assets",
    "OrdinarySharesNumber": "_shares",
}
CASHFLOW_FIELDS = {
    "OperatingCashFlow": "cfo",
    "InvestingCashFlow": "cfi",
    "FinancingCashFlow": "cff",
    "CapitalExpenditure": "capex",
    "CashDividendsPaid": "dividends_paid",
    "ChangesInCash": "net_cash_flow",
}


class YahooProvider(Provider):
    name = "yahoo"
    source_tier = "vendor"

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        res = ProviderResult(provider=self.name, source_tier=self.source_tier)
        self._fetch_prices_and_meta(ticker, config, res)
        self._fetch_fundamentals(ticker, res)
        self._post_process(res)
        return res

    # -- prices ----------------------------------------------------------
    def _fetch_prices_and_meta(
        self, ticker: str, config: Any, res: ProviderResult
    ) -> None:
        years = max(1, int(getattr(config, "price_years", 10)))
        url = f"{CHART_URL.format(sym=ticker)}?range={years}y&interval=1d"
        try:
            payload = http_get_json(url)
        except FetchError as exc:
            res.note_gap("price", str(exc))
            res.note_gap("snapshot", str(exc))
            return

        results = (payload.get("chart") or {}).get("result") or []
        if not results:
            err = ((payload.get("chart") or {}).get("error")) or "empty chart result"
            res.note_gap("price", f"no chart data: {err}")
            return

        r = results[0]
        meta = r.get("meta") or {}
        res.company.update(
            {
                "ticker": ticker,
                "name": meta.get("longName") or meta.get("shortName"),
                "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
                "currency": meta.get("currency"),
                "unit_scale": 1.0,
                "unit_label": meta.get("currency"),
                "source": self.name,
                "as_of": res.as_of,
            }
        )
        price = meta.get("regularMarketPrice")
        if price is not None:
            res.snapshot.update(
                {
                    "ticker": ticker,
                    "as_of": res.as_of,
                    "price": float(price),
                    "currency": meta.get("currency"),
                    "source": self.name,
                }
            )

        res.prices.extend(_parse_chart_rows(ticker, r, self.name, res.as_of))
        if not res.prices:
            res.note_gap("price", "chart returned no usable candles")

    # -- fundamentals ----------------------------------------------------
    def _fetch_fundamentals(self, ticker: str, res: ProviderResult) -> None:
        wanted = (
            list(INCOME_FIELDS) + list(BALANCE_FIELDS) + list(CASHFLOW_FIELDS)
        )
        types = ",".join(f"annual{k}" for k in wanted)
        p2 = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
        p1 = p2 - int(15 * 365.25 * 86400)  # 15y window -> up to 10 usable years
        url = (
            f"{TIMESERIES_URL.format(sym=ticker)}?symbol={ticker}&type={types}"
            f"&period1={p1}&period2={p2}&merge=false"
        )
        try:
            payload = http_get_json(url)
        except FetchError as exc:
            for dom in ("income", "balance", "cashflow"):
                res.note_gap(dom, str(exc))
            return

        series = _index_timeseries(payload)
        if not series:
            for dom in ("income", "balance", "cashflow"):
                res.note_gap(dom, "fundamentals-timeseries returned no series")
            return

        res.income = _assemble(ticker, series, INCOME_FIELDS, self.name, res.as_of)
        res.balance = _assemble(ticker, series, BALANCE_FIELDS, self.name, res.as_of)
        res.cashflow = _assemble(ticker, series, CASHFLOW_FIELDS, self.name, res.as_of)
        for dom, rows in (
            ("income", res.income), ("balance", res.balance), ("cashflow", res.cashflow)
        ):
            if not rows:
                res.note_gap(dom, "no annual periods returned")

    # -- normalisation ---------------------------------------------------
    def _post_process(self, res: ProviderResult) -> None:
        """Apply sign conventions and derive fields Yahoo does not report."""
        for row in res.cashflow:
            # Yahoo reports capex as a negative outflow; the schema wants it
            # positive so `fcf = cfo - capex` reads the way the formula does.
            if row.get("capex") is not None:
                row["capex"] = abs(row["capex"])
            if row.get("dividends_paid") is not None:
                row["dividends_paid"] = abs(row["dividends_paid"])

        shares_by_period: Dict[str, float] = {}
        for row in res.balance:
            if row.get("interest") is not None:
                row["interest"] = abs(row["interest"])
            shares = row.pop("_shares", None)
            if shares:
                shares_by_period[row["period_label"]] = float(shares)
            # Yahoo's accumulated depreciation is negative; store magnitude.
            if row.get("accumulated_dep") is not None:
                row["accumulated_dep"] = abs(row["accumulated_dep"])
            # Reported goodwill is included in the intangibles aggregate.
            if row.get("net_worth") is None and row.get("total_assets") is not None:
                tl = row.get("total_liabilities")
                if tl is not None:
                    row["net_worth"] = row["total_assets"] - tl

        for row in res.income:
            if row.get("interest") is not None:
                row["interest"] = abs(row["interest"])
            if row.get("tax") is not None and row.get("pbt"):
                try:
                    row["tax_rate"] = max(0.0, min(0.60, row["tax"] / row["pbt"]))
                except ZeroDivisionError:
                    pass
            if row.get("ebit") is None and row.get("pbt") is not None:
                row["ebit"] = row["pbt"] + (row.get("interest") or 0.0)
            if (
                row.get("expenses") is None
                and row.get("sales") is not None
                and row.get("ebit") is not None
            ):
                row["expenses"] = row["sales"] - row["ebit"]

        # Latest reported share count -> company master (absolute units).
        if shares_by_period:
            latest = sorted(shares_by_period)[-1]
            res.company.setdefault("shares_outstanding", shares_by_period[latest])

        price = (res.snapshot or {}).get("price")
        shares = res.company.get("shares_outstanding")
        if price and shares and res.snapshot.get("market_cap") is None:
            res.snapshot["market_cap"] = float(price) * float(shares)

        # Per-share dividend, needed by Buffetology's cumulative-dividend leg.
        div_by_period = {
            r["period_label"]: r.get("dividends_paid") for r in res.cashflow
        }
        for row in res.income:
            paid = div_by_period.get(row["period_label"])
            if paid and shares:
                row.setdefault("dividend_per_share", float(paid) / float(shares))
            if paid and row.get("pat"):
                row.setdefault("dividend_payout", float(paid) / float(row["pat"]))


# -- module-level parsing helpers (kept pure so tests can hit them) --------


def _parse_chart_rows(
    ticker: str, result: Dict[str, Any], source: str, as_of: str
) -> List[Dict[str, Any]]:
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((result.get("indicators") or {}).get("adjclose") or [{}])
    adjclose = (adj[0] or {}).get("adjclose") if adj else None

    rows: List[Dict[str, Any]] = []
    for i, ts in enumerate(stamps):
        close = _at(quote.get("close"), i)
        if close is None:
            continue  # holidays / halted sessions come back as nulls
        rows.append(
            {
                "ticker": ticker,
                "date": _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
                .date()
                .isoformat(),
                "open": _at(quote.get("open"), i),
                "high": _at(quote.get("high"), i),
                "low": _at(quote.get("low"), i),
                "close": close,
                "adj_close": _at(adjclose, i) if adjclose else close,
                "volume": _at(quote.get("volume"), i),
                "source": source,
                "as_of": as_of,
            }
        )
    return rows


def _at(seq: Optional[List[Any]], i: int) -> Optional[float]:
    if not seq or i >= len(seq):
        return None
    v = seq[i]
    return None if v is None else float(v)


def _index_timeseries(payload: Any) -> Dict[str, Dict[str, float]]:
    """Return ``{period_end_iso: {YahooField: value}}``."""
    out: Dict[str, Dict[str, float]] = {}
    for entry in ((payload.get("timeseries") or {}).get("result") or []):
        for key, points in entry.items():
            if not key.startswith("annual") or not isinstance(points, list):
                continue
            field = key[len("annual"):]
            for point in points:
                if not isinstance(point, dict):
                    continue
                date = point.get("asOfDate")
                reported = point.get("reportedValue") or {}
                raw = reported.get("raw", point.get("raw"))
                if date is None or raw is None:
                    continue
                out.setdefault(date, {})[field] = float(raw)
    return out


def _assemble(
    ticker: str,
    series: Dict[str, Dict[str, float]],
    mapping: Dict[str, str],
    source: str,
    as_of: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for date in sorted(series):
        values = series[date]
        picked = {col: values[fld] for fld, col in mapping.items() if fld in values}
        if not picked:
            continue
        rows.append(
            {
                "ticker": ticker,
                "period_label": _fiscal_label(date),
                "period_end": date,
                "period_type": "annual",
                "source": source,
                "as_of": as_of,
                **picked,
            }
        )
    return rows


def _fiscal_label(iso_date: str) -> str:
    """``2024-03-31`` -> ``Mar-24`` -- preserve the reported fiscal close."""
    try:
        d = _dt.date.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    return f"{d.strftime('%b')}-{d.strftime('%y')}"


SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"


def fetch_news(ticker: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Recent company news headlines for the §5 linkage section."""
    from ..db import utc_now

    url = f"{SEARCH_URL}?q={ticker}&newsCount={limit}&quotesCount=0"
    payload = http_get_json(url)
    as_of = utc_now()
    out: List[Dict[str, Any]] = []
    for item in payload.get("news") or []:
        published = item.get("providerPublishTime")
        stamp = None
        if published:
            stamp = (
                _dt.datetime.fromtimestamp(published, _dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat()
            )
        out.append(
            {
                "ticker": ticker,
                "published_at": stamp,
                "headline": item.get("title"),
                "url": item.get("link"),
                "publisher": item.get("publisher"),
                "source": "yahoo.search",
                "as_of": as_of,
            }
        )
    return [r for r in out if r["headline"]]


def fetch_commodity_series(
    symbol: str, years: int = 10
) -> List[Dict[str, Any]]:
    """Daily closes for a commodity future, for the §5 linkage analysis."""
    from ..db import utc_now

    url = f"{CHART_URL.format(sym=symbol)}?range={years}y&interval=1wk"
    payload = http_get_json(url)
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise FetchError(f"no commodity series for {symbol}")
    r = results[0]
    meta = r.get("meta") or {}
    as_of = utc_now()
    return [
        {
            "symbol": symbol,
            "date": row["date"],
            "close": row["close"],
            "unit": meta.get("currency"),
            "currency": meta.get("currency"),
            "source": "yahoo",
            "as_of": as_of,
        }
        for row in _parse_chart_rows(symbol, r, "yahoo", as_of)
    ]
