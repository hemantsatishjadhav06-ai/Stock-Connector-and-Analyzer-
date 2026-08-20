"""The website: stdlib HTTP server over the warehouse.

No framework and no build step -- ``python -m equity_analyst.web`` and it runs.
Requests never block on an analysis: a cache miss queues a background job and
returns a page that polls until the report exists.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from ..db import utc_now
from ..warehouse import Warehouse, _age_hours
from . import pages
from .jobs import JobRunner

TICKER_RE = re.compile(r"^[A-Za-z0-9._^-]{1,24}$")


class Site:
    """Holds the warehouse and the job runner for the process."""

    def __init__(self, warehouse: Warehouse, workers: int = 1):
        self.warehouse = warehouse
        # SQLite connections are not thread-safe, and the job worker writes
        # while request threads read, so every warehouse touch is serialised
        # through one lock rather than sharing a connection unguarded.
        self.lock = threading.RLock()
        self.jobs = JobRunner(self._analyse, workers=workers)

    def _analyse(self, ticker: str, market: str) -> Any:
        with self.lock:
            return self.warehouse.refresh(ticker, market=market, trigger="web")

    def read(self, fn, *args, **kwargs):
        with self.lock:
            return fn(*args, **kwargs)


class Handler(BaseHTTPRequestHandler):
    site: Site = None            # injected by serve()
    server_version = "equity-analyst"

    # -- plumbing ----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{utc_now()}] {self.address_string()} {fmt % args}")

    def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(json.dumps(data, default=str), status, "application/json; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routing -----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        try:
            self._route(path, params)
        except BrokenPipeError:
            pass
        except Exception as exc:  # never leak a traceback to a browser
            self.log_message("error handling %s: %s", path, exc)
            self._send(
                pages.shell("Error", f"<h1>Something went wrong</h1>"
                                     f"<p class='muted'>{pages.esc(exc)}</p>"),
                500,
            )

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        match = re.match(r"^/company/([^/]+)/refresh$", parsed.path)
        if match:
            ticker = urllib.parse.unquote(match.group(1))
            if TICKER_RE.match(ticker):
                self.site.jobs.submit(ticker)
                return self._redirect(f"/company/{urllib.parse.quote(ticker)}")
        self._send(pages.shell("Not found", "<h1>Not found</h1>"), 404)

    def _route(self, path: str, params: Dict[str, list]) -> None:
        site = self.site
        wh = site.warehouse

        if path == "/":
            recent = site.read(wh.companies, 6, True)
            size = site.read(wh.directory_size)
            return self._send(pages.home(recent, size))

        if path == "/healthz":
            return self._json({"ok": True, "time": utc_now()})

        if path == "/companies":
            return self._send(pages.companies(site.read(wh.companies, 300)))

        if path == "/status":
            return self._send(pages.status(self._status_info()))

        if path == "/search":
            return self._search(params)

        if path == "/api/search":
            query = _one(params, "q")
            if not query:
                return self._json({"error": "q is required"}, 400)
            res = site.read(wh.search, query, _one(params, "market"))
            return self._json({
                "query": query,
                "unambiguous": res.unambiguous,
                "candidates": [vars(c) for c in res.candidates],
                "notes": res.notes,
            })

        if path == "/api/companies":
            return self._json(site.read(wh.companies, 300))

        match = re.match(r"^/api/job/([^/]+)$", path)
        if match:
            ticker = urllib.parse.unquote(match.group(1))
            job = site.jobs.get(ticker)
            if job is None:
                return self._json({"state": "unknown", "message": "no job"}, 404)
            return self._json({
                "ticker": job.ticker, "state": job.state, "message": job.message,
                "error": job.error, "queued_at": job.queued_at,
                "finished_at": job.finished_at,
            })

        match = re.match(r"^/api/company/([^/]+)$", path)
        if match:
            ticker = urllib.parse.unquote(match.group(1))
            row = site.read(wh.company, ticker)
            if not row:
                return self._json({"error": "unknown ticker"}, 404)
            cached = site.read(wh.cached_report, ticker)
            row["summary"] = json.loads((cached or {}).get("summary_json") or "{}")
            return self._json(row)

        match = re.match(r"^/company/([^/]+)$", path)
        if match:
            return self._company(urllib.parse.unquote(match.group(1)), params)

        self._send(pages.shell("Not found", "<h1>Not found</h1>"
                                            "<p><a href='/'>Search for a company</a></p>"), 404)

    # -- handlers ----------------------------------------------------
    def _search(self, params: Dict[str, list]) -> None:
        query = _one(params, "q").strip()
        market = _one(params, "market").strip()
        if not query:
            return self._redirect("/")

        res = self.site.read(self.site.warehouse.search, query, market)
        # Go straight to the report only when the match is decisive. Guessing
        # between close names would analyse the wrong company silently.
        if res.unambiguous and res.best:
            target = urllib.parse.quote(res.best.ticker)
            hint = urllib.parse.quote(res.best.market or market)
            return self._redirect(f"/company/{target}?market={hint}")
        return self._send(pages.results(query, market, res.candidates, res.notes))

    def _company(self, ticker: str, params: Dict[str, list]) -> None:
        if not TICKER_RE.match(ticker):
            return self._send(pages.shell("Bad ticker", "<h1>Invalid ticker</h1>"), 400)

        site, wh = self.site, self.site.warehouse
        market = _one(params, "market")
        force = _one(params, "force") in ("1", "true", "yes")

        row = site.read(wh.company, ticker)
        cached = site.read(wh.cached_report, ticker)
        name = (row or {}).get("name") or ticker

        job = site.jobs.get(ticker)
        fresh = (
            cached
            and not force
            and not wh.policy.report_stale(cached.get("generated_at"))
        )

        if fresh and (job is None or job.finished):
            return self._send(self._wrap_report(ticker, name, cached, row, stale=False))

        # A stale-but-present report is served immediately with a refresh
        # queued behind it: showing yesterday's analysis beats showing a
        # spinner, as long as the page says how old it is.
        if job is None or job.finished:
            if force or cached is None or wh.policy.report_stale(cached.get("generated_at")):
                site.read(wh.track, ticker, name, market)
                job = site.jobs.submit(ticker, market)

        if cached and not force:
            return self._send(self._wrap_report(ticker, name, cached, row, stale=True))

        if job and job.state == "error":
            return self._send(pages.failed(ticker, job.error or "analysis failed", []))
        if job and job.state == "done" and cached is None:
            row = site.read(wh.company, ticker)
            reason = (row or {}).get("refresh_error") or (
                "The analysis completed but produced no report — most likely no "
                "data source could supply this company."
            )
            return self._send(pages.failed(ticker, reason, []))

        return self._send(pages.analysing(ticker, name, job))

    def _wrap_report(
        self, ticker: str, name: str, cached: Dict[str, Any], row: Optional[Dict[str, Any]],
        stale: bool,
    ) -> str:
        """Insert the site chrome into the standalone report HTML."""
        html = cached["html"]
        age = _age_hours(cached.get("generated_at"))
        age_text = (
            "just now" if age is not None and age < 0.05
            else f"{int(age * 60)} min ago" if age is not None and age < 1
            else f"{int(age)} h ago" if age is not None and age < 48
            else f"{int(age / 24)} d ago" if age is not None else "unknown"
        )
        note = (
            " · <strong>refreshing now</strong>, this page will update"
            if stale else ""
        )
        bar = f"""
