"""Offline JSON-bundle provider.

Two jobs:

1. **Reproducibility.** A run can be pinned to a snapshot on disk so a report
   regenerates byte-identically months later, long after the live endpoints
   have revised their history.
2. **Air-gapped / rate-limited operation.** When every network provider is
   unreachable the engine still has a path to a complete run.

The bundle is exactly the ``ProviderResult`` shape, so ``--save-bundle`` on a
live run produces a file this provider can replay.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import Provider, ProviderResult

BUNDLE_DOMAINS = (
    "income", "balance", "cashflow", "prices",
    "shareholding", "ratios", "news", "commodity_prices",
)


class OfflineProvider(Provider):
    name = "offline-bundle"

    def __init__(self, path: str, source_tier: str = "screener"):
        self.path = path
        self.source_tier = source_tier

    def fetch(self, ticker: str, config: Any) -> ProviderResult:
        with open(self.path, "r", encoding="utf-8") as fh:
            bundle: Dict[str, Any] = json.load(fh)

        res = ProviderResult(
            provider=bundle.get("provider", self.name),
            source_tier=bundle.get("source_tier", self.source_tier),
        )
        if bundle.get("as_of"):
            res.as_of = bundle["as_of"]
        res.company = dict(bundle.get("company") or {})
        res.snapshot = dict(bundle.get("snapshot") or {})
        for domain in BUNDLE_DOMAINS:
            setattr(res, domain, list(bundle.get(domain) or []))
        res.gaps = dict(bundle.get("gaps") or {})

        # The bundle may be keyed to a different symbol spelling than the run;
        # stamp the requested ticker so foreign keys line up.
        res.company["ticker"] = ticker
        if res.snapshot:
            res.snapshot["ticker"] = ticker
        for domain in ("income", "balance", "cashflow", "prices", "shareholding", "ratios", "news"):
            for row in getattr(res, domain):
                row["ticker"] = ticker
        return res


def save_bundle(res: ProviderResult, path: str) -> None:
    """Persist a live pull so the exact run can be replayed later."""
    payload: Dict[str, Any] = {
        "provider": res.provider,
        "source_tier": res.source_tier,
        "as_of": res.as_of,
        "company": res.company,
        "snapshot": res.snapshot,
        "gaps": res.gaps,
    }
    for domain in BUNDLE_DOMAINS:
        payload[domain] = getattr(res, domain)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def result_to_bundle(res: ProviderResult) -> Dict[str, List[Any]]:
    payload: Dict[str, Any] = {
        "provider": res.provider,
        "source_tier": res.source_tier,
        "as_of": res.as_of,
        "company": res.company,
        "snapshot": res.snapshot,
        "gaps": res.gaps,
    }
    for domain in BUNDLE_DOMAINS:
        payload[domain] = getattr(res, domain)
    return payload
