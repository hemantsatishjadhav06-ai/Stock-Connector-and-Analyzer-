"""Provider registry -- picks the right screener per market and degrades.

Resolution order (§0 "use what is available; degrade gracefully"):

* an explicit offline bundle wins outright -- it exists to pin a run;
* India -> Screener.in first (audited-filing aggregator, the market's primary
  screener), then Yahoo ``.NS`` for the price series Screener does not carry;
* everywhere else -> OpenBB when installed, then Yahoo.

Providers are merged with :func:`merge_results`, first-supplier-wins per
domain, so the higher-tier source always sets the number and the lower-tier one
only fills what is missing.
"""

from __future__ import annotations

from typing import Any, List

from .base import Provider, ProviderResult, merge_results
from .offline import OfflineProvider, result_to_bundle, save_bundle
from .openbb_provider import OpenBBProvider, openbb_available
from .screener_in import ScreenerInProvider
from .yahoo import YahooProvider

__all__ = [
    "Provider",
    "ProviderResult",
    "merge_results",
    "OfflineProvider",
    "OpenBBProvider",
    "ScreenerInProvider",
    "YahooProvider",
    "openbb_available",
    "save_bundle",
    "result_to_bundle",
    "build_chain",
    "collect",
]


def build_chain(config: Any) -> List[Provider]:
    """Ordered providers for this run. Highest-authority source first."""
    from ..config import market_is_india

    if getattr(config, "offline_file", None):
        return [OfflineProvider(config.offline_file)]
    if not getattr(config, "allow_network", True):
        return []

    chain: List[Provider] = []
    if market_is_india(getattr(config, "market", "")):
        chain.append(ScreenerInProvider())
        chain.append(YahooProvider())
    else:
        if openbb_available():
            chain.append(OpenBBProvider())
        chain.append(YahooProvider())
    return chain


def collect(config: Any) -> ProviderResult:
    """Run the chain and merge. Never raises -- an empty result is a valid
    outcome that the verification score will score at/near zero."""
    chain = build_chain(config)
    if not chain:
        empty = ProviderResult(provider="none")
        empty.note_gap("all", "no provider available (network disabled, no bundle)")
        return empty

    results: List[ProviderResult] = []
    for provider in chain:
        try:
            results.append(provider.fetch(config.ticker, config))
        except Exception as exc:  # a provider bug must not kill the run
            failed = ProviderResult(provider=provider.name)
            failed.note_gap(provider.name, f"provider raised: {exc}")
            results.append(failed)

    return merge_results(results[0], *results[1:])