<div class="reportbar">
  <span><a href="/">← Search</a> &nbsp;·&nbsp; <a href="/companies">All companies</a></span>
  <span class="muted">Updated {pages.esc(age_text)}{note}</span>
  <form method="post" action="/company/{urllib.parse.quote(ticker)}/refresh" style="margin:0">
    <button class="btn" type="submit">Refresh now</button>
  </form>
</div>
<style>{pages.SITE_CSS}</style>"""
        refresh_meta = '<meta http-equiv="refresh" content="20">' if stale else ""
        html = html.replace("</head>", f"{refresh_meta}</head>", 1)
        return html.replace('<main class="wrap viz-root">', f'{bar}<main class="wrap viz-root">', 1)

    def _status_info(self) -> Dict[str, Any]:
        site, wh = self.site, self.site.warehouse
        with site.lock:
            stats = {
                "Companies tracked": wh.db.scalar("SELECT COUNT(*) FROM company_index") or 0,
                "Reports cached": wh.db.scalar("SELECT COUNT(*) FROM report_cache") or 0,
                "Tickers in directory": wh.directory_size(),
                "Aliases cached": wh.db.scalar("SELECT COUNT(*) FROM company_alias") or 0,
                "Refreshes logged": wh.db.scalar("SELECT COUNT(*) FROM refresh_log") or 0,
                "Due a refresh": len(wh.stale(1000)),
                "Database": wh.path,
            }
            recent = wh.db.dicts(
                "SELECT * FROM refresh_log ORDER BY id DESC LIMIT 15"
            )
        return {"stats": stats, "jobs": site.jobs.active(), "recent_refreshes": recent}


def _one(params: Dict[str, list], key: str, default: str = "") -> str:
    values = params.get(key) or []
    return values[0] if values else default


def serve(
    warehouse: Warehouse,
    host: str = "127.0.0.1",
    port: int = 8000,
    workers: int = 1,
) -> None:
    site = Site(warehouse, workers=workers)
    Handler.site = site
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Stock Analyzer on http://{host}:{port}  (db: {warehouse.path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        site.jobs.shutdown()
        httpd.server_close()
