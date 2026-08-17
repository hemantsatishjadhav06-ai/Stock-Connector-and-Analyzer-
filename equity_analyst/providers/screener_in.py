"""Screener.in provider -- the primary screener for Indian listings.

Screener reports in **INR crore** with fiscal years labelled ``Mar 2024``. We
keep those labels (schema rule) and set ``unit_scale = 1e7`` so a downstream
consumer can recover base rupees.

Share count is derived, not scraped: Screener publishes equity capital (crore)
and face value (rupees), and ``shares = equity_capital / face_value`` yields the
count *in crore*, which is exactly the schema invariant (shares in the same
scale as the money). Cross-check: market cap / shares must reproduce the quoted
price, and the loader asserts that within tolerance.

Screener carries no price history on the company page, so a run for an Indian
ticker merges this provider with Yahoo (``TICKER.NS``) for the OHLCV series.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Dict, List, Optional, Tuple

from .base import FetchError, Provider, ProviderResult, http_get

BASE = "https://www.screener.in/company/{sym}/"
CONSOLIDATED = "https://www.screener.in/company/{sym}/consolidated/"

INCOME_MAP = {
    "sales": "sales",
    "expenses": "expenses",
    "operating profit": "ebitda",
    "other income": "other_income",
    "interest": "interest",
    "depreciation": "depreciation",
    "profit before tax": "pbt",
    "net profit": "pat",
    "eps in rs": "eps",
    "dividend payout %": "dividend_payout",
    "tax %": "_tax_pct",
    "opm %": "_opm_pct",
}
BALANCE_MAP = {
    "equity capital": "equity_capital",
    "reserves": "reserves",
    "borrowings": "borrowings",
    "other liabilities": "other_liabilities",
    "total liabilities": "total_liabilities",
    "fixed assets": "net_block",
    "cwip": "cwip",
    "investments": "investments",
    "other assets": "other_assets",
    "total assets": "total_assets",
}
CASHFLOW_MAP = {
    "cash from operating activity": "cfo",
    "cash from investing activity": "cfi",
    "cash from financing activity": "cff",
    "net cash flow": "net_cash_flow",
    "free cash flow": "_fcf",
}


class ScreenerInProvider(Provider):
    name = "screener.in"
    source_tier = "screener"

    def supports(self, market: str) -> bool:
        from ..config import market_is_india

        return market_is_india(market)

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        res = ProviderResult(provider=self.name, source_tier=self.source_tier)
        symbol = _screener_symbol(ticker)
        page = self._load_page(symbol, res)
        if page is None:
            return res

        res.company.update(
            {
                "ticker": ticker,
                "name": _extract_name(page) or symbol,
                "market": "India",
                "exchange": "NSE/BSE",
                "currency": "INR",
                "unit_scale": 1e7,
                "unit_label": "INR crore",
                "source": self.name,
                "as_of": res.as_of,
            }
        )

        top = _parse_top_ratios(page)
        face_value = top.get("Face Value")
        price = top.get("Current Price")
        market_cap = top.get("Market Cap")
        if price is not None:
            res.snapshot.update(
                {
                    "ticker": ticker,
                    "as_of": res.as_of,
                    "price": price,
                    "market_cap": market_cap,
                    "currency": "INR",
                    "source": self.name,
                }
            )
        res.company["face_value"] = face_value

        sections = {
            "profit-loss": (INCOME_MAP, "income"),
            "balance-sheet": (BALANCE_MAP, "balance"),
            "cash-flow": (CASHFLOW_MAP, "cashflow"),
        }
        parsed: Dict[str, List[Dict[str, Any]]] = {}
        for sid, (mapping, domain) in sections.items():
            rows = _parse_statement(page, sid, mapping, ticker, self.name, res.as_of)
            parsed[domain] = rows
            if not rows:
                res.note_gap(domain, f"section '{sid}' missing or unparsable")

        res.income = parsed.get("income", [])
        res.balance = parsed.get("balance", [])
        res.cashflow = parsed.get("cashflow", [])

        shares = _derive_shares_crore(res.balance, face_value, market_cap, price)
        if shares:
            res.company["shares_outstanding"] = shares
        else:
            res.note_gap("shares", "could not derive share count from equity capital")

        self._post_process(res, shares)

        res.shareholding = _parse_shareholding(page, ticker, self.name, res.as_of)
        if not res.shareholding:
            res.note_gap("shareholding", "shareholding section unavailable")

        for label, value in top.items():
            res.ratios.append(
                {
                    "ticker": ticker,
                    "period_label": "current",
                    "metric": label,
                    "value": value,
                    "source": self.name,
                    "as_of": res.as_of,
                }
            )
        return res

    def _load_page(self, symbol: str, res: ProviderResult) -> Optional[str]:
        # Consolidated first: it is the economically correct view for a group.
        for url in (CONSOLIDATED.format(sym=symbol), BASE.format(sym=symbol)):
            try:
                page = http_get(url).decode("utf-8", "replace")
            except FetchError:
                continue
            if 'id="profit-loss"' in page:
                return page
        for dom in ("income", "balance", "cashflow", "snapshot", "company"):
            res.note_gap(dom, f"screener.in page unreachable for {symbol}")
        return None

    def _post_process(self, res: ProviderResult, shares: Optional[float]) -> None:
        for row in res.income:
            opm = row.pop("_opm_pct", None)
            tax_pct = row.pop("_tax_pct", None)
            if tax_pct is not None:
                row["tax_rate"] = tax_pct / 100.0
            # Screener's "Operating Profit" is EBITDA (sales less expenses).
            if row.get("ebitda") is not None and row.get("depreciation") is not None:
                row["ebit"] = row["ebitda"] - row["depreciation"]
            if row.get("pbt") is not None and row.get("tax_rate") is not None:
                row["tax"] = row["pbt"] * row["tax_rate"]
                if row.get("pat") is None:
                    row["pat"] = row["pbt"] - row["tax"]
            if (
                row.get("dividend_payout") is not None
                and row.get("eps") is not None
            ):
                row["dividend_per_share"] = row["eps"] * row["dividend_payout"] / 100.0
            if opm is not None and row.get("ebit") is None and row.get("sales"):
                row["ebit"] = row["sales"] * opm / 100.0

        for row in res.balance:
            eq, rs = row.get("equity_capital"), row.get("reserves")
            if eq is not None or rs is not None:
                row["net_worth"] = (eq or 0.0) + (rs or 0.0)

        fcf_by_period = {}
        for row in res.cashflow:
            fcf = row.pop("_fcf", None)
            if fcf is not None:
                fcf_by_period[row["period_label"]] = fcf
            # Screener publishes FCF but not capex; capex = CFO - FCF.
            if fcf is not None and row.get("cfo") is not None:
                row["capex"] = row["cfo"] - fcf

        div_ps = {r["period_label"]: r.get("dividend_per_share") for r in res.income}
        if shares:
            for row in res.cashflow:
                dps = div_ps.get(row["period_label"])
                if dps is not None and row.get("dividends_paid") is None:
                    row["dividends_paid"] = dps * shares


# -- parsing helpers -------------------------------------------------------


def _screener_symbol(ticker: str) -> str:
    """``RELIANCE.NS`` / ``RELIANCE.BO`` -> ``RELIANCE``."""
    return re.sub(r"\.(NS|BO|NSE|BSE)$", "", ticker.strip(), flags=re.I).upper()


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _to_number(text: str) -> Optional[float]:
    """Parse Screener cells: ``1,234``, ``-5.6``, ``12%``, ``₹ 1,310``, ``''``."""
    if text is None:
        return None
    cleaned = _html.unescape(text)
    cleaned = re.sub(r"[₹,%\s]", "", cleaned).replace("Cr.", "").replace(",", "")
    cleaned = cleaned.replace("₹", "").strip()
    if cleaned in ("", "-", "--"):
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _section(page: str, section_id: str) -> Optional[str]:
    match = re.search(
        r'<section[^>]*id="%s".*?(?=<section|\Z)' % re.escape(section_id), page, re.S
    )
    return match.group(0) if match else None


def _first_data_table(fragment: str) -> Optional[str]:
    match = re.search(r'<table class="data-table.*?</table>', fragment, re.S)
    return match.group(0) if match else None


def _table_periods(table: str) -> List[Tuple[str, str]]:
    """Header cells -> ``[(period_label, period_end_iso_or_label)]``.

    Statement tables carry ``data-date-key``; the shareholding table does not
    and only labels its columns ``Mar 2024``. Fall back to the header text so
    both shapes parse, and let the caller decide what the second element means.
    """
    head = re.search(r"<thead>.*?</thead>", table, re.S)
    if not head:
        return []
    periods: List[Tuple[str, str]] = []
    for th in re.findall(r"<th[^>]*>.*?</th>", head.group(0), re.S):
        label = _strip_tags(th)
        key = re.search(r'data-date-key="([^"]+)"', th)
        if key:
            periods.append((label or key.group(1), key.group(1)))
        elif label:
            periods.append((label, label))
    return periods


def _table_rows(table: str) -> List[Tuple[str, List[str]]]:
    body = re.search(r"<tbody>.*?</tbody>", table, re.S)
    if not body:
        return []
    out: List[Tuple[str, List[str]]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(0), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not cells:
            continue
        label = _strip_tags(cells[0]).rstrip("+").strip().lower()
        out.append((label, [_strip_tags(c) for c in cells[1:]]))
    return out


def _parse_statement(
    page: str,
    section_id: str,
    mapping: Dict[str, str],
    ticker: str,
    source: str,
    as_of: str,
) -> List[Dict[str, Any]]:
    section = _section(page, section_id)
    if not section:
        return []
    table = _first_data_table(section)
    if not table:
        return []
    periods = _table_periods(table)
    if not periods:
        return []

    # Screener appends a trailing-twelve-month column whose header carries no
    # parseable date. It is NOT a fiscal year: loading it as one would give the
    # latest "year" a P&L with no balance sheet behind it and would skew every
    # CAGR window by an extra period. Classify it as 'ttm' so v_annual ignores it.
    columns: Dict[int, Dict[str, Any]] = {}
    for i, (_, end) in enumerate(periods):
        is_fiscal = _is_iso_date(end)
        columns[i] = {
            "ticker": ticker,
            "period_label": _fiscal_label(end) if is_fiscal else end.strip().upper(),
            "period_end": end if is_fiscal else None,
            "period_type": "annual" if is_fiscal else "ttm",
            "source": source,
            "as_of": as_of,
        }
    for label, values in _table_rows(table):
        column = mapping.get(label)
        if column is None:
            continue
        for i, raw in enumerate(values):
            if i in columns:
                number = _to_number(raw)
                if number is not None:
                    columns[i][column] = number

    payload_keys = set(mapping.values())
    return [
        row
        for row in columns.values()
        if payload_keys & set(row)  # drop period columns with no data at all
    ]


def _is_iso_date(text: str) -> bool:
    import datetime as dt

    try:
        dt.date.fromisoformat((text or "").strip())
    except (ValueError, TypeError):
        return False
    return True


def _fiscal_label(iso_date: str) -> str:
    import datetime as dt

    try:
        d = dt.date.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    return f"{d.strftime('%b')}-{d.strftime('%y')}"


def _extract_name(page: str) -> Optional[str]:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    return _strip_tags(match.group(1)) if match else None


def _parse_top_ratios(page: str) -> Dict[str, float]:
    """The ratio strip: Market Cap, Current Price, Stock P/E, ROCE, ..."""
    block = re.search(r'<ul[^>]*id="top-ratios".*?</ul>', page, re.S)
    if not block:
        return {}
    out: Dict[str, float] = {}
    for li in re.findall(r"<li[^>]*>.*?</li>", block.group(0), re.S):
        name = re.search(r'class="name"[^>]*>(.*?)<', li, re.S)
        value = re.search(r'class="(?:nowrap )?value"[^>]*>(.*?)</span>', li, re.S)
        if not name:
            continue
        label = _strip_tags(name.group(1)).strip()
        # Read the whole <li>, not the truncated first value span: Screener
        # nests <span class="number"> inside the value, so a non-greedy match
        # stops early and would return "1,612" for "₹ 1,612 / 1,250".
        text = _strip_tags(li)
        if label:
            text = text[len(label):].strip() if text.startswith(label) else text
        # A "x / y" pair (High / Low) cannot become one float without silently
        # meaning "high". Skip it rather than half-parse. Note the test is on
        # the *value*, so labels containing a slash ("Stock P/E") still load.
        if re.search(r"\d\s*/\s*[₹\s]*\d", text):
            continue
        number = _to_number(text)
        if number is not None:
            out[label] = number
    return out


def _derive_shares_crore(
    balance: List[Dict[str, Any]],
    face_value: Optional[float],
    market_cap: Optional[float],
    price: Optional[float],
) -> Optional[float]:
    """Shares outstanding *in crore*, cross-checked against market cap."""
    from_equity: Optional[float] = None
    if face_value:
        latest = [r for r in balance if r.get("equity_capital") is not None]
        if latest:
            latest.sort(key=lambda r: r.get("period_end") or "")
            from_equity = latest[-1]["equity_capital"] / face_value

    from_cap: Optional[float] = None
    if market_cap and price:
        from_cap = market_cap / price

    if from_equity and from_cap:
        # Buybacks/ESOPs between the last balance sheet and today make a small
        # divergence normal; a large one means the equity-capital route is
        # wrong (e.g. multiple share classes), so trust the market-cap route.
        if abs(from_equity - from_cap) / from_cap <= 0.05:
            return from_equity
        return from_cap
    return from_equity or from_cap


def _parse_shareholding(
    page: str, ticker: str, source: str, as_of: str
) -> List[Dict[str, Any]]:
    section = _section(page, "shareholding")
    if not section:
        return []
    table = _first_data_table(section)
    if not table:
        return []
    periods = _table_periods(table)
    if not periods:
        return []

    label_map = {
        "promoters": "promoter_pct",
        "fiis": "fii_pct",
        "diis": "dii_pct",
        "government": "government_pct",
        "public": "public_pct",
    }
    columns: Dict[int, Dict[str, Any]] = {
        i: {
            "ticker": ticker,
            "period_label": label,
            "source": source,
            "as_of": as_of,
        }
        for i, (label, _) in enumerate(periods)
    }
    for label, values in _table_rows(table):
        key = label_map.get(label.rstrip("+").strip())
        if key is None:
            continue
        for i, raw in enumerate(values):
            if i in columns:
                number = _to_number(raw)
                if number is not None:
                    columns[i][key] = number
    return [row for row in columns.values() if len(row) > 4]
