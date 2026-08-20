"""Company lookup, the persistent warehouse, and the job runner.

The behaviour that matters here is the one that decides whether a user gets
the *right* company: resolution must disambiguate rather than guess, and the
warehouse must not present stale or unanalysed rows as finished work.
"""

import json
import time

import pytest

from equity_analyst.db import Database
from equity_analyst.search import (
    Candidate,
    Resolution,
    normalize,
    score_match,
    search_directory,
)
from equity_analyst.warehouse import FreshnessPolicy, _age_hours, _guess_market
from equity_analyst.web.jobs import JobRunner


# -- normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Apple Inc.", "apple"),
        ("Reliance Industries Ltd", "reliance industries"),
        ("The Coca-Cola Company", "coca cola"),
        ("Tata Motors Limited", "tata motors"),
        ("HDFC Bank Ltd.", "hdfc bank"),
    ],
)
def test_normalize_strips_legal_form(raw, expected):
    assert normalize(raw) == expected


def test_normalize_never_returns_empty_for_real_input():
    # "Inc" alone is all suffix; falling through to empty would match everything.
    assert normalize("Inc") == "inc"


# -- scoring ---------------------------------------------------------------


def test_exact_name_scores_one():
    assert score_match(normalize("apple"), "Apple Inc.") == 1.0


def test_ticker_match_scores_one():
    assert score_match(normalize("aapl"), "Something Else", "AAPL") == 1.0


def test_prefix_is_damped_by_unexplained_words():
    """'reliance' should not tie with the exact company across every sibling."""
    short = score_match(normalize("reliance"), "Reliance Industries Ltd")
    long = score_match(normalize("reliance"), "Reliance Chemotex Industries Ltd")
    assert short > long


def test_unrelated_names_score_low():
    assert score_match(normalize("microsoft"), "Apple Inc.") < 0.35


# -- ambiguity -------------------------------------------------------------


def _res(*pairs):
    r = Resolution(query="q")
    r.candidates = [Candidate(ticker=t, name=t, score=s) for t, s in pairs]
    return r


def test_single_strong_candidate_is_unambiguous():
    assert _res(("AAPL", 0.95)).unambiguous


def test_exact_hit_beats_a_close_runner_up():
    """An exact 1.0 must not be sent to disambiguation by a 0.86 namesake."""
    assert _res(("AAPL", 1.0), ("AAPI", 0.86)).unambiguous


def test_two_exact_hits_stay_ambiguous():
    """HDFCBANK.NS and its US ADR both match exactly -- the user must choose."""
    assert not _res(("HDFCBANK.NS", 1.0), ("HDB", 1.0)).unambiguous


def test_weak_top_candidate_is_ambiguous():
    assert not _res(("XYZ", 0.6)).unambiguous


def test_close_pair_is_ambiguous():
    assert not _res(("A", 0.90), ("B", 0.86)).unambiguous


def test_empty_resolution_is_ambiguous():
    assert not Resolution(query="q").unambiguous


# -- directory search ------------------------------------------------------


@pytest.fixture
def wh_db(tmp_path):
    db = Database(str(tmp_path / "w.sqlite"), warehouse=True)
    db.insert_many("ticker_directory", [
        {"ticker": "AAPL", "name": "Apple Inc.", "normalized_name": "apple",
         "market": "US", "cik": "0000320193", "source": "sec"},
        {"ticker": "MSFT", "name": "Microsoft Corporation",
         "normalized_name": "microsoft", "market": "US", "source": "sec"},
    ])
    yield db
    db.close()


def test_directory_search_finds_by_name(wh_db):
    out = search_directory(wh_db, "apple")
    assert out and out[0].ticker == "AAPL"
    assert out[0].score == 1.0


def test_directory_search_finds_by_ticker(wh_db):
    out = search_directory(wh_db, "MSFT")
    assert out and out[0].ticker == "MSFT"


def test_directory_search_handles_no_match(wh_db):
    assert search_directory(wh_db, "zzzzznotacompany") == []


# -- persistence -----------------------------------------------------------


def test_warehouse_survives_reopen(tmp_path):
    path = str(tmp_path / "w.sqlite")
    db = Database(path, warehouse=True)
    db.insert("company_index", {"ticker": "AAPL", "name": "Apple Inc."})
    db.close()

    again = Database(path, warehouse=True)
    rows = again.dicts("SELECT * FROM company_index")
    again.close()
    assert len(rows) == 1 and rows[0]["ticker"] == "AAPL"


def test_plain_mode_still_rebuilds(tmp_path):
    """A one-shot CLI run must not inherit yesterday's staged rows."""
    path = str(tmp_path / "run.sqlite")
    db = Database(path)
    db.insert("company", {"ticker": "AAPL"})
    db.close()

    again = Database(path)                       # reset=True by default
    count = again.scalar("SELECT COUNT(*) FROM company")
    again.close()
    assert count == 0


