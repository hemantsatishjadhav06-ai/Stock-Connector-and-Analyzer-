"""Universal company lookup: a typed name in, a ticker out.

Resolution is layered, cheapest and most authoritative first:

1. **Alias cache** -- a query this warehouse has already resolved.
2. **Local ticker directory** -- the SEC universe (~10k US registrants),
   synced once and then searched in SQL. No network per keystroke.
3. **Screener.in search** -- the authority for Indian listings.
4. **Yahoo search** -- the global catch-all for everything else.

Every candidate carries the source that produced it and a 0-1 confidence, and
the UI shows both. A wrong company is a far worse failure than a slow search,
so the engine never silently auto-picks a weak match: it disambiguates unless
one candidate is both strong and clearly ahead of the runner-up.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from .db import utc_now
from .providers.base import FetchError, http_get, http_get_json

SCREENER_SEARCH = "https://www.screener.in/api/company/search/?q={q}"
YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"

#: Legal-form suffixes that carry no identifying information. Stripped before
#: matching so "Apple Inc." and "apple" score as the same company.
SUFFIXES = {
    "ltd", "limited", "inc", "incorporated", "corp", "corporation", "plc",
    "sa", "nv", "ag", "co", "company", "holdings", "holding", "group",
    "the", "and", "&", "llc", "lp", "se", "spa", "oyj", "ab", "as", "asa",
}


def normalize(name: str) -> str:
    """Lowercase, strip punctuation and drop legal-form noise words."""
    text = re.sub(r"[^\w\s]", " ", (name or "").lower())
    tokens = [t for t in text.split() if t and t not in SUFFIXES]
    return " ".join(tokens) or (name or "").strip().lower()


@dataclass
class Candidate:
    ticker: str
    name: str
    market: str = ""
    exchange: str = ""
    source: str = ""
    score: float = 0.0
    cik: Optional[str] = None

    def as_row(self, query: str) -> Dict[str, Any]:
        return {
            "query": query, "ticker": self.ticker, "display_name": self.name,
            "market": self.market, "exchange": self.exchange,
            "source": self.source, "score": self.score, "as_of": utc_now(),
        }


@dataclass
class Resolution:
    query: str
    candidates: List[Candidate] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def best(self) -> Optional[Candidate]:
        return self.candidates[0] if self.candidates else None

    @property
    def unambiguous(self) -> bool:
        """One strong candidate, clearly ahead of the next.

        Both halves matter: a 0.95 match that ties with another 0.95 match is
        still ambiguous, and picking one would silently analyse the wrong
        company.
        """
        if not self.candidates:
            return False
        top = self.candidates[0]
        if top.score < 0.85:
            return False
        if len(self.candidates) == 1:
            return True
        runner_up = self.candidates[1]
        # An exact normalized-name (or ticker) hit is decisive, provided nothing
        # else matched exactly too. Requiring a fixed gap on top of that would
        # send "apple" -> AAPL to a disambiguation page over a near-namesake.
        if top.score >= 1.0 and runner_up.score < 1.0:
            return True
        return top.score - runner_up.score >= 0.15


def score_match(query_norm: str, name: str, ticker: str = "") -> float:
    """0-1 confidence that ``name``/``ticker`` is what the user typed."""
    name_norm = normalize(name)
    if not query_norm or not name_norm:
        return 0.0
    if query_norm == name_norm:
        return 1.0
    if ticker and query_norm == ticker.lower():
        return 1.0

    q_tokens, n_tokens = query_norm.split(), name_norm.split()
    if n_tokens[: len(q_tokens)] == q_tokens:
        # "reliance" matching "reliance industries" -- a real prefix hit, but
        # damped by how much of the name is left unexplained, so the exact
        # company still outranks its longer-named siblings.
        return 0.90 - 0.04 * min(5, len(n_tokens) - len(q_tokens))
    if name_norm.startswith(query_norm):
        return 0.86

    overlap = len(set(q_tokens) & set(n_tokens))
    if overlap:
        coverage = overlap / max(len(q_tokens), 1)
        return 0.55 + 0.25 * coverage
    return 0.75 * SequenceMatcher(None, query_norm, name_norm).ratio()


# -- individual sources ----------------------------------------------------


def search_directory(db: Any, query: str, limit: int = 8) -> List[Candidate]:
    """Search the locally synced ticker directory (SQL, no network)."""
    q = normalize(query)
    if not q:
        return []
    rows = db.dicts(
        """
        SELECT ticker, name, normalized_name, market, exchange, cik
        FROM ticker_directory
        WHERE normalized_name LIKE ? OR ticker = ?
        LIMIT 400
        """,
        (f"%{q}%", query.strip().upper()),
    )
    out = [
        Candidate(
            ticker=r["ticker"], name=r["name"], market=r["market"],
            exchange=r["exchange"] or "", source="sec-directory",
            score=score_match(q, r["name"], r["ticker"]), cik=r.get("cik"),
        )
        for r in rows
    ]
    out.sort(key=lambda c: -c.score)
    return out[:limit]


def search_screener(query: str, limit: int = 8) -> List[Candidate]:
    """Screener.in company search -- the authority for Indian listings."""
    url = SCREENER_SEARCH.format(q=urllib.parse.quote(query))
    payload = json.loads(http_get(url).decode("utf-8", "replace"))
    q = normalize(query)
    out = []
    for row in payload if isinstance(payload, list) else []:
        url_path = row.get("url") or ""
        match = re.search(r"/company/([^/]+)", url_path)
        if not match:
            continue
        symbol = match.group(1).upper()
        name = row.get("name") or symbol
        out.append(Candidate(
            ticker=f"{symbol}.NS", name=name, market="India",
            exchange="NSE/BSE", source="screener.in",
            score=score_match(q, name, symbol),
        ))
    out.sort(key=lambda c: -c.score)
    return out[:limit]


def search_yahoo(query: str, limit: int = 8) -> List[Candidate]:
    """Yahoo's global symbol search -- the catch-all for other markets."""
    params = urllib.parse.urlencode(
        {"q": query, "quotesCount": limit, "newsCount": 0}
    )
    payload = http_get_json(f"{YAHOO_SEARCH}?{params}")
    q = normalize(query)
    out = []
    for row in (payload or {}).get("quotes") or []:
        symbol = row.get("symbol")
        if not symbol or row.get("quoteType") not in (None, "EQUITY", "ETF", "INDEX"):
            continue
        name = row.get("longname") or row.get("shortname") or symbol
        out.append(Candidate(
            ticker=symbol, name=name,
            market=_market_from_symbol(symbol, row.get("exchange")),
            exchange=row.get("fullExchangeName") or row.get("exchange") or "",
            source="yahoo", score=score_match(q, name, symbol),
        ))
    out.sort(key=lambda c: -c.score)
    return out[:limit]


