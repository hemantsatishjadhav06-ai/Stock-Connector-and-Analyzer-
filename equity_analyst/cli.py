"""Command-line entry point.

    equity-analyst RELIANCE.NS --market India --out reports/reliance.html
    equity-analyst AAPL --save-bundle data/aapl.json
    equity-analyst AAPL --offline data/aapl.json --no-network
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .config import Assumptions, RunConfig
from .pipeline import run
from .report import render


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="equity-analyst",
        description=(
            "Autonomous equity research: screener -> SQL -> fundamental, forensic, "
            "technical and commodity analysis -> four valuation models -> a "
            "chart-first HTML report carrying an explicit verification score."
        ),
    )
    p.add_argument("ticker", help="e.g. AAPL, RELIANCE.NS, NESN.SW")
    p.add_argument("--market", default="US", help="US / India / Europe / ... (default: US)")
    p.add_argument("--name", dest="company_name", help="override the company name")
    p.add_argument("--out", "-o", help="write the HTML report here")
    p.add_argument("--db", default=None, help="persist the SQL layer to this sqlite file")
    p.add_argument("--horizon", type=int, default=10, help="intrinsic-value horizon in years")
    p.add_argument("--price-years", type=int, default=10, help="years of price history to pull")
    p.add_argument(
        "--commodity", action="append", default=[], dest="commodities",
        help="force a commodity linkage, e.g. --commodity CL=F (repeatable)",
    )
    p.add_argument("--offline", dest="offline_file", help="replay a saved JSON bundle")
    p.add_argument("--save-bundle", help="save the raw pull to JSON for later replay")
    p.add_argument("--no-network", action="store_true", help="never touch the network")

    g = p.add_argument_group("assumption overrides (§6)")
    g.add_argument("--fcf-growth", type=float, help="FCF growth, years 1-10 (default 0.20)")
    g.add_argument("--discount-rate", type=float, help="default 0.07")
    g.add_argument("--terminal-growth", type=float, help="default 0.02")
    g.add_argument("--sustainable-pe", type=float, help="default 20")
    g.add_argument("--cost-of-capital", type=float, help="EVA charge, default 0.10")
    g.add_argument("--margin-of-safety", type=float, help="default 0.50")
    return p


def _assumptions(args: argparse.Namespace) -> Assumptions:
    a = Assumptions()
    for attr, key in (
        ("fcf_growth", "fcf_growth"),
        ("discount_rate", "discount_rate"),
        ("terminal_growth", "terminal_growth"),
        ("sustainable_pe", "avg_sustainable_pe"),
        ("cost_of_capital", "cost_of_capital"),
        ("margin_of_safety", "margin_of_safety"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(a, key, value)
    return a


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    config = RunConfig(
        ticker=args.ticker,
        market=args.market,
        company_name=args.company_name,
        horizon_years=args.horizon,
        db_path=args.db or ":memory:",
        output_path=args.out,
        assumptions=_assumptions(args),
        offline_file=args.offline_file,
        commodities=args.commodities,
        price_years=args.price_years,
        allow_network=not args.no_network,
    )

    report = run(config)

    if args.save_bundle:
        from .providers import save_bundle

        os.makedirs(os.path.dirname(os.path.abspath(args.save_bundle)), exist_ok=True)
        save_bundle(report.provider_result, args.save_bundle)
        print(f"bundle saved  {args.save_bundle}", file=sys.stderr)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(render(report))
        print(f"report written {args.out}", file=sys.stderr)

    print(summarise(report))
    # A failed forensic screen is a meaningful non-zero exit for automation.
    return 2 if getattr(report.forensic, "flag", None) == "fail" else 0


def summarise(report) -> str:
    """Terminal verdict card -- the same call the HTML leads with."""
    v, f, t, ver = report.valuation, report.forensic, report.technicals, report.verification
    company = report.company or {}
    currency = report.currency
    name = company.get("name") or report.config.ticker

    def money(x):
        return "n/a" if x is None else f"{x:,.2f} {currency}"

    lines = [
        "",
        f"  {name}  ({report.config.ticker} · {report.config.market})",
        "  " + "─" * 64,
        f"  Verification score   {getattr(ver, 'total', 0):.1f}%  ({getattr(ver, 'band', 'low')} confidence)",
        f"  Valuation verdict    {getattr(v, 'zone', 'unknown')}",
        f"  Forensic flag        {getattr(f, 'flag', 'unknown')}  (score {getattr(f, 'score', None) or 0:.0f}/100)",
        f"  Technical bias       {getattr(t, 'bias', 'unknown')}",
        "  " + "─" * 64,
    ]
    for m in getattr(v, "models", []) or []:
        if m.name.startswith("Buffetology") and m.expected_return:
            parts = [
                f"{k} {x * 100:.1f}%" for k, x in m.expected_return.items() if x is not None
            ]
            lines.append(f"  {m.name:<28} {', '.join(parts) or 'n/a'} p.a.")
        elif m.value is not None:
            lines.append(f"  {m.name:<28} {money(m.value)}")
        else:
            lines.append(f"  {m.name:<28} unavailable — {m.unavailable_reason}")
    lines += [
        "  " + "─" * 64,
        f"  Selected intrinsic value  {money(getattr(v, 'selected_value', None))}",
        f"  Buy below (50% MoS)       {money(getattr(v, 'buy_below', None))}",
        f"  Current price             {money(getattr(v, 'current_price', None))}",
    ]
    breaches = [c for c in (getattr(f, "breaches", []) or [])]
    if breaches:
        lines.append("")
        lines.append("  Forensic breaches:")
        for c in breaches[:6]:
            lines.append(f"    [{c.status}] {c.title} — {c.detail}")
    if report.warnings:
        lines.append("")
        lines.append("  Data gaps (these lower the verification score, nothing was guessed):")
        for w in report.warnings[:8]:
            lines.append(f"    · {w}")
    lines.append("")
    lines.append("  Research and education for individual investors, not investment advice.")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
