"""Price-history providers that keep working when Yahoo blocks you.

Ported from `indian-stock-signal-ai`. All three are OHLCV-first: they exist to
keep the price series (and therefore technicals, signals and backtests) alive
when the primary source rate-limits a shared IP, which is the single most common
failure in hosted runs.

* :class:`TwelveDataProvider` — works from datacenter IPs where Yahoo 429s.
  Free tier ~800 calls/day. Set ``TWELVEDATA_API_KEY``.
* :class:`AlpacaProvider` — US equities and crypto. Set ``ALPACA_KEY_ID`` and
  ``ALPACA_SECRET_KEY``.
* :class:`IndianAPIProvider` — adapter for a deployed Indian-Stock-Market-API
  instance. Quote/fundamentals oriented. Set ``INDIAN_API_BASE_URL``.

Each returns a :class:`~equity_analyst.providers.base.ProviderResult` so it
merges into the same first-supplier-wins chain as everything else.
"""

from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
from typing import Any, Dict, List, Optional

from .base import FetchError, Provider, ProviderResult, http_get_json

TWELVEDATA_BASE = "https://api.twelvedata.com"

#: Index tickers Twelve Data names differently from Yahoo.
TD_INDEX_MAP = {"^NSEI": ("NIFTY 50", None), "^BSESN": ("SENSEX", None),
                "^GSPC": ("GSPC", None), "^DJI": ("DJI", None)}


def _td_symbol(ticker: str):
    if ticker in TD_INDEX_MAP:
        return TD_INDEX_MAP[ticker]
    if ticker.endswith(".NS"):
        return ticker[:-3], "NSE"
    if ticker.endswith(".BO"):
        return ticker[:-3], "BSE"
    return ticker, None