def test_clear_company_leaves_other_companies(tmp_path):
    db = Database(str(tmp_path / "w.sqlite"), warehouse=True)
    for t in ("AAPL", "MSFT"):
        db.insert("company", {"ticker": t, "name": t})
        db.insert("price_daily", {"ticker": t, "date": "2026-01-01", "close": 1.0})
    db.clear_company("AAPL")
    assert db.scalar("SELECT COUNT(*) FROM company WHERE ticker='AAPL'") == 0
    assert db.scalar("SELECT COUNT(*) FROM company WHERE ticker='MSFT'") == 1
    assert db.scalar("SELECT COUNT(*) FROM price_daily WHERE ticker='MSFT'") == 1
    db.close()


# -- freshness -------------------------------------------------------------


def test_missing_timestamp_is_stale():
    assert FreshnessPolicy().report_stale(None)


def test_fresh_report_is_not_stale():
    from equity_analyst.db import utc_now

    assert not FreshnessPolicy(report_hours=12).report_stale(utc_now())


def test_old_report_is_stale():
    assert FreshnessPolicy(report_hours=1).report_stale("2020-01-01T00:00:00+00:00")


def test_age_hours_handles_naive_and_garbage():
    assert _age_hours("2020-01-01T00:00:00") > 0     # naive treated as UTC
    assert _age_hours("not-a-date") is None
    assert _age_hours(None) is None


@pytest.mark.parametrize(
    "ticker,market",
    [("AAPL", "US"), ("RELIANCE.NS", "India"), ("TCS.BO", "India"),
     ("BP.L", "UK"), ("SAP.DE", "Europe"), ("7203.T", "JP")],
)
def test_market_is_guessed_from_the_suffix(ticker, market):
    assert _guess_market(ticker) == market


# -- job runner ------------------------------------------------------------


def test_job_runs_and_reports_done():
    runner = JobRunner(lambda t, m: f"analysed {t}")
    try:
        job = runner.submit("AAPL")
        for _ in range(100):
            if job.finished:
                break
            time.sleep(0.05)
        assert job.state == "done"
        assert job.outcome == "analysed AAPL"
    finally:
        runner.shutdown()


def test_duplicate_submit_reuses_the_inflight_job():
    """Two browser tabs on the same company must not queue two analyses."""
    started = []

    def slow(ticker, market):
        started.append(ticker)
        time.sleep(0.4)
        return "ok"

    runner = JobRunner(slow)
    try:
        first = runner.submit("AAPL")
        second = runner.submit("AAPL")
        assert first is second
        for _ in range(100):
            if first.finished:
                break
            time.sleep(0.05)
        assert started.count("AAPL") == 1
    finally:
        runner.shutdown()


def test_worker_survives_a_failing_job():
    def boom(ticker, market):
        raise RuntimeError("source exploded")

    runner = JobRunner(boom)
    try:
        job = runner.submit("BAD")
        for _ in range(100):
            if job.finished:
                break
            time.sleep(0.05)
        assert job.state == "error"
        assert "source exploded" in job.error
        # the worker must still accept new work
        assert runner.submit("NEXT").state in ("queued", "running", "done")
    finally:
        runner.shutdown()


# -- the downgrade guard ---------------------------------------------------


def _cached(wh, ticker, summary):
    from equity_analyst.config import ENGINE_VERSION

    wh.db.insert("report_cache", {
        "ticker": ticker, "generated_at": utc_now_str(),
        "engine_version": ENGINE_VERSION, "html": "<html></html>",
        "summary_json": json.dumps(summary),
    })


def utc_now_str():
    from equity_analyst.db import utc_now

    return utc_now()


@pytest.fixture
def wh(tmp_path):
    from equity_analyst.warehouse import Warehouse

    w = Warehouse(str(tmp_path / "w.sqlite"), allow_network=False)
    yield w
    w.close()


def test_no_cache_means_publish(wh):
    keep, _ = wh._should_keep_existing("X", {"intrinsic_value": 10, "verification_score": 50})
    assert keep is False


def test_losing_the_valuation_keeps_the_old_report(wh):
    """A source outage must not replace a valued report with an empty one."""
    _cached(wh, "X", {"intrinsic_value": 100.0, "verification_score": 70.0})
    keep, why = wh._should_keep_existing("X", {"intrinsic_value": None, "verification_score": 26.0})
    assert keep is True
    assert "no intrinsic value" in why


def test_a_verification_collapse_keeps_the_old_report(wh):
    _cached(wh, "X", {"intrinsic_value": 100.0, "verification_score": 80.0})
    keep, why = wh._should_keep_existing("X", {"intrinsic_value": 90.0, "verification_score": 40.0})
    assert keep is True
    assert "verification fell" in why


def test_small_drift_still_publishes(wh):
    """Normal day-to-day variation must not freeze the cache forever."""
    _cached(wh, "X", {"intrinsic_value": 100.0, "verification_score": 72.0})
    keep, _ = wh._should_keep_existing("X", {"intrinsic_value": 98.0, "verification_score": 68.0})
    assert keep is False


def test_an_improvement_always_publishes(wh):
    _cached(wh, "X", {"intrinsic_value": 100.0, "verification_score": 55.0})
    keep, _ = wh._should_keep_existing("X", {"intrinsic_value": 105.0, "verification_score": 82.0})
    assert keep is False
