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
    # §S ported from indian-stock-signal-ai
    regime: Any = None
    signals: Any = None
    backtests: List[Any] = field(default_factory=list)
    trade_scores: Dict[str, Any] = field(default_factory=dict)

    @property
    def currency(self) -> str:
        return self.company.get("currency") or ""

    @property
    def unit_label(self) -> str:
        return self.company.get("unit_label") or self.currency


def run(config: RunConfig) -> Report:
    from .providers.base import BUDGET

    BUDGET.start(config.network_budget_seconds if config.allow_network else None)
    try:
        return _run(config)
    finally:
        BUDGET.clear()


def _run(config: RunConfig) -> Report:
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
    if config.with_signals or config.with_backtest:
        _signals_and_backtest(db, config, report, log)

    report.verification = verification_mod.score(
        db, config, result, report.fundamentals, report.forensic,
        report.valuation, report.technicals,
    )

    for domain, reason in (result.gaps or {}).items():
        report.warnings.append(f"{domain}: {reason}")

    from .providers.base import BUDGET

    if BUDGET.exhausted_note:
        report.warnings.append(
            f"{BUDGET.exhausted_note}. Raise it with --network-budget if the "
            f"source is merely slow rather than blocking."
        )
    return report


def _signals_and_backtest(
    db: Database, config: RunConfig, report: Report, log: QueryLog
) -> None:
    """§S regime -> per-strategy signals -> cost-aware backtest."""
    from .analysis import regime as regime_mod
    from .strategies import engine as strategy_engine

    a = config.assumptions
    rows = log.run(
        db, "signal_price_series",
        "SELECT date, open, high, low, close, volume FROM price_daily "
        "WHERE ticker = ? ORDER BY date",
        (config.ticker,),
        "Daily bars feeding the strategy snapshot and the backtester.",
    )

    benchmark = config.resolved_benchmark()
    report.regime = _detect_regime(db, config, report, benchmark, a)

    snapshot = regime_mod.snapshot(rows, a)
    report.trade_scores = regime_mod.fundamental_score(report.fundamentals, a)

    if config.with_signals:
        report.signals = strategy_engine.evaluate(
            snapshot, report.trade_scores, report.regime, a
        )
        _persist_signals(db, config, report)

    if config.with_backtest:
        from . import backtest as backtest_mod

        report.backtests = backtest_mod.run_all(
            rows, a, start_cash=a.starting_cash
        )
        for result in report.backtests:
            if not result.ok:
                report.warnings.append(
                    f"backtest {result.strategy_id}: {result.error}"
                )
        _persist_backtests(db, config, report)


def _detect_regime(
    db: Database, config: RunConfig, report: Report, benchmark: str, a: Any
) -> Any:
    from .analysis import regime as regime_mod

    rows: List[Dict[str, Any]] = []
    if config.allow_network and not config.offline_file:
        try:
            from .providers import fetch_benchmark_history

            rows = fetch_benchmark_history(benchmark, config.data, years=3)
        except Exception as exc:
            report.warnings.append(
                f"Benchmark {benchmark} unavailable ({exc}); market regime is "
                f"reported as unknown, which gates every strategy off."
            )
    else:
        # Offline: fall back to the subject's own series so the regime code
        # path still runs. Labelled, because a single stock is not the market.
        rows = db.dicts(
            "SELECT date, open, high, low, close, volume FROM price_daily "
            "WHERE ticker = ? ORDER BY date",
            (config.ticker,),
        )
        if rows:
            report.warnings.append(
                f"No benchmark feed available offline; regime was derived from "
                f"{config.ticker}'s own price action, not from {benchmark}. "
                f"Treat the regime label as indicative only."
            )
            benchmark = f"{config.ticker} (self, no benchmark)"

    detected = regime_mod.detect_regime(rows, a, benchmark)
    db.insert("market_regime", {
        "run_id": config.run_id,
        "benchmark": benchmark,
        "as_of": detected.as_of,
        "regime": detected.regime,
        "confidence": detected.confidence,
        "atr_pct": detected.atr_pct,
        "drivers": "\n".join(detected.drivers),
    })
    return detected


def _persist_signals(db: Database, config: RunConfig, report: Report) -> None:
    db.insert_many("signal", [
        {
            "run_id": config.run_id, "ticker": config.ticker,
            "strategy_id": s.strategy_id, "strategy": s.strategy,
            "category": s.category, "archetype": s.archetype, "bias": s.bias,
            "fused_score": s.fused_score, "technical_score": s.technical_score,
            "fundamental_score": s.fundamental_score, "regime_score": s.regime_score,
            "regime_fit": int(s.regime_fit), "entry": s.entry, "stop": s.stop,
            "target": s.target, "reward_risk": s.reward_risk,
            "technical_reasons": "\n".join(s.technical_reasons),
            "gate_failures": "\n".join(s.gate_failures),
            "intraday_approx": int(s.intraday_approximation),
            "as_of": report.snapshot.get("as_of"),
        }
        for s in report.signals.signals
    ])


def _persist_backtests(db: Database, config: RunConfig, report: Report) -> None:
    for result in report.backtests:
        m = result.metrics or {}
        db.insert("backtest_result", {
            "run_id": config.run_id, "ticker": config.ticker,
            "strategy_id": result.strategy_id, "archetype": result.archetype,
            "period_from": m.get("from"), "period_to": m.get("to"),
            "trades": m.get("trades"), "win_rate_pct": m.get("win_rate_pct"),
            "expectancy_pct": m.get("expectancy_pct"),
            "profit_factor": m.get("profit_factor"),
            "total_return_pct": m.get("total_return_pct"),
            "cagr_pct": m.get("cagr_pct"),
            "max_drawdown_pct": m.get("max_drawdown_pct"),
            "sharpe": m.get("sharpe"), "exposure_pct": m.get("exposure_pct"),
            "buy_hold_pct": m.get("buy_hold_pct"),
            "excess_vs_buy_hold_pct": m.get("excess_vs_buy_hold_pct"),
            "round_trip_bps": m.get("round_trip_bps"),
            "intraday_approx": int(result.intraday_approximation),
            "error": result.error,
        })
        db.insert_many("backtest_trade", [
            {
                "run_id": config.run_id, "ticker": config.ticker,
                "strategy_id": result.strategy_id,
                "entry_date": t.entry_date, "exit_date": t.exit_date,
                "entry": t.entry, "exit": t.exit, "return_pct": t.return_pct,
                "bars_held": t.bars_held, "reason": t.reason,
            }
            for t in result.trades
        ])


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
