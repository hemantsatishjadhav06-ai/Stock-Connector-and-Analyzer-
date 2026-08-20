"""The multi-company warehouse: resolve, analyse, cache, refresh.

One persistent SQLite file holds every company the system has seen. A page
load reads a cached report; a refresh re-scrapes and re-analyses in place.

**Freshness is tracked per domain, not per company.** Prices go stale every
trading day; audited statements only change when a company files. Using one
timestamp for both would force a full re-scrape daily just to move a quote,
which is how you get rate-limited off every free data source.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import ENGINE_VERSION, Assumptions, DataSettings, RunConfig
from .db import Database, utc_now
from .search import Resolution, normalize, resolve, sync_ticker_directory

DEFAULT_DB = "data/warehouse.sqlite"


@dataclass
class FreshnessPolicy:
    """How old each domain may get before a refresh is due (hours)."""

    price_hours: float = 12.0          # a trading day
    statement_hours: float = 24.0 * 30 # statements move quarterly at best
    report_hours: float = 12.0         # rendered report follows the price
    failed_retry_hours: float = 2.0    # do not hammer a source that just failed

    def report_stale(self, generated_at: Optional[str]) -> bool:
        return _age_hours(generated_at) is None or _age_hours(generated_at) > self.report_hours


def _age_hours(stamp: Optional[str]) -> Optional[float]:
    if not stamp:
        return None
    try:
        when = _dt.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - when).total_seconds() / 3600.0


@dataclass
class RefreshOutcome:
    ticker: str
    status: str                        # ok | partial | failed
    verification: Optional[float] = None
    duration_ms: int = 0
    error: Optional[str] = None
    gaps: List[str] = field(default_factory=list)


class Warehouse:
    """Owns the persistent database and the analyse/cache/refresh cycle."""

    def __init__(
        self,
        path: str = DEFAULT_DB,
        policy: Optional[FreshnessPolicy] = None,
        settings: Optional[DataSettings] = None,
        assumptions: Optional[Assumptions] = None,
        allow_network: bool = True,
        network_budget_seconds: float = 90.0,
        with_signals: bool = True,
        with_backtest: bool = True,
        cross_thread: bool = False,
    ):
        self.path = path
        self.policy = policy or FreshnessPolicy()
        self.settings = settings or DataSettings.from_env()
        self.assumptions = assumptions or Assumptions()
        self.allow_network = allow_network
        self.network_budget_seconds = network_budget_seconds
        self.with_signals = with_signals
        self.with_backtest = with_backtest
        self.db = Database(path, warehouse=True, cross_thread=cross_thread)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- lookup ------------------------------------------------------
    def search(self, query: str, market_hint: str = "") -> Resolution:
        return resolve(
            query, self.db, market_hint=market_hint, use_network=self.allow_network
        )

    def sync_directory(self) -> int:
        return sync_ticker_directory(self.db)

    def directory_size(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM ticker_directory") or 0)

    # -- index -------------------------------------------------------
    def companies(self, limit: int = 200, analysed_only: bool = False) -> List[Dict[str, Any]]:
        """Tracked companies, most recently refreshed first.

        ``analysed_only`` drops the ones that are merely queued -- a company
        with no result yet must not appear under "recently analysed", which
        would read as a finished analysis that produced nothing.
        """
        where = "WHERE refresh_status IN ('ok','partial')" if analysed_only else ""
        return self.db.dicts(
            f"""
            SELECT * FROM company_index {where}
            ORDER BY (last_refreshed IS NULL), last_refreshed DESC
            LIMIT ?
            """,
            (limit,),
        )

    def company(self, ticker: str) -> Optional[Dict[str, Any]]:
        rows = self.db.dicts(
            "SELECT * FROM company_index WHERE ticker = ?", (ticker,)
        )
        return rows[0] if rows else None

    def cached_report(self, ticker: str) -> Optional[Dict[str, Any]]:
        rows = self.db.dicts("SELECT * FROM report_cache WHERE ticker = ?", (ticker,))
        return rows[0] if rows else None

    def stale(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Companies due a refresh, most overdue first.

        A company that failed recently is held back for ``failed_retry_hours``:
        re-scraping a source that just rejected us produces the same failure
        and burns the rate limit everything else shares.
        """
        rows = self.db.dicts("SELECT * FROM company_index")
        due = []
        for row in rows:
            age = _age_hours(row.get("last_refreshed"))
            if row.get("refresh_status") == "failed":
                if age is not None and age < self.policy.failed_retry_hours:
                    continue
            if age is None or age >= self.policy.report_hours:
                due.append((age if age is not None else 1e9, row))
        due.sort(key=lambda pair: -pair[0])
        return [row for _, row in due[:limit]]

    # -- the cycle ---------------------------------------------------
    def get_report(
        self, ticker: str, market: str = "", force: bool = False
    ) -> Dict[str, Any]:
        """Cached HTML if fresh, otherwise analyse now and cache the result."""
        cached = self.cached_report(ticker)
        if (
            cached
            and not force
            and cached.get("engine_version") == ENGINE_VERSION
            and not self.policy.report_stale(cached.get("generated_at"))
        ):
            return {"html": cached["html"], "cached": True,
                    "generated_at": cached["generated_at"],
                    "summary": json.loads(cached.get("summary_json") or "{}")}

        outcome = self.refresh(ticker, market=market, trigger="web")
        cached = self.cached_report(ticker)
        if cached:
            return {"html": cached["html"], "cached": False,
                    "generated_at": cached["generated_at"],
                    "summary": json.loads(cached.get("summary_json") or "{}"),
                    "outcome": outcome}
        return {"html": None, "cached": False, "outcome": outcome}

    def refresh(
        self, ticker: str, market: str = "", trigger: str = "cli"
    ) -> RefreshOutcome:
        """Re-scrape, re-analyse and re-cache one company."""
        from .pipeline import run
        from .report import render

        started = utc_now()
        began = time.monotonic()
        known = self.company(ticker) or {}
        market = market or known.get("market") or _guess_market(ticker)

        config = RunConfig(
            ticker=ticker,
            market=market,
            assumptions=self.assumptions,
            data=self.settings,
            allow_network=self.allow_network,
            network_budget_seconds=self.network_budget_seconds,
            with_signals=self.with_signals,
            with_backtest=self.with_backtest,
            db_handle=self.db,
        )

        try:
            report = run(config)
        except Exception as exc:  # a broken source must not kill the server
            outcome = RefreshOutcome(
                ticker=ticker, status="failed", error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - began) * 1000),
            )
            self._record_failure(ticker, market, started, outcome, trace=traceback.format_exc())
            return outcome

        html = render(report)
        summary = _summarise(report)
        verification = summary.get("verification_score")
        # "ok" means a verdict was actually produced. A run that completed but
        # could not value the company is "partial", not success -- the index
        # must not show it as healthy.
        status = "ok" if summary.get("intrinsic_value") is not None else "partial"
        duration = int((time.monotonic() - began) * 1000)

        keep, why = self._should_keep_existing(ticker, summary)
        if keep:
            # A source outage must not downgrade a good report. Record the
            # attempt and hold the refresh clock so we retry later, but leave
            # the better analysis in place for anyone loading the page.
            self.db.conn.execute(
                "UPDATE company_index SET last_refreshed = ?, refresh_status = ?, "
                "refresh_error = ?, refresh_count = refresh_count + 1 WHERE ticker = ?",
                (utc_now(), "partial", why, ticker),
            )
            self.db.conn.commit()
            self.db.insert("refresh_log", {
                "ticker": ticker, "started_at": started, "finished_at": utc_now(),
                "status": "partial", "verification": verification,
                "gaps": why + "\n" + "\n".join(report.warnings[:10]),
                "trigger": trigger,
            })
            return RefreshOutcome(
                ticker=ticker, status="partial", verification=verification,
                duration_ms=duration, error=why,
                gaps=list(report.warnings[:12]),
            )

        self.db.insert("report_cache", {
            "ticker": ticker, "generated_at": utc_now(),
            "engine_version": ENGINE_VERSION, "html": html,
            "summary_json": json.dumps(summary), "duration_ms": duration,
        })
        self._upsert_index(ticker, market, report, summary, status)
        self.db.insert("refresh_log", {
            "ticker": ticker, "started_at": started, "finished_at": utc_now(),
            "status": status, "verification": verification,
            "gaps": "\n".join(report.warnings[:12]), "trigger": trigger,
        })
        return RefreshOutcome(
            ticker=ticker, status=status, verification=verification,
            duration_ms=duration, gaps=list(report.warnings[:12]),
        )

    def _should_keep_existing(
        self, ticker: str, summary: Dict[str, Any]
    ) -> "tuple[bool, str]":
        """Would publishing this run replace a better report with a worse one?

        Data sources fail transiently. Without this guard the first scheduler
        pass during an outage would overwrite every cached analysis with a
        near-empty one, and the site would look broken until the source
        recovered. A refresh only publishes when it is at least as complete as
        what it replaces.
        """
        cached = self.cached_report(ticker)
        if not cached:
            return False, ""
        try:
            previous = json.loads(cached.get("summary_json") or "{}")
        except ValueError:
            return False, ""
        if not previous:
            return False, ""

        had_value = previous.get("intrinsic_value") is not None
        has_value = summary.get("intrinsic_value") is not None
        old_score = previous.get("verification_score") or 0.0
        new_score = summary.get("verification_score") or 0.0

        if had_value and not has_value:
            return True, (
                f"Kept the previous report: this refresh produced no intrinsic "
                f"value while the cached one has {previous.get('intrinsic_value')}. "
                f"Most likely a temporary data-source outage."
            )
        # A few points of drift is normal (a stale price, one missing filing).
        # A 15-point drop is a collapse in coverage, not new information.
        if new_score + 15.0 < old_score:
            return True, (
                f"Kept the previous report: verification fell from "
                f"{old_score:.0f}% to {new_score:.0f}%, which indicates lost "
                f"data rather than a changed business."
            )
        return False, ""

    def refresh_stale(self, limit: int = 20, trigger: str = "scheduler") -> List[RefreshOutcome]:
        out = []
        for row in self.stale(limit):
            out.append(self.refresh(row["ticker"], row.get("market") or "", trigger))
        return out

    def track(self, ticker: str, name: str = "", market: str = "") -> None:
        """Add a company to the index without analysing it yet."""
        if self.company(ticker):
            return
        self.db.insert("company_index", {
            "ticker": ticker, "name": name or ticker,
            "normalized_name": normalize(name or ticker),
            "market": market or _guess_market(ticker),
            "first_seen": utc_now(), "refresh_status": "pending",
        })

    # -- internals ---------------------------------------------------
    def _upsert_index(
        self, ticker: str, market: str, report: Any, summary: Dict[str, Any], status: str
    ) -> None:
        existing = self.company(ticker) or {}
        company = report.company or {}
        now = utc_now()
        has_prices = bool(
            self.db.scalar("SELECT COUNT(*) FROM price_daily WHERE ticker = ?", (ticker,))
        )
        has_statements = bool(
            self.db.scalar(
                "SELECT COUNT(*) FROM income_statement WHERE ticker = ? AND period_type='annual'",
                (ticker,),
            )
        )
        self.db.insert("company_index", {
            "ticker": ticker,
            "name": company.get("name") or existing.get("name") or ticker,
            "normalized_name": normalize(company.get("name") or ticker),
            "market": market,
            "exchange": company.get("exchange") or existing.get("exchange"),
            "currency": company.get("currency") or existing.get("currency"),
            "sector": company.get("sector") or existing.get("sector"),
            "industry": company.get("industry") or existing.get("industry"),
            "first_seen": existing.get("first_seen") or now,
            "last_refreshed": now,
            # Only stamp a domain as refreshed when rows actually arrived,
            # otherwise a failed pull would look current and never retry.
            "last_price_refresh": now if has_prices else existing.get("last_price_refresh"),
            "last_statement_refresh": now if has_statements else existing.get("last_statement_refresh"),
            "refresh_status": status,
            "refresh_error": None,
            "refresh_count": int(existing.get("refresh_count") or 0) + 1,
            "price": summary.get("price"),
            "verification_score": summary.get("verification_score"),
            "valuation_zone": summary.get("zone"),
            "forensic_flag": summary.get("forensic_flag"),
            "technical_bias": summary.get("technical_bias"),
            "intrinsic_value": summary.get("intrinsic_value"),
            "buy_below": summary.get("buy_below"),
        })

    def _record_failure(
        self, ticker: str, market: str, started: str, outcome: RefreshOutcome, trace: str
    ) -> None:
        existing = self.company(ticker) or {}
        self.db.insert("company_index", {
            "ticker": ticker,
            "name": existing.get("name") or ticker,
            "normalized_name": existing.get("normalized_name") or normalize(ticker),
            "market": market,
            "first_seen": existing.get("first_seen") or utc_now(),
            "last_refreshed": utc_now(),
            "refresh_status": "failed",
            "refresh_error": outcome.error,
            "refresh_count": int(existing.get("refresh_count") or 0) + 1,
        })
        self.db.insert("refresh_log", {
            "ticker": ticker, "started_at": started, "finished_at": utc_now(),
            "status": "failed", "error": outcome.error, "gaps": trace[-2000:],
            "trigger": "web",
        })


