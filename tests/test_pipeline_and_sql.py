"""End-to-end behaviour: the SQL layer, graceful degradation and the report."""

import sqlite3

import pytest

from equity_analyst.config import RunConfig
from equity_analyst.db import Database
from equity_analyst.pipeline import run
from equity_analyst.report import render


# -- SQL layer -------------------------------------------------------------


def test_schema_creates_every_view():
    with Database(":memory:") as db:
        views = {
            r["name"] for r in db.query(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            )
        }
    assert {
        "v_annual", "v_margins", "v_returns", "v_dupont", "v_leverage",
        "v_capital_allocation", "v_earnings_quality", "v_working_capital_trend",
        "v_balance_sheet_flags", "v_historic_valuation", "v_cashflow_derived",
    } <= views


def test_fcf_and_reinvestment_are_derived_in_sql():
    with Database(":memory:") as db:
        db.insert("company", {"ticker": "T", "shares_outstanding": 10})
        db.insert("cash_flow", {
            "ticker": "T", "period_label": "Mar-24", "period_type": "annual",
            "cfo": 200.0, "capex": 50.0,
        })
        row = db.dicts("SELECT * FROM v_cashflow_derived WHERE ticker = 'T'")[0]
    assert row["fcf"] == pytest.approx(150.0)
    assert row["reinvestment_rate"] == pytest.approx(0.25)


def test_ttm_rows_are_excluded_from_the_annual_spine():
    """A trailing-twelve-month column is not a fiscal year and must not
    become the latest 'year' or skew a CAGR window."""
    with Database(":memory:") as db:
        db.insert("company", {"ticker": "T", "shares_outstanding": 10})
        for label, ptype, end in (
            ("Mar-24", "annual", "2024-03-31"),
            ("Mar-25", "annual", "2025-03-31"),
            ("TTM", "ttm", None),
        ):
            db.insert("income_statement", {
                "ticker": "T", "period_label": label, "period_type": ptype,
                "period_end": end, "sales": 100.0, "pat": 10.0,
            })
        labels = [r["period_label"] for r in db.dicts(
            "SELECT period_label FROM v_annual WHERE ticker = 'T' ORDER BY yr_idx"
        )]
    assert labels == ["Mar-24", "Mar-25"]


def test_insert_many_ignores_unknown_columns():
    with Database(":memory:") as db:
        db.insert("company", {"ticker": "T"})
        n = db.insert_many("income_statement", [{
            "ticker": "T", "period_label": "Mar-24", "period_type": "annual",
            "sales": 1.0, "not_a_real_column": "x",
        }])
    assert n == 1


def test_foreign_keys_are_enforced():
    with Database(":memory:") as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.insert("income_statement", {
                "ticker": "MISSING", "period_label": "Mar-24", "period_type": "annual",
            })


# -- pipeline --------------------------------------------------------------


def test_offline_run_populates_every_domain(report):
    db = report.db
    ticker = report.config.ticker
    for table in ("income_statement", "balance_sheet", "cash_flow", "price_daily"):
        count = db.scalar(f"SELECT COUNT(*) FROM {table} WHERE ticker = ?", (ticker,))
        assert count > 0, f"{table} is empty"


def test_run_records_assumptions_and_coverage(report):
    run_id = report.config.run_id
    assumptions = report.db.dicts(
        "SELECT * FROM assumption WHERE run_id = ?", (run_id,)
    )
    coverage = report.db.dicts(
        "SELECT * FROM data_coverage WHERE run_id = ?", (run_id,)
    )
    assert len(assumptions) > 10
    assert coverage


def test_query_log_captures_the_sql_for_the_appendix(report):
    entries = report.query_log.entries
    assert len(entries) > 10
    assert all(e["sql"] for e in entries)
    assert any("v_annual" in e["sql"] for e in entries)


def test_all_four_models_are_reported_even_when_unavailable(report):
    names = [m.name for m in report.valuation.models]
    assert len(names) == 4
    for model in report.valuation.models:
        assert model.available or model.unavailable_reason


def test_no_provider_yields_an_honest_empty_run():
    """Network off and no bundle: the run must complete, publish no verdict,
    and score its own confidence as low -- never invent data."""
    config = RunConfig(ticker="NOPE", market="US", allow_network=False)
    report = run(config)
    assert report.valuation.selected_value is None
    assert report.valuation.zone == "unknown"
    assert report.verification.total < 55.0
    assert report.warnings


def test_missing_data_never_becomes_a_number():
    config = RunConfig(ticker="NOPE", market="US", allow_network=False)
    report = run(config)
    for model in report.valuation.models:
        assert model.value is None
        assert model.unavailable_reason


# -- report ----------------------------------------------------------------


def test_report_is_self_contained(report):
    html = render(report)
    assert html.startswith("<!doctype html>")
    for external in ("<script src=", 'href="http', "cdn.", "@import"):
        assert external not in html, f"report reaches out to {external}"


def test_report_carries_the_disclaimer_and_the_appendix(report):
    html = render(report)
    assert "not personalised investment advice" in html
    assert "CREATE TABLE" in html          # the schema is printed
    assert "Verification =" in html        # the formula is printed


def test_report_renders_all_required_sections(report):
    html = render(report)
    for anchor in (
        'id="valuation"', 'id="fundamentals"', 'id="technical"',
        'id="linkage"', 'id="forensics"', 'id="verification"', 'id="appendix"',
    ):
        assert anchor in html


def test_report_is_theme_aware(report):
    html = render(report)
    assert "prefers-color-scheme:dark" in html
    assert '[data-theme="dark"]' in html


def test_every_chart_ships_a_table_view(report):
    html = render(report)
    assert html.count("tableview") >= 5
