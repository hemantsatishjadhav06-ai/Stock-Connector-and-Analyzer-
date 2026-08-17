"""End-to-end run: §1 collect -> §2 load -> §3-§5 analyse -> §6 value -> §8 verify.

Orchestration only. Every number in the output is produced by the analysis
modules from the SQL layer; nothing is computed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import providers as provider_registry
from .analysis import QueryLog
from .analysis import forensics as forensics_mod
from .analysis import fundamentals as fundamentals_mod
from .analysis import linkage as linkage_mod
from .analysis import technicals as technicals_mod
from .config import ENGINE_VERSION, Assumptions, RunConfig
from .db import Database
from .providers.base import ProviderResult
from .valuation import models as valuation_mod
from . import verification as verification_mod


@dataclass
class Report:
    config: RunConfig
    db: Database
    provider_result: ProviderResult
    company: Dict[str, Any] = field(default_factory=dict)
    snapshot: Dict[str, Any] = field(default_factory=dict)
    fundamentals: Any = None
    forensic: Any = None
    technicals: Any = None
    linkage: Any = None
    valuation: Any = None
    verification: Any = None
    query_log: QueryLog = field(default_factory=QueryLog)
    warnings: List[str] = field(default_factory=list)

    @property
    def currency(self) -> str:
        return self.company.get("currency") or ""

    @property
    def unit_label(self) -> str:
        return self.company.get("unit_label") or self.currency


def run(config: RunConfig) -> Report:
    db = Database(config.db_path)
    db.start_run(
        config.run_id, config.ticker, config.market, config.horizon_years, ENGINE_VERSION
    )
    db.record_assumptions(config.run_id, config.assumptions.as_rows(Assumptions()))

    result = provider_registry.collect(config)
    report = Report(config=config, db=db, provider_result=result, query_log=QueryLog())

    _load(db, config, result, report)
    _record_coverage(db, config, result)

    log = report.query_log
    report.company = _first(db, "SELECT * FROM company WHERE ticker = ?", config.ticker)
    report.snapshot = _first(
        db,
        "SELECT * FROM market_snapshot WHERE ticker = ? ORDER BY as_of DESC LIMIT 1",
        config.ticker,
    )

    _load_commodities(db, config, report)

    report.fundamentals = fundamentals_mod.analyse(db, config.ticker, config, log)
    report.forensic = forensics_mod.analyse(db, config.ticker, config, log)
    report.technicals = technicals_mod.analyse(db, config.ticker, config, log)
    report.linkage = linkage_mod.analyse(db, config.ticker, config, log)

    price = report.snapshot.get("price")
    if price is None and report.technicals and report.technicals.last_close:
        price = report.technicals.last_close
        report.warnings.append(
            "No live quote available; the last daily close is used as the current price."
        )

    report.valuation = valuation_mod.value(
        report.fundamentals,
        price,
        report.company.get("shares_outstanding"),
        report.company.get("currency") or "",
        config.assumptions,
    )
    report.verification = verification_mod.score(
        db, config, result, report.fundamentals, report.forensic,
        report.valuation, report.technicals,
    )

    for domain, reason in (result.gaps or {}).items():
        report.warnings.append(f"{domain}: {reason}")
    return report


# -- staging ---------------------------------------------------------------


def _load(db: Database, config: RunConfig, res: ProviderResult, report: Report) -> None:
    company = dict(res.company or {})
    company.setdefault("ticker", config.ticker)
    company.setdefault("market", config.market)
    if config.company_name:
        company["name"] = config.company_name
    company.setdefault("name", config.ticker)
    db.insert("company", company)

    if res.snapshot:
        snapshot = dict(res.snapshot)
        snapshot.setdefault("ticker", config.ticker)
        snapshot.setdefault("as_of", res.as_of)
        db.insert("market_snapshot", snapshot)

    loaded = {
        "income_statement": db.insert_many("income_statement", res.income),
        "balance_sheet": db.insert_many("balance_sheet", res.balance),
        "cash_flow": db.insert_many("cash_flow", res.cashflow),
        "price_daily": db.insert_many("price_daily", res.prices),
        "shareholding": db.insert_many("shareholding", res.shareholding),
        "ratios_reported": db.insert_many("ratios_reported", res.ratios),
        # An offline bundle carries its own commodity history; without this the
        # §5 linkage would silently go qualitative on a replayed run.
        "commodity_price": db.insert_many("commodity_price", res.commodity_prices),
    }
    for table, count in loaded.items():
        if count == 0 and table not in ("shareholding", "ratios_reported", "commodity_price"):
            report.warnings.append(f"No rows loaded into {table}.")

    news = list(res.news or [])
    if not news and config.allow_network and not config.offline_file:
        try:
            from .providers.yahoo import fetch_news

            news = fetch_news(config.ticker)
        except Exception as exc:
            report.warnings.append(f"News unavailable: {exc}")
    db.insert_many("news_item", news)


def _record_coverage(db: Database, config: RunConfig, res: ProviderResult) -> None:
    tier = res.source_tier
    for domain, present in res.domains_present.items():
        db.record_coverage(
            config.run_id,
            domain,
            "*",
            present,
            source=res.provider,
            source_tier=tier if present else None,
            as_of=res.as_of,
            note=res.gaps.get(domain),
        )


def _load_commodities(db: Database, config: RunConfig, report: Report) -> None:
    """Fetch the price series for whichever commodities §5 will test."""
    profile = report.company or {}
    if config.commodities:
        candidates = [
            {"symbol": s, "name": s, "role": "input"} for s in config.commodities
        ]
    else:
        candidates = linkage_mod.default_commodities_for(profile)

    for candidate in candidates:
        db.insert(
            "commodity_link",
            {
                "ticker": config.ticker,
                "symbol": candidate["symbol"],
                "name": candidate.get("name"),
                "role": candidate.get("role"),
                "rationale": "sector/industry keyword match"
                if not config.commodities
                else "supplied on the command line",
            },
        )
        if not config.allow_network or config.offline_file:
            continue
        try:
            from .providers.yahoo import fetch_commodity_series

            db.insert_many(
                "commodity_price",
                fetch_commodity_series(candidate["symbol"], config.price_years),
            )
        except Exception as exc:
            report.warnings.append(
                f"Commodity series {candidate['symbol']} unavailable: {exc}"
            )


def _first(db: Database, sql: str, *params: Any) -> Dict[str, Any]:
    rows = db.dicts(sql, params)
    return rows[0] if rows else {}
