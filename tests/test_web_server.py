"""HTTP routes, driven against a real server on an ephemeral port.

Network is disabled for these runs, so they exercise the routing, the caching
and the degraded paths without touching a data source.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from equity_analyst.db import utc_now
from equity_analyst.warehouse import Warehouse
from equity_analyst.web.server import Handler, Site


@pytest.fixture(scope="module")
def live_site(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("wh") / "w.sqlite")
    wh = Warehouse(path, allow_network=False, cross_thread=True)
    wh.db.insert_many("ticker_directory", [
        {"ticker": "AAPL", "name": "Apple Inc.", "normalized_name": "apple",
         "market": "US", "source": "sec"},
        {"ticker": "AAPI", "name": "Apple Hospitality", "normalized_name": "apple hospitality",
         "market": "US", "source": "sec"},
    ])
    # A company that already has a cached report, so the fast path is covered.
    wh.db.insert("company_index", {
        "ticker": "CACHED", "name": "Cached Co", "normalized_name": "cached co",
        "market": "US", "currency": "USD", "first_seen": utc_now(),
        "last_refreshed": utc_now(), "refresh_status": "ok",
        "price": 10.0, "verification_score": 80.0, "valuation_zone": "buy",
        "forensic_flag": "pass", "intrinsic_value": 30.0, "buy_below": 15.0,
    })
    wh.db.insert("report_cache", {
        "ticker": "CACHED", "generated_at": utc_now(),
        "engine_version": __import__("equity_analyst.config", fromlist=["x"]).ENGINE_VERSION,
        "html": "<!doctype html><html><head></head><body>"
                '<main class="wrap viz-root">REPORT BODY</main></body></html>',
        "summary_json": json.dumps({"zone": "buy"}),
    })

    site = Site(wh, workers=1)
    Handler.site = site
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    site.jobs.shutdown()
    wh.close()


def get(base, path, allow_redirect=True):
    url = base + path
    opener = urllib.request.build_opener()
    if not allow_redirect:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers


def test_home_renders(live_site):
    status, body, _ = get(live_site, "/")
    assert status == 200
    assert "Analyse any listed company" in body
    assert "<form" in body


def test_healthz(live_site):
    status, body, _ = get(live_site, "/healthz")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_companies_page_lists_the_index(live_site):
    status, body, _ = get(live_site, "/companies")
    assert status == 200
    assert "Cached Co" in body


def test_status_page_reports_counts(live_site):
    status, body, _ = get(live_site, "/status")
    assert status == 200
    assert "Companies tracked" in body


def test_api_search_ranks_the_exact_match_first(live_site):
    status, body, _ = get(live_site, "/api/search?q=apple")
    assert status == 200
    data = json.loads(body)
    assert data["candidates"][0]["ticker"] == "AAPL"
    assert data["unambiguous"] is True


def test_api_search_requires_a_query(live_site):
    status, _, _ = get(live_site, "/api/search")
    assert status == 400


def test_search_redirects_when_unambiguous(live_site):
    status, _, headers = get(live_site, "/search?q=apple", allow_redirect=False)
    assert status == 303
    assert "/company/AAPL" in headers["Location"]


def test_search_shows_choices_when_ambiguous(live_site):
    """Two close matches must produce a picker, never a silent guess."""
    status, body, _ = get(live_site, "/search?q=appl")
    assert status == 200
    assert "Matches for" in body
    assert "AAPL" in body and "AAPI" in body


def test_empty_search_returns_home(live_site):
    status, _, headers = get(live_site, "/search?q=", allow_redirect=False)
    assert status == 303
    assert headers["Location"] == "/"


def test_cached_report_is_served_with_site_chrome(live_site):
    status, body, _ = get(live_site, "/company/CACHED")
    assert status == 200
    assert "REPORT BODY" in body          # the report itself
    assert "Refresh now" in body          # the injected site bar
    assert "All companies" in body


def test_api_company_returns_the_index_row(live_site):
    status, body, _ = get(live_site, "/api/company/CACHED")
    assert status == 200
    data = json.loads(body)
    assert data["ticker"] == "CACHED"
    assert data["summary"]["zone"] == "buy"


def test_api_company_404s_for_unknown(live_site):
    status, _, _ = get(live_site, "/api/company/NOPE")
    assert status == 404


def test_unknown_route_404s(live_site):
    status, body, _ = get(live_site, "/definitely/not/a/route")
    assert status == 404
    assert "Not found" in body


def test_invalid_ticker_is_rejected(live_site):
    """The ticker goes straight into SQL and a filename-ish URL, so it is
    validated rather than trusted."""
    status, _, _ = get(live_site, "/company/..%2F..%2Fetc%2Fpasswd")
    assert status == 400


def test_job_api_404s_for_unknown_ticker(live_site):
    status, body, _ = get(live_site, "/api/job/NOSUCH")
    assert status == 404
    assert json.loads(body)["state"] == "unknown"


def test_security_headers_are_set(live_site):
    _, _, headers = get(live_site, "/")
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_offline_company_request_degrades_without_crashing(live_site):
    """No network and no cache: the page must explain, not 500."""
    status, body, _ = get(live_site, "/company/AAPL")
    assert status == 200
    assert ("Analysing" in body) or ("Could not analyse" in body)