class TwelveDataProvider(Provider):
    name = "twelvedata"
    source_tier = "vendor"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        res = ProviderResult(provider=self.name, source_tier=self.source_tier)
        rows = self.history(ticker, years=getattr(config, "price_years", 10))
        if not rows:
            res.note_gap("price", "twelvedata returned no candles")
            return res
        res.prices = [dict(r, ticker=ticker, source=self.name, as_of=res.as_of) for r in rows]
        last = rows[-1]
        res.company.update({
            "ticker": ticker, "source": self.name, "as_of": res.as_of,
            "unit_scale": 1.0,
        })
        res.snapshot.update({
            "ticker": ticker, "as_of": res.as_of,
            "price": last["close"], "source": self.name,
        })
        for domain in ("income", "balance", "cashflow"):
            res.note_gap(
                domain,
                "twelvedata's free tier does not serve full financial statements",
            )
        return res

    def history(self, ticker: str, years: int = 10) -> List[Dict[str, Any]]:
        symbol, exchange = _td_symbol(ticker)
        params = {
            "symbol": symbol,
            "interval": "1day",
            "outputsize": min(5000, max(100, years * 260)),
            "apikey": self.api_key,
            "order": "ASC",
        }
        if exchange:
            params["exchange"] = exchange
        url = f"{TWELVEDATA_BASE}/time_series?{urllib.parse.urlencode(params)}"
        payload = http_get_json(url)

        if isinstance(payload, dict) and payload.get("status") == "error":
            raise FetchError(f"twelvedata: {payload.get('message', 'error')}")
        values = (payload or {}).get("values") or []
        out = []
        for v in values:
            try:
                close = float(v["close"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append({
                "date": str(v.get("datetime"))[:10],
                "open": _f(v.get("open")), "high": _f(v.get("high")),
                "low": _f(v.get("low")), "close": close, "adj_close": close,
                "volume": _f(v.get("volume")) or 0.0,
            })
        return out


class AlpacaProvider(Provider):
    name = "alpaca"
    source_tier = "vendor"

    def __init__(self, key_id: str, secret_key: str, base_url: str = "https://data.alpaca.markets"):
        self.key_id = key_id
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")

    def supports(self, market: str) -> bool:
        # Alpaca covers US equities and crypto only.
        return (market or "").strip().upper() in {"US", "USA", "CRYPTO"}

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        res = ProviderResult(provider=self.name, source_tier=self.source_tier)
        try:
            rows = self.history(ticker, years=getattr(config, "price_years", 10))
        except FetchError as exc:
            res.note_gap("price", str(exc))
            return res
        if not rows:
            res.note_gap("price", "alpaca returned no bars")
            return res
        res.prices = [dict(r, ticker=ticker, source=self.name, as_of=res.as_of) for r in rows]
        res.snapshot.update({
            "ticker": ticker, "as_of": res.as_of,
            "price": rows[-1]["close"], "source": self.name,
        })
        return res

    def history(self, ticker: str, years: int = 10) -> List[Dict[str, Any]]:
        start = (_dt.date.today() - _dt.timedelta(days=int(years * 365.25))).isoformat()
        out: List[Dict[str, Any]] = []
        page_token = None
        headers = {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
        }
        # Alpaca paginates; follow the cursor rather than silently truncating.
        for _ in range(20):
            params = {"timeframe": "1Day", "start": start, "limit": 10000,
                      "adjustment": "all"}
            if page_token:
                params["page_token"] = page_token
            url = (
                f"{self.base_url}/v2/stocks/{urllib.parse.quote(ticker)}/bars"
                f"?{urllib.parse.urlencode(params)}"
            )
            payload = http_get_json(url, headers=headers)
            for bar in (payload or {}).get("bars") or []:
                out.append({
                    "date": str(bar.get("t"))[:10],
                    "open": _f(bar.get("o")), "high": _f(bar.get("h")),
                    "low": _f(bar.get("l")), "close": _f(bar.get("c")),
                    "adj_close": _f(bar.get("c")), "volume": _f(bar.get("v")) or 0.0,
                })
            page_token = (payload or {}).get("next_page_token")
            if not page_token:
                break
        return [r for r in out if r["close"] is not None]


class IndianAPIProvider(Provider):
    """Adapter for a deployed Indian-Stock-Market-API instance.

    Endpoint shapes vary by deployment, so this only claims what it can verify:
    it maps whatever recognisable keys the payload carries and reports the rest
    as a gap. It never fabricates a statement line from a quote payload.
    """

    name = "indian_api"
    source_tier = "vendor"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def supports(self, market: str) -> bool:
        from ..config import market_is_india

        return market_is_india(market)

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        res = ProviderResult(provider=self.name, source_tier=self.source_tier)
        symbol = ticker.split(".")[0]
        url = f"{self.base_url}/stock?{urllib.parse.urlencode({'name': symbol})}"
        try:
            payload = http_get_json(url)
        except FetchError as exc:
            res.note_gap("company", f"indian_api unreachable: {exc}")
            return res

        if not isinstance(payload, dict):
            res.note_gap("company", "indian_api returned an unexpected payload shape")
            return res

        price = _first_number(payload, "currentPrice", "price", "lastPrice", "close")
        if price is not None:
            res.snapshot.update({
                "ticker": ticker, "as_of": res.as_of,
                "price": price, "currency": "INR", "source": self.name,
            })
        name = payload.get("companyName") or payload.get("name")
        if name:
            res.company.update({
                "ticker": ticker, "name": name, "currency": "INR",
                "market": "India", "source": self.name, "as_of": res.as_of,
            })
        res.note_gap(
            "price",
            "indian_api is quote-oriented and serves no OHLCV history; another "
            "provider must supply the price series",
        )
        return res


# -- helpers ---------------------------------------------------------------


def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(payload: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _f(payload.get(key))
        if value is not None:
            return value
    return None


def fetch_benchmark_history(
    symbol: str, settings: Any, years: int = 3
) -> List[Dict[str, Any]]:
    """Benchmark candles for §S regime detection, trying every configured source."""
    errors = []
    if getattr(settings, "twelvedata_api_key", ""):
        try:
            rows = TwelveDataProvider(settings.twelvedata_api_key).history(symbol, years)
            if rows:
                return rows
        except Exception as exc:
            errors.append(f"twelvedata: {exc}")
    try:
        from .yahoo import CHART_URL, _parse_chart_rows

        payload = http_get_json(f"{CHART_URL.format(sym=symbol)}?range={years}y&interval=1d")
        results = (payload.get("chart") or {}).get("result") or []
        if results:
            from ..db import utc_now

            return _parse_chart_rows(symbol, results[0], "yahoo", utc_now())
    except Exception as exc:
        errors.append(f"yahoo: {exc}")
    raise FetchError("; ".join(errors) or f"no provider could supply {symbol}")
