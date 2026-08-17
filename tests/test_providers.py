"""Provider parsing and merge behaviour.

These parsers read third-party HTML and JSON, so they are the parts most likely
to break silently. The tests pin the conversions that carry units and signs,
because a sign error here becomes a wrong valuation downstream.
"""

import pytest

from equity_analyst.providers.base import ProviderResult, merge_results
from equity_analyst.providers.screener_in import (
    _derive_shares_crore,
    _is_iso_date,
    _screener_symbol,
    _to_number,
)
from equity_analyst.providers.yahoo import _fiscal_label, _index_timeseries


# -- Screener.in -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1,234", 1234.0),
        ("₹ 1,310", 1310.0),
        ("17,72,762", 1772762.0),      # Indian digit grouping
        ("10%", 10.0),
        ("-5.6", -5.6),
        ("(42)", -42.0),               # accounting negatives
        ("", None),
        ("-", None),
        ("n/a", None),
    ],
)
def test_number_parsing(text, expected):
    assert _to_number(text) == expected


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("RELIANCE.NS", "RELIANCE"),
        ("reliance.bo", "RELIANCE"),
        ("TCS", "TCS"),
    ],
)
def test_symbol_normalisation(ticker, expected):
    assert _screener_symbol(ticker) == expected


def test_iso_date_detection_separates_fiscal_years_from_ttm():
    assert _is_iso_date("2024-03-31")
    assert not _is_iso_date("TTM")
    assert not _is_iso_date("")


def test_share_count_derives_from_equity_capital_and_face_value():
    """equity capital (crore) / face value (rupees) = shares in crore, which is
    the schema invariant that makes every per-share view a plain division."""
    balance = [{"equity_capital": 13532.0, "period_end": "2025-03-31"}]
    shares = _derive_shares_crore(balance, face_value=10.0,
                                  market_cap=1772762.0, price=1310.0)
    assert shares == pytest.approx(1353.2)
    # cross-check: it must reproduce the quoted market cap
    assert shares * 1310.0 == pytest.approx(1772762.0, rel=0.01)


def test_share_count_prefers_market_cap_when_equity_route_diverges():
    """Multiple share classes break the equity-capital route; the market-cap
    route is trusted when the two disagree by more than a rounding margin."""
    balance = [{"equity_capital": 100.0, "period_end": "2025-03-31"}]
    shares = _derive_shares_crore(balance, face_value=1.0,
                                  market_cap=1000.0, price=2.0)
    assert shares == pytest.approx(500.0)


def test_share_count_returns_none_without_inputs():
    assert _derive_shares_crore([], None, None, None) is None


# -- Yahoo -----------------------------------------------------------------


def test_fiscal_label_preserves_the_reported_close():
    assert _fiscal_label("2024-03-31") == "Mar-24"
    assert _fiscal_label("2023-12-31") == "Dec-23"
    assert _fiscal_label("garbage") == "garbage"


def test_timeseries_indexing_groups_by_period():
    payload = {
        "timeseries": {
            "result": [
                {"annualTotalRevenue": [
                    {"asOfDate": "2024-09-28", "reportedValue": {"raw": 391035000000}},
                ]},
                {"annualNetIncome": [
                    {"asOfDate": "2024-09-28", "reportedValue": {"raw": 93736000000}},
                ]},
            ]
        }
    }
    indexed = _index_timeseries(payload)
    assert indexed["2024-09-28"]["TotalRevenue"] == 391035000000
    assert indexed["2024-09-28"]["NetIncome"] == 93736000000


def test_timeseries_indexing_survives_nulls_and_junk():
    payload = {"timeseries": {"result": [
        {"annualTotalRevenue": [None, {"asOfDate": None, "reportedValue": {"raw": 1}}]},
        {"timestamp": [1, 2, 3]},
    ]}}
    assert _index_timeseries(payload) == {}


# -- merge -----------------------------------------------------------------


def test_first_supplier_wins_each_domain():
    primary = ProviderResult(provider="screener.in", source_tier="screener")
    primary.income = [{"ticker": "T", "sales": 100}]
    secondary = ProviderResult(provider="yahoo")
    secondary.income = [{"ticker": "T", "sales": 999}]
    secondary.prices = [{"ticker": "T", "date": "2026-01-01", "close": 5.0}]

    merged = merge_results(primary, secondary)
    assert merged.income[0]["sales"] == 100      # authoritative source kept
    assert merged.prices                          # gap filled from the fallback
    assert merged.source_tier == "screener"


def test_merge_fills_missing_company_fields_without_overwriting():
    primary = ProviderResult(provider="screener.in")
    primary.company = {"ticker": "T", "name": "Real Name", "currency": None}
    secondary = ProviderResult(provider="yahoo")
    secondary.company = {"ticker": "T", "name": "Other", "currency": "INR",
                         "sector": "Energy"}

    merged = merge_results(primary, secondary)
    assert merged.company["name"] == "Real Name"
    assert merged.company["currency"] == "INR"
    assert merged.company["sector"] == "Energy"


def test_a_domain_someone_supplied_is_not_reported_as_a_gap():
    primary = ProviderResult(provider="screener.in")
    primary.note_gap("price", "screener has no price history")
    secondary = ProviderResult(provider="yahoo")
    secondary.prices = [{"ticker": "T", "date": "2026-01-01", "close": 5.0}]

    merged = merge_results(primary, secondary)
    assert "price" not in merged.gaps


def test_gaps_survive_when_nobody_supplies_the_domain():
    primary = ProviderResult(provider="screener.in")
    primary.note_gap("news", "unreachable")
    merged = merge_results(primary, ProviderResult(provider="yahoo"))
    assert "news" in merged.gaps
