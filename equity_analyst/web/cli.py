"""Entry point for the website and the refresh scheduler.

    python -m equity_analyst.web                     # serve on :8000
    python -m equity_analyst.web --sync-directory    # pull the SEC universe
    python -m equity_analyst.web --refresh-stale     # one scheduler pass
    python -m equity_analyst.web --scheduler         # serve + refresh loop
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import List, Optional

from ..config import Assumptions, DataSettings
from ..warehouse import DEFAULT_DB, FreshnessPolicy, Warehouse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="equity-analyst-web",
        description="Search any listed company by name, analyse it, and serve the report.",
    )
    p.add_argument("--db", default=DEFAULT_DB, help=f"warehouse path (default {DEFAULT_DB})")
    p.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 to expose)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=1,
                   help="concurrent analyses; keep low, the data sources are shared")
    p.add_argument("--no-network", action="store_true",
                   help="serve only what is already cached")
    p.add_argument("--network-budget", type=float, default=90.0,
                   help="per-analysis ceiling on network time, seconds")

    f = p.add_argument_group("freshness")
    f.add_argument("--price-hours", type=float, default=12.0)
    f.add_argument("--statement-hours", type=float, default=720.0)
    f.add_argument("--report-hours", type=float, default=12.0)

    a = p.add_argument_group("one-shot actions (no server)")
    a.add_argument("--sync-directory", action="store_true",
                   help="pull the SEC ticker universe into SQL, then exit")
    a.add_argument("--refresh", metavar="TICKER", action="append", default=[],
                   help="refresh one company, then exit (repeatable)")
    a.add_argument("--refresh-stale", action="store_true",
                   help="refresh everything past its freshness window, then exit")
    a.add_argument("--limit", type=int, default=25, help="max companies per refresh pass")

    s = p.add_argument_group("scheduler")
    s.add_argument("--scheduler", action="store_true",
                   help="run a background refresh loop alongside the server")
    s.add_argument("--interval", type=float, default=3600.0,
                   help="seconds between scheduler passes (default 3600)")
    return p


def _warehouse(args: argparse.Namespace) -> Warehouse:
    return Warehouse(
        path=args.db,
        policy=FreshnessPolicy(
            price_hours=args.price_hours,
            statement_hours=args.statement_hours,
            report_hours=args.report_hours,
        ),
        settings=DataSettings.from_env(),
        assumptions=Assumptions(),
        allow_network=not args.no_network,
        network_budget_seconds=args.network_budget,
        # The web layer serialises DB access behind Site.lock.
        cross_thread=True,
    )


def _scheduler_loop(args: argparse.Namespace, stop: threading.Event) -> None:
    """Periodic refresh in its own warehouse handle.

    A separate SQLite connection, not the server's: sharing one across threads
    is exactly the sqlite3 misuse that produces intermittent corruption.
    """
    while not stop.is_set():
        if stop.wait(args.interval):
            return
        try:
            wh = _warehouse(args)
            try:
                due = wh.stale(args.limit)
                if due:
                    print(f"[scheduler] refreshing {len(due)} stale companies")
                    for row in due:
                        if stop.is_set():
                            return
                        outcome = wh.refresh(
                            row["ticker"], row.get("market") or "", trigger="scheduler"
                        )
                        print(f"[scheduler] {outcome.ticker}: {outcome.status} "
                              f"({outcome.duration_ms} ms)")
            finally:
                wh.close()
        except Exception as exc:  # a bad pass must not kill the loop
            print(f"[scheduler] pass failed: {exc}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    wh = _warehouse(args)

    try:
        if args.sync_directory:
            count = wh.sync_directory()
            print(f"ticker directory synced: {count} entries")
            return 0

        if args.refresh:
            for ticker in args.refresh:
                outcome = wh.refresh(ticker, trigger="cli")
                print(f"{outcome.ticker}: {outcome.status} "
                      f"verification={outcome.verification} ({outcome.duration_ms} ms)")
                for gap in outcome.gaps[:5]:
                    print(f"   gap: {gap}")
            return 0

        if args.refresh_stale:
            outcomes = wh.refresh_stale(args.limit, trigger="cli")
            if not outcomes:
                print("nothing due a refresh")
            for o in outcomes:
                print(f"{o.ticker}: {o.status} ({o.duration_ms} ms)")
            return 0

        # Seed the directory on first run so name search works immediately.
        if wh.directory_size() == 0 and not args.no_network:
            print("first run: syncing the SEC ticker directory…")
            try:
                print(f"  {wh.sync_directory()} tickers indexed")
            except Exception as exc:
                print(f"  directory sync failed ({exc}); name search will fall "
                      f"back to live lookup", file=sys.stderr)

        stop = threading.Event()
        if args.scheduler:
            threading.Thread(
                target=_scheduler_loop, args=(args, stop), daemon=True,
                name="refresh-scheduler",
            ).start()
            print(f"scheduler on: a refresh pass every {args.interval:.0f}s")

        from .server import serve

        try:
            serve(wh, host=args.host, port=args.port, workers=args.workers)
        finally:
            stop.set()
        return 0
    finally:
        wh.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