def _summarise(report: Any) -> Dict[str, Any]:
    """The verdict-card fields the index page needs, without the full HTML."""
    v, f, t = report.valuation, report.forensic, report.technicals
    ver = report.verification
    company = report.company or {}
    signals = report.signals
    return {
        "ticker": report.config.ticker,
        "name": company.get("name"),
        "market": report.config.market,
        "currency": company.get("currency"),
        "price": getattr(v, "current_price", None),
        "intrinsic_value": getattr(v, "selected_value", None),
        "buy_below": getattr(v, "buy_below", None),
        "zone": getattr(v, "zone", "unknown"),
        "upside_pct": getattr(v, "upside_pct", None),
        "verification_score": getattr(ver, "total", None),
        "verification_band": getattr(ver, "band", None),
        "forensic_flag": getattr(f, "flag", "unknown"),
        "technical_bias": getattr(t, "bias", "unknown"),
        "regime": getattr(report.regime, "regime", None) if report.regime else None,
        "actionable_signals": len(signals.actionable) if signals else 0,
        "warnings": len(report.warnings or []),
    }


def _guess_market(ticker: str) -> str:
    upper = (ticker or "").upper()
    if upper.endswith((".NS", ".BO")):
        return "India"
    if upper.endswith(".L"):
        return "UK"
    if upper.endswith((".DE", ".PA", ".AS", ".SW", ".MI", ".MC")):
        return "Europe"
    if upper.endswith(".T"):
        return "JP"
    return "US"