def _market_from_symbol(symbol: str, exchange: Optional[str]) -> str:
    upper = (symbol or "").upper()
    if upper.endswith((".NS", ".BO")):
        return "India"
    if upper.endswith(".L"):
        return "UK"
    if upper.endswith((".DE", ".PA", ".AS", ".SW", ".MI", ".MC")):
        return "Europe"
    if upper.endswith(".T"):
        return "JP"
    return "US"


# -- orchestration ---------------------------------------------------------


def resolve(
    query: str,
    db: Any = None,
    market_hint: str = "",
    use_network: bool = True,
    limit: int = 8,
) -> Resolution:
    """Resolve a typed company name (or ticker) to ranked candidates."""
    res = Resolution(query=query)
    q = normalize(query)
    if not q:
        res.notes.append("Empty query.")
        return res

    seen: Dict[str, Candidate] = {}

    def add(candidates: List[Candidate]) -> None:
        for c in candidates:
            existing = seen.get(c.ticker.upper())
            if existing is None or c.score > existing.score:
                seen[c.ticker.upper()] = c

    if db is not None:
        cached = db.dicts(
            """
            SELECT ticker, display_name, market, exchange, source, score
            FROM company_alias WHERE query = ? ORDER BY score DESC LIMIT ?
            """,
            (q, limit),
        )
        add([
            Candidate(ticker=r["ticker"], name=r["display_name"] or r["ticker"],
                      market=r["market"] or "", exchange=r["exchange"] or "",
                      source=r["source"] or "cache", score=r["score"] or 0.0)
            for r in cached
        ])
        add(search_directory(db, query, limit))

    hint = (market_hint or "").strip().upper()
    india_first = hint in {"IN", "INDIA", "NSE", "BSE"}
    sources = [
        ("screener.in", search_screener, "INDIA"),
        ("yahoo", search_yahoo, ""),
    ]
    if not india_first:
        sources.reverse()

    def satisfied() -> bool:
        """Confident enough to stop searching.

        A high score alone is not enough when the user named a market: the SEC
        directory will happily return a perfect US match for "reliance" and
        skip the Indian search entirely. The confident match must also be in
        the market that was asked for.
        """
        if not seen:
            return False
        pool = list(seen.values())
        if hint:
            pool = [c for c in pool if c.market.strip().upper()[:2] == hint[:2]]
            if not pool:
                return False
        return max(c.score for c in pool) >= 0.95

    if use_network:
        for label, fn, covers in sources:
            if satisfied():
                break
            try:
                add(fn(query, limit))
            except (FetchError, Exception) as exc:  # noqa: BLE001
                res.notes.append(f"{label} search unavailable: {exc}")

    if not seen:
        res.notes.append(
            "No company matched. Try the full registered name, or the ticker "
            "directly (e.g. AAPL, RELIANCE.NS)."
        )
        return res

    ranked = sorted(seen.values(), key=lambda c: -c.score)
    if hint:
        ranked.sort(key=lambda c: (c.market.strip().upper()[:2] != hint[:2], -c.score))
    res.candidates = ranked[:limit]

    if db is not None:
        db.insert_many("company_alias", [c.as_row(q) for c in res.candidates])
    return res


def sync_ticker_directory(db: Any) -> int:
    """Pull the SEC ticker universe into SQL. Idempotent; run occasionally."""
    from .providers.sec import fetch_ticker_directory

    rows = fetch_ticker_directory()
    stamped = [
        dict(r, normalized_name=normalize(r["name"]), as_of=utc_now()) for r in rows
    ]
    return db.insert_many("ticker_directory", stamped)
