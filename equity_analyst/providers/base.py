"""Provider contract and a small resilient HTTP client.

Design rule from the master prompt: *if a tool is unreachable, continue with
what you have and flag the gap in the verification score rather than guessing.*
So every fetch here can fail, and failure is data -- it is recorded on the
``ProviderResult`` and flows into §8 rather than raising the run to the ground.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..db import utc_now

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

#: How trustworthy a figure is, for the §8 "source quality" sub-score.
SOURCE_TIERS = {
    "filing": 1.00,      # audited statutory filing
    "screener": 0.90,    # screener aggregating audited filings
    "vendor": 0.75,      # market-data vendor (Yahoo / OpenBB provider)
    "estimated": 0.45,   # analyst/consensus estimate
    "interpolated": 0.25,  # filled by us from neighbouring periods
}


class FetchError(RuntimeError):
    """Raised inside a provider; callers convert it into a coverage gap."""


def http_get(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    retries: int = 4,
    backoff: float = 2.0,
) -> bytes:
    """GET with exponential backoff.

    Retries on 429/5xx and transport errors -- the shared egress IP used by
    hosted runs gets rate-limited by public finance endpoints routinely, and a
    single 429 is not a reason to abandon a data domain.
    """
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)

    last: Optional[Exception] = None
    for attempt in range(retries):
        wait = backoff * (2 ** attempt)
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise FetchError(f"HTTP {exc.code} for {url}") from exc
            # Respect the server's own pacing when it tells us one. Public
            # finance endpoints throttle by IP, and a shared egress address
            # gets 429s that a fixed backoff would keep walking into.
            wait = max(wait, _retry_after_seconds(exc, wait))
        except Exception as exc:  # transport / TLS / timeout
            last = exc
        if attempt < retries - 1:
            time.sleep(min(wait, 30.0))
    raise FetchError(f"unreachable after {retries} attempts: {url} ({last})")


def _retry_after_seconds(exc: urllib.error.HTTPError, default: float) -> float:
    header = None
    try:
        header = exc.headers.get("Retry-After")
    except Exception:
        return default
    if not header:
        return default
    try:
        return float(header)          # delta-seconds form
    except (TypeError, ValueError):
        return default                # HTTP-date form: fall back to our backoff


def http_get_json(url: str, **kw: Any) -> Any:
    return json.loads(http_get(url, **kw).decode("utf-8", "replace"))


@dataclass
class ProviderResult:
    """Staged rows plus the provenance the verification score needs."""

    provider: str
    as_of: str = field(default_factory=utc_now)
    company: Dict[str, Any] = field(default_factory=dict)
    snapshot: Dict[str, Any] = field(default_factory=dict)
    income: List[Dict[str, Any]] = field(default_factory=list)
    balance: List[Dict[str, Any]] = field(default_factory=list)
    cashflow: List[Dict[str, Any]] = field(default_factory=list)
    prices: List[Dict[str, Any]] = field(default_factory=list)
    shareholding: List[Dict[str, Any]] = field(default_factory=list)
    ratios: List[Dict[str, Any]] = field(default_factory=list)
    news: List[Dict[str, Any]] = field(default_factory=list)
    commodity_prices: List[Dict[str, Any]] = field(default_factory=list)
    source_tier: str = "vendor"
    #: domain -> human-readable reason the pull failed or was partial
    gaps: Dict[str, str] = field(default_factory=dict)

    def note_gap(self, domain: str, reason: str) -> None:
        self.gaps[domain] = reason

    @property
    def domains_present(self) -> Dict[str, bool]:
        return {
            "company": bool(self.company),
            "snapshot": bool(self.snapshot),
            "income": bool(self.income),
            "balance": bool(self.balance),
            "cashflow": bool(self.cashflow),
            "price": bool(self.prices),
            "shareholding": bool(self.shareholding),
            "news": bool(self.news),
        }


class Provider:
    """Base class. Subclasses implement :meth:`fetch` and never raise."""

    name = "base"
    source_tier = "vendor"

    def supports(self, market: str) -> bool:  # pragma: no cover - trivial
        return True

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        raise NotImplementedError


def merge_results(primary: ProviderResult, *others: ProviderResult) -> ProviderResult:
    """Fill gaps in ``primary`` from later providers, never overwriting.

    First provider to supply a domain wins, so the market's authoritative
    screener beats a generic vendor even when both respond.
    """
    merged = ProviderResult(provider=primary.provider, as_of=primary.as_of)
    merged.source_tier = primary.source_tier
    list_domains = (
        "income", "balance", "cashflow", "prices",
        "shareholding", "ratios", "news", "commodity_prices",
    )
    dict_domains = ("company", "snapshot")
    contributors = [primary, *others]

    for dom in dict_domains:
        for res in contributors:
            value = getattr(res, dom)
            if value:
                target = getattr(merged, dom)
                for k, v in value.items():
                    if target.get(k) in (None, "") and v not in (None, ""):
                        target[k] = v
                target.setdefault("source", res.provider)
    for dom in list_domains:
        for res in contributors:
            if getattr(res, dom) and not getattr(merged, dom):
                setattr(merged, dom, list(getattr(res, dom)))

    for res in contributors:
        for dom, reason in res.gaps.items():
            merged.gaps.setdefault(dom, reason)
    # A domain someone did supply is not a gap.
    for dom, present in merged.domains_present.items():
        if present:
            merged.gaps.pop(dom, None)
    merged.provider = " + ".join(
        dict.fromkeys(
            r.provider for r in contributors if any(r.domains_present.values())
        )
    ) or primary.provider
    return merged
