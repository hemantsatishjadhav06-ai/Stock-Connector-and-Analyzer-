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
from .market_data import (
    AlpacaProvider,
    IndianAPIProvider,
    TwelveDataProvider,
    fetch_benchmark_history,
)
from .offline import OfflineProvider, result_to_bundle, save_bundle
from .openbb_provider import OpenBBProvider, openbb_available
from .screener_in import ScreenerInProvider
from .sec import SECProvider
from .yahoo import YahooProvider

__all__ = [
    "Provider",
    "ProviderResult",
    "merge_results",
    "AlpacaProvider",
    "IndianAPIProvider",
    "TwelveDataProvider",
    "fetch_benchmark_history",
    "OfflineProvider",
    "OpenBBProvider",
    "ScreenerInProvider",
    "SECProvider",
    "YahooProvider",
    "openbb_available",
    "save_bundle",
    "result_to_bundle",
    "build_chain",
    "collect",
]


def build_chain(config: Any) -> List[Provider]:
    """Ordered providers for this run. Highest-authority source first.

    Statement quality decides the order, then price coverage. The keyed
    providers sit *after* the free ones for statements but are what keep the
    price series alive when a shared IP gets rate-limited, which is why they
    join the chain whenever a key is present rather than only on failure —
    first-supplier-wins merging means adding them can only fill gaps.
    """
    from ..config import market_is_india

    if getattr(config, "offline_file", None):
        return [OfflineProvider(config.offline_file)]
    if not getattr(config, "allow_network", True):
        return []

    market = getattr(config, "market", "")
    settings = getattr(config, "data", None)
    chain: List[Provider] = []

    if market_is_india(market):
        chain.append(ScreenerInProvider())      # audited filings, INR crore
        chain.append(YahooProvider())           # price series for .NS/.BO
    else:
        if openbb_available():
            chain.append(OpenBBProvider())
        # SEC XBRL is filing-tier -- the statements come from the 10-K itself
        # rather than a vendor's re-keying -- so it outranks Yahoo for US
        # fundamentals. It serves no prices, which Yahoo/TwelveData then fill.
        sec = SECProvider()
        if sec.supports(market):
            chain.append(sec)
        chain.append(YahooProvider())

    if settings is not None:
        if getattr(settings, "twelvedata_api_key", ""):
            chain.append(TwelveDataProvider(settings.twelvedata_api_key))
        if getattr(settings, "alpaca_key_id", "") and getattr(settings, "alpaca_secret_key", ""):
            alpaca = AlpacaProvider(
                settings.alpaca_key_id, settings.alpaca_secret_key, settings.alpaca_base_url
            )
            if alpaca.supports(market):
                chain.append(alpaca)
        if getattr(settings, "indian_api_base_url", "") and market_is_india(market):
            chain.append(IndianAPIProvider(settings.indian_api_base_url))

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
