"""SEC EDGAR provider -- filing-tier US financials, straight from XBRL.

This is the highest-quality source the engine has for US companies: the numbers
come from the 10-K itself rather than from a vendor's re-keying of it, which is
why it carries ``source_tier = "filing"`` (weight 1.00 in the §8 score) and why
it sits ahead of Yahoo in the chain.

Two XBRL shapes have to be handled differently:

* **Duration facts** (revenue, profit, cash flow) carry ``start`` and ``end``.
* **Instant facts** (assets, equity, cash) carry only ``end``.

Facts also *repeat*: the same fiscal year is restated in later filings. Taking
the first match would pin a superseded figure, so every period keeps the value
from the most recently **filed** document.

SEC requires a descriptive User-Agent and asks for <= 10 requests/second.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Tuple

from .base import FetchError, Provider, ProviderResult, http_get_json

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

SEC_HEADERS = {
    # SEC's fair-access policy requires an identifying User-Agent.
    "User-Agent": "equity-analyst/0.1 (open-source research tool)",
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov",
}

#: Our column <- the first us-gaap concept that is present, in priority order.
#: Companies tag the same economics differently, so each column lists the
#: alternatives rather than assuming one canonical concept.
INCOME_CONCEPTS: List[Tuple[str, List[str]]] = [
    ("sales", ["RevenueFromContractWithCustomerExcludingAssessedTax",
               "RevenueFromContractWithCustomerIncludingAssessedTax",
               "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"]),
    ("cogs", ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"]),
    ("ebit", ["OperatingIncomeLoss"]),
    ("depreciation", ["DepreciationDepletionAndAmortization",
                      "DepreciationAmortizationAndAccretionNet", "Depreciation"]),
    ("interest", ["InterestExpense", "InterestIncomeExpenseNet",
                  "InterestExpenseNonoperating"]),
    ("pbt", ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    ("tax", ["IncomeTaxExpenseBenefit"]),
    ("pat", ["NetIncomeLoss", "ProfitLoss"]),
    ("eps", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]),
    ("dividend_per_share", ["CommonStockDividendsPerShareDeclared"]),
]

BALANCE_CONCEPTS: List[Tuple[str, List[str]]] = [
    ("total_assets", ["Assets"]),
    ("total_liabilities", ["Liabilities"]),
    ("net_worth", ["StockholdersEquity",
                   "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    ("reserves", ["RetainedEarningsAccumulatedDeficit"]),
    ("equity_capital", ["CommonStockValue"]),
    ("cash", ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
              "CashAndCashEquivalentsAtCarryingValue"]),
    ("investments", ["MarketableSecuritiesCurrent", "LongTermInvestments",
                     "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent"]),
    ("receivables", ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"]),
    ("inventory", ["InventoryNet"]),
    ("gross_block", ["PropertyPlantAndEquipmentGross"]),
    ("net_block", ["PropertyPlantAndEquipmentNet"]),
    ("accumulated_dep", ["AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment"]),
    ("goodwill", ["Goodwill"]),
    ("intangibles", ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"]),
    ("current_liabilities", ["LiabilitiesCurrent"]),
    ("borrowings", ["LongTermDebtNoncurrent", "LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"]),
]

CASHFLOW_CONCEPTS: List[Tuple[str, List[str]]] = [
    ("cfo", ["NetCashProvidedByUsedInOperatingActivities",
             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    ("cfi", ["NetCashProvidedByUsedInInvestingActivities"]),
    ("cff", ["NetCashProvidedByUsedInFinancingActivities"]),
    ("capex", ["PaymentsToAcquirePropertyPlantAndEquipment",
               "PaymentsToAcquireProductiveAssets"]),
    ("dividends_paid", ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"]),
]

INSTANT_COLUMNS = {c for c, _ in BALANCE_CONCEPTS}


class SECProvider(Provider):
    name = "sec-edgar"
    source_tier = "filing"

    def supports(self, market: str) -> bool:
        return (market or "").strip().upper() in {"US", "USA", ""}

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        res = ProviderResult(provider=self.name, source_tier=self.source_tier)
        cik = getattr(config, "cik", None) or lookup_cik(ticker)
        if not cik:
            for domain in ("income", "balance", "cashflow", "company"):
                res.note_gap(domain, f"no SEC CIK found for {ticker}")
            return res
        try:
            facts = http_get_json(FACTS_URL.format(cik=int(cik)), headers=SEC_HEADERS)
        except FetchError as exc:
            for domain in ("income", "balance", "cashflow"):
                res.note_gap(domain, f"SEC companyfacts unreachable: {exc}")
            return res

        gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        if not gaap:
            for domain in ("income", "balance", "cashflow"):
                res.note_gap(domain, "filing carries no us-gaap facts (foreign issuer?)")
            return res

        res.company.update({
            "ticker": ticker,
            "name": facts.get("entityName"),
            "market": "US",
            "currency": "USD",
            "unit_scale": 1.0,
            "unit_label": "USD",
            "source": self.name,
            "as_of": res.as_of,
        })

        income = _assemble(gaap, INCOME_CONCEPTS, ticker, self.name, res.as_of)
        balance = _assemble(gaap, BALANCE_CONCEPTS, ticker, self.name, res.as_of)
        cashflow = _assemble(gaap, CASHFLOW_CONCEPTS, ticker, self.name, res.as_of)

        res.income = income
        res.balance = balance
        res.cashflow = cashflow
        for domain, rows in (("income", income), ("balance", balance), ("cashflow", cashflow)):
            if not rows:
                res.note_gap(domain, "no annual periods in the XBRL facts")

        shares = _latest_shares(facts)
        if shares:
            res.company["shares_outstanding"] = shares

        _post_process(res, shares)
        # SEC files statements, not quotes; the price series comes from a
        # market-data provider further down the chain.
        res.note_gap("price", "SEC serves filings, not market prices")
        return res


# -- fact extraction -------------------------------------------------------


def _annual_values(unit_rows: List[Dict[str, Any]], instant: bool) -> Dict[str, Tuple[str, float]]:
    """``{period_end: (period_start, value)}`` from annual-report facts.

    Keeps the most recently *filed* value for each period so a restatement
    supersedes the original rather than colliding with it.
    """
    picked: Dict[str, Tuple[str, float, str]] = {}
    for row in unit_rows or []:
        if row.get("form") not in ("10-K", "10-K/A", "20-F", "40-F"):
            continue
        end = row.get("end")
        val = row.get("val")
        if not end or val is None:
            continue
        start = row.get("start")
        if instant:
            if start:            # an instant fact should carry no duration
                continue
        else:
            if not start:
                continue
            # Guard against quarterly rows that slipped into a 10-K: only keep
            # spans of roughly a year.
            try:
                days = (_dt.date.fromisoformat(end) - _dt.date.fromisoformat(start)).days
            except ValueError:
                continue
            if not (300 <= days <= 430):
                continue
        filed = row.get("filed") or ""
        prior = picked.get(end)
        if prior is None or filed >= prior[2]:
            picked[end] = (start or "", float(val), filed)
    return {end: (start, val) for end, (start, val, _) in picked.items()}


def _assemble(
    gaap: Dict[str, Any],
    concepts: List[Tuple[str, List[str]]],
    ticker: str,
    source: str,
    as_of: str,
) -> List[Dict[str, Any]]:
    columns: Dict[str, Dict[str, Any]] = {}
    for column, candidates in concepts:
        instant = column in INSTANT_COLUMNS
        for concept in candidates:
            block = gaap.get(concept)
            if not block:
                continue
            usd = (block.get("units") or {}).get("USD") or (block.get("units") or {}).get("USD/shares")
            if not usd:
                continue
            values = _annual_values(usd, instant)
            if not values:
                continue
            for end, (_, val) in values.items():
                columns.setdefault(end, {})[column] = val
            break                # first concept that yields data wins

    from .yahoo import _fiscal_label

    rows = []
    for end in sorted(columns):
        rows.append({
            "ticker": ticker,
            "period_label": _fiscal_label(end),
            "period_end": end,
            "period_type": "annual",
            "source": source,
            "as_of": as_of,
            **columns[end],
        })
    # Keep at most the last 12 fiscal years; older XBRL is patchy.
    return rows[-12:]


def _latest_shares(facts: Dict[str, Any]) -> Optional[float]:
    dei = (facts.get("facts") or {}).get("dei") or {}
    block = dei.get("EntityCommonStockSharesOutstanding")
    if not block:
        return None
    best: Tuple[str, float] = ("", 0.0)
    for rows in (block.get("units") or {}).values():
        for row in rows:
            end, val = row.get("end"), row.get("val")
            if end and val and end > best[0]:
                best = (end, float(val))
    return best[1] or None


def _post_process(res: ProviderResult, shares: Optional[float]) -> None:
    """Derive what XBRL does not tag directly, and fix signs."""
    for row in res.cashflow:
        for key in ("capex", "dividends_paid"):
            if row.get(key) is not None:
                row[key] = abs(row[key])

    for row in res.income:
        if row.get("interest") is not None:
            row["interest"] = abs(row["interest"])
        if row.get("sales") is not None and row.get("cogs") is not None:
            row.setdefault("expenses", row["sales"] - (row.get("ebit") or 0.0))
        if row.get("tax") is not None and row.get("pbt"):
            try:
                row["tax_rate"] = max(0.0, min(0.60, row["tax"] / row["pbt"]))
            except ZeroDivisionError:
                pass
        # EBITDA is not an XBRL concept; build it from operating income only
        # when depreciation is actually reported, never by guessing.
        if row.get("ebit") is not None and row.get("depreciation") is not None:
            row["ebitda"] = row["ebit"] + row["depreciation"]
        if row.get("pat") and row.get("eps") in (None, 0) and shares:
            row["eps"] = row["pat"] / shares
        if row.get("dividend_per_share") is not None and row.get("eps"):
            try:
                row["dividend_payout"] = row["dividend_per_share"] / row["eps"] * 100.0
            except ZeroDivisionError:
                pass

    for row in res.balance:
        if row.get("accumulated_dep") is not None:
            row["accumulated_dep"] = abs(row["accumulated_dep"])
        if row.get("net_worth") is None and row.get("total_assets") is not None:
            liabilities = row.get("total_liabilities")
            if liabilities is not None:
                row["net_worth"] = row["total_assets"] - liabilities


# -- ticker directory ------------------------------------------------------


def fetch_ticker_directory() -> List[Dict[str, Any]]:
    """The full SEC ticker <-> company-name universe (~10k US registrants)."""
    payload = http_get_json(
        TICKERS_URL,
        headers={**SEC_HEADERS, "Host": "www.sec.gov"},
    )
    rows = payload.values() if isinstance(payload, dict) else payload
    out = []
    for row in rows:
        ticker, title, cik = row.get("ticker"), row.get("title"), row.get("cik_str")
        if not ticker or not title:
            continue
        out.append({
            "ticker": ticker.upper(),
            "name": title,
            "market": "US",
            "exchange": "US",
            "cik": str(cik).zfill(10) if cik else None,
            "source": "sec",
        })
    return out


_CIK_CACHE: Dict[str, str] = {}


def lookup_cik(ticker: str) -> Optional[str]:
    """CIK for a US ticker, memoised for the process."""
    key = ticker.upper().split(".")[0]
    if not _CIK_CACHE:
        try:
            for row in fetch_ticker_directory():
                _CIK_CACHE.setdefault(row["ticker"], row["cik"])
        except Exception:
            return None
    return _CIK_CACHE.get(key)
