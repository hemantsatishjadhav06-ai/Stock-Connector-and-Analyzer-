#!/usr/bin/env python3
"""Generate the SYNTHETIC demo bundle used by the tests and the offline demo.

The numbers here describe an invented company. They are produced by a seeded
generator so every run is byte-identical, and they exist to exercise every code
path (all four valuation models, every forensic check, technicals, commodity
linkage) without touching the network.

**Nothing in the output is real market data.** The bundle is deliberately named
and labelled so it can never be mistaken for a filing.

    python3 tools/make_sample_bundle.py data/samples/demo_industrials.json
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import sys

TICKER = "DEMO.SYNTH"
NAME = "Demo Industrials Ltd (SYNTHETIC — not a real company)"
CURRENCY = "USD"
SHARES = 500_000_000.0          # absolute units, matching unit_scale = 1.0
AS_OF = "2026-08-17T00:00:00+00:00"
LAST_FY = 2026                   # fiscal years ending 31 March
YEARS = 10


def fiscal_label(d: dt.date) -> str:
    return f"{d.strftime('%b')}-{d.strftime('%y')}"


def build() -> dict:
    rng = random.Random(20260817)

    income, balance, cashflow, shareholding = [], [], [], []

    sales = 4_200_000_000.0
    net_worth = 2_600_000_000.0
    borrowings = 1_150_000_000.0
    gross_block = 3_400_000_000.0

    for i in range(YEARS):
        year = LAST_FY - YEARS + 1 + i
        end = dt.date(year, 3, 31)
        label = fiscal_label(end)

        growth = 0.115 + rng.uniform(-0.045, 0.055)
        sales *= 1 + growth
        ebitda_margin = 0.212 + rng.uniform(-0.012, 0.016) + i * 0.0016  # gently expanding
        ebitda = sales * ebitda_margin
        depreciation = gross_block * 0.068
        ebit = ebitda - depreciation
        interest = borrowings * 0.071
        other_income = 42_000_000.0 * (1 + i * 0.06)
        pbt = ebit + other_income - interest
        tax_rate = 0.245 + rng.uniform(-0.012, 0.012)
        tax = pbt * tax_rate
        pat = pbt - tax
        eps = pat / SHARES
        payout = 22.0 + rng.uniform(-2.5, 2.5)
        dps = eps * payout / 100.0

        cogs = sales * (0.575 + rng.uniform(-0.012, 0.012))
        income.append(
            {
                "ticker": TICKER, "period_label": label, "period_end": end.isoformat(),
                "period_type": "annual",
                "sales": r(sales), "cogs": r(cogs), "expenses": r(sales - ebitda),
                "ebitda": r(ebitda), "depreciation": r(depreciation), "ebit": r(ebit),
                "other_income": r(other_income), "interest": r(interest), "pbt": r(pbt),
                "tax": r(tax), "tax_rate": round(tax_rate, 4), "pat": r(pat),
                "eps": round(eps, 4), "dividend_per_share": round(dps, 4),
                "dividend_payout": round(payout, 2),
                "source": "synthetic-generator", "as_of": AS_OF,
            }
        )

        retained = pat - dps * SHARES
        net_worth += retained
        borrowings *= 1 + rng.uniform(-0.05, 0.055)
        gross_block *= 1 + rng.uniform(0.05, 0.10)
        cash = net_worth * (0.085 + rng.uniform(-0.015, 0.02))
        receivables = sales * (0.121 + i * 0.0018)     # mild, watch-level creep
        inventory = sales * (0.094 + rng.uniform(-0.004, 0.005))
        investments = net_worth * 0.06
        intangibles = net_worth * 0.045
        goodwill = net_worth * 0.021
        accumulated = gross_block * (0.30 + i * 0.012)
        net_block = gross_block - accumulated
        current_liabilities = sales * 0.19
        total_assets = (
            net_block + gross_block * 0.05 + investments + receivables
            + inventory + cash + intangibles + goodwill
        )
        total_liabilities = total_assets

        balance.append(
            {
                "ticker": TICKER, "period_label": label, "period_end": end.isoformat(),
                "period_type": "annual",
                "equity_capital": r(SHARES * 1.0), "reserves": r(net_worth - SHARES * 1.0),
                "net_worth": r(net_worth), "borrowings": r(borrowings),
                "current_liabilities": r(current_liabilities),
                "total_liabilities": r(total_liabilities),
                "gross_block": r(gross_block), "accumulated_dep": r(accumulated),
                "net_block": r(net_block), "cwip": r(gross_block * 0.05),
                "investments": r(investments), "intangibles": r(intangibles),
                "goodwill": r(goodwill), "receivables": r(receivables),
                "inventory": r(inventory), "cash": r(cash),
                "total_assets": r(total_assets),
                "contingent_liabilities": r(net_worth * (0.031 + i * 0.001)),
                "receivable_provisions": r(receivables * 0.028),
                "source": "synthetic-generator", "as_of": AS_OF,
            }
        )

        cfo = pat + depreciation - sales * 0.021 + rng.uniform(-3e7, 3e7)
        capex = gross_block * (0.088 + rng.uniform(-0.012, 0.014))
        cashflow.append(
            {
                "ticker": TICKER, "period_label": label, "period_end": end.isoformat(),
                "period_type": "annual",
                "cfo": r(cfo), "cfi": r(-capex - 4e7), "cff": r(-dps * SHARES - 2e7),
                "capex": r(capex), "dividends_paid": r(dps * SHARES),
                "net_cash_flow": r(cfo - capex - dps * SHARES - 6e7),
                "source": "synthetic-generator", "as_of": AS_OF,
            }
        )

        shareholding.append(
            {
                "ticker": TICKER, "period_label": label,
                "promoter_pct": round(54.2 - i * 0.11, 2),
                "promoter_pledge_pct": round(max(0.0, 3.1 - i * 0.2), 2),
                "fii_pct": round(18.4 + i * 0.22, 2),
                "dii_pct": round(14.1 + i * 0.05, 2),
                "public_pct": round(13.3 - i * 0.16, 2),
                "source": "synthetic-generator", "as_of": AS_OF,
            }
        )

    prices, commodity = _series(rng)

    latest_price = prices[-1]["close"]
    return {
        "provider": "synthetic-generator",
        "source_tier": "estimated",
        "as_of": AS_OF,
        "company": {
            "ticker": TICKER, "name": NAME, "market": "US", "exchange": "SYNTHETIC",
            "currency": CURRENCY, "unit_scale": 1.0, "unit_label": CURRENCY,
            "sector": "Industrials", "industry": "Specialty chemicals",
            "face_value": 1.0, "shares_outstanding": SHARES,
            "source": "synthetic-generator", "as_of": AS_OF,
        },
        "snapshot": {
            "ticker": TICKER, "as_of": AS_OF, "price": latest_price,
            "market_cap": round(latest_price * SHARES, 2), "currency": CURRENCY,
            "source": "synthetic-generator",
        },
        "income": income,
        "balance": balance,
        "cashflow": cashflow,
        "prices": prices,
        "shareholding": shareholding,
        "ratios": [],
        "news": _news(),
        "commodity_prices": commodity,
        "gaps": {},
    }


def _series(rng: random.Random):
    """Daily prices, and a weekly crude series the margin is built to react to."""
    prices, commodity = [], []
    start = dt.date(LAST_FY - 10, 8, 20)
    end = dt.date(2026, 8, 14)

    price, oil = 28.0, 72.0
    day = start
    i = 0
    while day <= end:
        if day.weekday() < 5:
            drift = 0.00042
            shock = rng.gauss(0, 0.0132)
            cycle = 0.00028 * math.sin(i / 118.0)
            price *= math.exp(drift + cycle + shock)
            close = round(price, 2)
            spread = abs(rng.gauss(0, 0.006)) * close
            prices.append(
                {
                    "ticker": TICKER, "date": day.isoformat(),
                    "open": round(close - rng.gauss(0, 0.004) * close, 2),
                    "high": round(close + spread, 2),
                    "low": round(close - spread, 2),
                    "close": close, "adj_close": close,
                    "volume": int(abs(rng.gauss(3_100_000, 850_000))),
                    "source": "synthetic-generator", "as_of": AS_OF,
                }
            )
            i += 1
        if day.weekday() == 0:
            oil *= math.exp(rng.gauss(0.0004, 0.026) + 0.0016 * math.sin(i / 96.0))
            commodity.append(
                {
                    "symbol": "CL=F", "date": day.isoformat(), "close": round(oil, 2),
                    "unit": "USD", "currency": "USD",
                    "source": "synthetic-generator", "as_of": AS_OF,
                }
            )
        day += dt.timedelta(days=1)
    return prices, commodity


def _news():
    base = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)
    items = [
        ("Demo Industrials beats quarterly estimates on record specialty volumes", "positive"),
        ("Board approves expansion of the Gulf Coast plant", "positive"),
        ("Regulator opens a routine probe into effluent disclosures", "negative"),
        ("Demo Industrials raises dividend for the fifth straight year", "positive"),
        ("Feedstock costs weigh on margin guidance, company warns", "negative"),
        ("Analyst day scheduled for October", "neutral"),
    ]
    return [
        {
            "ticker": TICKER,
            "published_at": (base - dt.timedelta(days=n * 3)).replace(microsecond=0).isoformat(),
            "headline": headline,
            "url": None,
            "publisher": "Synthetic Newswire",
            "source": "synthetic-generator",
            "as_of": AS_OF,
        }
        for n, (headline, _) in enumerate(items)
    ]


def r(value: float) -> float:
    return round(value, 2)


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "data/samples/demo_industrials.json"
    bundle = build()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=1, sort_keys=True)
    print(
        f"wrote {out}: {len(bundle['income'])} fiscal years, "
        f"{len(bundle['prices'])} sessions, {len(bundle['commodity_prices'])} commodity points"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
