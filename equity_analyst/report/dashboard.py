"""§7 Self-contained HTML dashboard.

One file, no external requests: inline CSS, inline SVG, no JS required. Theme
tokens follow the validated reference palette and are declared for light, the
OS dark preference, and an explicit ``data-theme`` stamp, so the report is
readable wherever it is opened.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence

from . import charts as C
from .charts import esc, fmt

STATUS_ICON = {
    "pass": "✔", "watch": "!", "fail": "✕", "skipped": "–", "unknown": "?",
}
ZONE_LABEL = {
    "buy": "Buy zone",
    "near": "Near the buy line",
    "expensive": "No margin of safety",
    "unknown": "Undetermined",
}
ZONE_STATUS = {
    "buy": "good", "near": "warning", "expensive": "critical", "unknown": "muted",
}


def render(report: Any) -> str:
    company = report.company or {}
    name = company.get("name") or report.config.ticker
    currency = report.currency or ""
    unit = report.unit_label or currency

    body = "".join(
        [
            _header(report, name),
            _verdict_card(report),
            _valuation_section(report, currency),
            _fundamentals_section(report, unit),
            _technical_section(report, currency),
            _linkage_section(report),
            _forensics_section(report),
            _verification_section(report),
            _appendix(report),
            _footer(report),
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(name)} — equity research</title>
<style>{_CSS}</style>
</head>
<body>
<main class="wrap viz-root">{body}</main>
</body>
</html>"""


# -- sections --------------------------------------------------------------


def _header(report: Any, name: str) -> str:
    c = report.company or {}
    snapshot = report.snapshot or {}
    as_of = snapshot.get("as_of") or _dt.datetime.now(_dt.timezone.utc).isoformat()
    bits = [b for b in (c.get("exchange"), c.get("sector"), c.get("industry")) if b]
    return f"""
<header class="pagehead">
  <p class="eyebrow">Equity research · {esc(report.config.market)}</p>
  <h1>{esc(name)} <span class="ticker">{esc(report.config.ticker)}</span></h1>
  <p class="sub">{esc(' · '.join(bits)) or 'Sector not classified'}</p>
  <p class="asof">Data as of {esc(str(as_of)[:19])} UTC · reporting unit {esc(report.unit_label or '—')}</p>
</header>"""


def _verdict_card(report: Any) -> str:
    v = report.valuation
    f = report.forensic
    t = report.technicals
    ver = report.verification
    fund = report.fundamentals

    zone = getattr(v, "zone", "unknown")
    quality = getattr(fund, "quality", {}) or {}
    high_quality = quality.get("high_quality")
    quality_status = "good" if high_quality else "warning" if high_quality is False else "muted"
    quality_text = (
        "Clears 15% on ROE, ROCE and ROIC" if high_quality
        else "Does not clear the 15% gate on all three" if high_quality is False
        else "Quality gate not assessable"
    )

    forensic_flag = getattr(f, "flag", "unknown")
    forensic_status = {
        "pass": "good", "watch": "warning", "fail": "critical", "unknown": "muted"
    }[forensic_flag]

    bias = getattr(t, "bias", "unknown")
    bias_status = {
        "bullish": "good", "bearish": "critical", "neutral": "warning", "unknown": "muted"
    }[bias]

    total = getattr(ver, "total", 0.0)
    band = getattr(ver, "band", "low")

    price = getattr(v, "current_price", None)
    buy_below = getattr(v, "buy_below", None)
    currency = getattr(v, "currency", "") or ""

    tiles = [
        _tile("Valuation verdict", ZONE_LABEL.get(zone, zone), ZONE_STATUS.get(zone, "muted"),
              _zone_detail(v, currency)),
        _tile("Business quality", "High quality" if high_quality else
              "Below gate" if high_quality is False else "Unknown",
              quality_status, quality_text),
        _tile("Forensic screen", forensic_flag.title(), forensic_status,
              f"{len([c for c in (getattr(f, 'checks', []) or []) if c.status not in ('skipped',)])} checks run"
              + (f", {len(getattr(f, 'skipped', []) or [])} skipped" if getattr(f, "skipped", None) else "")),
        _tile("12-month technical bias", bias.title(), bias_status,
              _invalidation_text(t, currency)),
    ]

    return f"""
<section class="verdict">
  <div class="hero">
    <div class="heronum">
      <p class="herolabel">Verification score</p>
      <p class="herovalue status-{esc(band)}">{total:.0f}<span class="pct">%</span></p>
      <p class="herosub">{esc(band)} confidence · §8 weighted rigor score</p>
    </div>
    <div class="heroline">
      <p class="verdictline">{_verdict_sentence(report, price, buy_below, currency)}</p>
    </div>
  </div>
  <div class="tiles">{''.join(tiles)}</div>
</section>"""


def _zone_detail(v: Any, currency: str) -> str:
    buy_below = getattr(v, "buy_below", None)
    price = getattr(v, "current_price", None)
    if buy_below is None or price is None:
        return "No buy-below line could be published."
    gap = (price - buy_below) / buy_below * 100.0
    return (
        f"Buy below {fmt(buy_below, 2)} {currency}; price is "
        f"{abs(gap):.0f}% {'above' if gap > 0 else 'below'} that line."
    )


def _invalidation_text(t: Any, currency: str) -> str:
    inv = getattr(t, "invalidation", {}) or {}
    level = inv.get("level")
    if level is None:
        return "No invalidation level could be set."
    return f"Invalidated on a weekly {inv.get('direction', 'move')} {fmt(level, 2)} {currency}."


def _verdict_sentence(report: Any, price: Any, buy_below: Any, currency: str) -> str:
    v = report.valuation
    zone = getattr(v, "zone", "unknown")
    selected = getattr(v, "selected_value", None)
    if selected is None or price is None:
        return (
            "No intrinsic value could be established from the available data, so no "
            "buy/avoid call is published. See the gaps listed under verification."
        )
    upside = getattr(v, "upside_pct", None)
    lead = {
        "buy": "Trades below the margin-of-safety line",
        "near": "Trades close to the margin-of-safety line",
        "expensive": "Offers no margin of safety at today's price",
        "unknown": "Price zone undetermined",
    }[zone]
    tail = ""
    if getattr(report.forensic, "flag", None) == "fail":
        tail = " A forensic check has failed, which caps this verdict regardless of the valuation."
    return (
        f"{lead}: intrinsic value {fmt(selected, 2)} {currency} "
        f"({getattr(v, 'selected_basis', '')}) against a price of {fmt(price, 2)} "
        f"{currency}"
        + (f", {upside:+.0f}% to the central estimate." if upside is not None else ".")
        + tail
    )


def _tile(label: str, value: str, status: str, detail: str) -> str:
    icon = {"good": "●", "warning": "▲", "critical": "■", "muted": "○"}[status]
    return f"""
<div class="tile">
  <p class="tilelabel">{esc(label)}</p>
  <p class="tilevalue status-{esc(status)}"><span class="ic" aria-hidden="true">{icon}</span>{esc(value)}</p>
  <p class="tiledetail">{esc(detail)}</p>
</div>"""


def _valuation_section(report: Any, currency: str) -> str:
    v = report.valuation
    if v is None:
        return ""
    rows = []
    for m in v.models:
        if m.name.startswith("Buffetology"):
            er = m.expected_return or {}
            hist = er.get("historical")
            sust = er.get("sustainable")
            value_cell = " / ".join(
                f"{x * 100:.1f}%" if x is not None else "n/a" for x in (hist, sust)
            ) + " p.a."
            vs = "expected annual return (historical / sustainable)"
        elif m.value is not None:
            value_cell = f"{fmt(m.value, 2)} {currency}"
            vs = (
                f"{(m.value - v.current_price) / v.current_price * 100:+.0f}%"
                if v.current_price else "n/a"
            )
        else:
            value_cell = "not available"
            vs = esc(m.unavailable_reason or "")
        caveats = "<br>".join(esc(c) for c in m.caveats) or "—"
        rows.append(
            f"<tr><td><strong>{esc(m.name)}</strong><div class='formula'>{esc(m.formula)}</div></td>"
            f"<td class='num'>{value_cell}</td><td>{vs}</td><td class='caveat'>{caveats}</td></tr>"
        )

    if v.buy_below is not None:
        rows.append(
            f"<tr class='mos'><td><strong>Margin of safety @ {v.margin_of_safety:.0%}</strong>"
            f"<div class='formula'>selected IV × {1 - v.margin_of_safety:.2f}</div></td>"
            f"<td class='num'>{fmt(v.buy_below, 2)} {esc(currency)}</td>"
            f"<td>buy-below line</td><td class='caveat'>Selected IV: {esc(v.selected_basis)}</td></tr>"
        )
    if v.current_price is not None:
        rows.append(
            f"<tr class='current'><td><strong>Current price</strong></td>"
            f"<td class='num'>{fmt(v.current_price, 2)} {esc(currency)}</td>"
            f"<td>—</td><td class='caveat'>—</td></tr>"
        )

    divergence = (
        f"<p class='note'>{esc(v.divergence_note)}</p>" if v.divergence_note else ""
    )
    notes = "".join(f"<li>{esc(n)}</li>" for n in (v.notes or []))
    notes_block = f"<ul class='notes'>{notes}</ul>" if notes else ""

    return f"""
<section id="valuation">
  <h2>Valuation</h2>
  {C.football_field(v)}
  <div class="scroll"><table class="valtable">
    <thead><tr><th>Model</th><th>Intrinsic value / share</th><th>vs current price</th><th>Caveats</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
  {divergence}
  {notes_block}
</section>"""


def _fundamentals_section(report: Any, unit: str) -> str:
    f = report.fundamentals
    if f is None or not f.annual:
        return "<section id='fundamentals'><h2>Fundamentals</h2><p class='nodata'>No annual statements were loaded.</p></section>"

    growth = f.growth or {}
    windows = growth.get("windows") or {}
    grows = []
    for w in (10, 7, 5, 3):
        block = windows.get(w)
        if not block:
            grows.append(f"<tr><td>{w}y</td><td colspan='4' class='muted'>insufficient history</td></tr>")
            continue
        grows.append(
            f"<tr><td>{w}y<div class='muted'>{esc(block['from'])}→{esc(block['to'])}</div></td>"
            f"<td class='num'>{fmt(block['sales_cagr'], 1, '%')}</td>"
            f"<td class='num'>{fmt(block['pat_cagr'], 1, '%')}</td>"
            f"<td class='num'>{fmt(block['eps_cagr'], 1, '%')}</td></tr>"
        )

    quality = f.quality or {}
    qrows = []
    for key, label in (("roe", "ROE"), ("roce", "ROCE"), ("roic", "ROIC")):
        block = (quality.get("metrics") or {}).get(key)
        if not isinstance(block, dict):
            qrows.append(f"<tr><td>{label}</td><td colspan='4' class='muted'>not computable</td></tr>")
            continue
        icon = "✔" if block["clears_gate"] else "✕"
        qrows.append(
            f"<tr><td>{label}</td><td class='num'>{fmt(block['median'], 1, '%')}</td>"
            f"<td class='num'>{fmt(block['latest'], 1, '%')}</td>"
            f"<td class='num'>{block['years_above_gate']}/{block['years']}</td>"
            f"<td class='{'status-good' if block['clears_gate'] else 'status-critical'}'>{icon}</td></tr>"
        )

    roiic = f.roiic or {}
    dollar = f.buffett_dollar_test or {}
    eva = f.eva or []
    latest_eva = eva[-1] if eva else None
    vr = f.valuation_ratios or {}

    capital_rows = [
        ("ROIIC (incremental returns)", fmt(roiic.get("roiic"), 1, "%"),
         f"{esc(roiic.get('from') or '')}→{esc(roiic.get('to') or '')}" if roiic.get("roiic") is not None else esc(roiic.get("note") or "")),
        ("Average reinvestment rate",
         fmt((roiic.get("avg_reinvestment_rate") or 0) * 100, 0, "%") if roiic.get("avg_reinvestment_rate") is not None else "n/a",
         "capex ÷ CFO"),
        ("Intrinsic compounding rate",
         fmt((roiic.get("intrinsic_compounding_rate") or 0) * 100, 1, "%") if roiic.get("intrinsic_compounding_rate") is not None else "n/a",
         "ROIIC × reinvestment rate"),
        ("EVA (latest year)",
         f"{fmt(latest_eva['eva'])} {esc(unit)}" if latest_eva else "n/a",
         f"NOPAT − capital employed × {report.config.assumptions.cost_of_capital:.0%}"),
        ("Value per $1 retained",
         fmt(dollar.get("value_per_dollar_retained"), 2) if dollar.get("applicable") else "n/a",
         "Buffett test: ≥ 1.00 passes" if dollar.get("applicable") else esc(dollar.get("reason") or "")),
    ]
    cap_html = "".join(
        f"<tr><td>{esc(a)}</td><td class='num'>{b}</td><td class='muted'>{c}</td></tr>"
        for a, b, c in capital_rows
    )

    ratio_rows = "".join(
        f"<tr><td>{esc(label)}</td><td class='num'>{fmt(vr.get(key), 1)}</td>"
        f"<td class='num'>{fmt(vr.get(alt), 1)}</td></tr>"
        for label, key, alt in (
            ("P/E", "median_pe", "max_pe"),
            ("EV/EBITDA", "median_ev_ebitda", "max_ev_ebitda"),
            ("P/B", "avg_pb", "avg_pb"),
            ("P/S", "avg_ps", "avg_ps"),
            ("P/CF", "avg_pcf", "avg_pcf"),
        )
    )

    margin_note = ""
    if quality.get("margin_trend"):
        margin_note = (
            f"<p class='note'>Operating margin is <strong>{esc(quality['margin_trend'])}</strong>"
            f" ({quality.get('margin_trend_bps', 0):+.0f} bps between the first and second "
            f"half of the record) — "
            f"{'consistent with pricing power' if quality.get('pricing_power') else 'a pricing-power concern'}.</p>"
        )

    return f"""
<section id="fundamentals">
  <h2>Fundamentals</h2>
  {C.revenue_profit_margins(f.annual, f.margins, unit)}
  {margin_note}
  {C.returns_vs_gate(f.returns, report.config.assumptions.quality_return_gate)}
  {C.fcf_and_reinvestment(f.capital_allocation, unit)}

  <div class="grid2">
    <div>
      <h3>Growth <span class="muted">(CAGR)</span></h3>
      <div class="scroll"><table><thead><tr><th>Window</th><th class="num">Sales</th><th class="num">PAT</th><th class="num">EPS</th></tr></thead>
      <tbody>{''.join(grows)}</tbody></table></div>
      <p class="note">Best case {fmt(growth.get('best_case_growth'), 1, '%')} ·
        worst case {fmt(growth.get('worst_case_growth'), 1, '%')} ·
        median {fmt(growth.get('median_growth'), 1, '%')} (year-on-year sales).</p>
    </div>
    <div>
      <h3>Quality gate ({report.config.assumptions.quality_return_gate:.0f}%)</h3>
      <div class="scroll"><table><thead><tr><th>Metric</th><th>Median</th><th>Latest</th><th>Yrs above</th><th></th></tr></thead>
      <tbody>{''.join(qrows)}</tbody></table></div>
    </div>
    <div>
      <h3>Capital allocation</h3>
      <div class="scroll"><table><tbody>{cap_html}</tbody></table></div>
    </div>
    <div>
      <h3>Valuation multiples (history)</h3>
      <div class="scroll"><table><thead><tr><th>Multiple</th><th>Median</th><th>10-yr max</th></tr></thead>
      <tbody>{ratio_rows}</tbody></table></div>
    </div>
  </div>
</section>"""


def _technical_section(report: Any, currency: str) -> str:
    t = report.technicals
    if t is None:
        return ""
    if not t.series:
        note = "; ".join(t.notes) or "no price history"
        return f"<section id='technical'><h2>Technical view</h2><p class='nodata'>{esc(note)}</p></section>"

    rationale = "".join(f"<li>{esc(r)}</li>" for r in t.rationale)
    inv = t.invalidation or {}
    stats = [
        ("Last close", f"{fmt(t.last_close, 2)} {currency}"),
        ("SMA 50 / 200", f"{fmt(t.sma_fast, 2)} / {fmt(t.sma_slow, 2)}"),
        ("Cross state", (t.cross_state or "unknown") + (f" since {t.cross_date}" if t.cross_date else "")),
        ("RSI (14)", fmt(t.rsi, 0)),
        ("MACD / signal", f"{fmt(t.macd, 2)} / {fmt(t.macd_signal, 2)}"),
        ("ATR (14)", f"{fmt(t.atr, 2)} ({fmt(t.atr_pct, 1, '%')} of price)"),
        ("Bollinger (20, 2σ)", f"{fmt(t.bollinger_lower, 2)} – {fmt(t.bollinger_upper, 2)}"),
        ("Volume trend", f"{t.volume_trend or 'n/a'} ({fmt(t.volume_ratio, 2)}× the 100-day base)"),
        ("52-week range", f"{fmt(t.fifty_two_week_low, 2)} – {fmt(t.fifty_two_week_high, 2)}"),
        ("Support", ", ".join(fmt(s, 2) for s in t.support) or "none identified"),
        ("Resistance", ", ".join(fmt(s, 2) for s in t.resistance) or "none identified"),
    ]
    stat_html = "".join(
        f"<tr><td>{esc(a)}</td><td class='num'>{esc(b)}</td></tr>" for a, b in stats
    )
    bias_status = {
        "bullish": "good", "bearish": "critical", "neutral": "warning"
    }.get(t.bias, "muted")
    return f"""
<section id="technical">
  <h2>Technical view — 12-month bias: <span class="status-{esc(bias_status)} biasword">{esc(t.bias)}</span></h2>
  {C.price_chart(t, currency)}
  <div class="grid2">
    <div>
      <h3>Why</h3>
      <ul class="rationale">{rationale}</ul>
      <p class="note"><strong>Invalidation:</strong> {esc(inv.get('note') or '')}
        {('Level ' + fmt(inv.get('level'), 2) + ' ' + currency) if inv.get('level') else ''}</p>
    </div>
    <div>
      <h3>Indicators</h3>
      <div class="scroll"><table><tbody>{stat_html}</tbody></table></div>
    </div>
  </div>
</section>"""


def _news_row(n: Dict[str, Any]) -> str:
    headline = esc(n.get("headline"))
    url = n.get("url")
    if url:
        headline = f'<a href="{esc(url)}" rel="noopener noreferrer" target="_blank">{headline}</a>'
    sentiment = n.get("sentiment") or "neutral"
    status = {"positive": "good", "negative": "critical"}.get(sentiment, "muted")
    return (
        f"<tr><td class='muted'>{esc((n.get('published_at') or '')[:10])}</td>"
        f"<td>{headline}</td>"
        f"<td><span class='status-{status}'>{esc(sentiment)}</span></td>"
        f"<td class='muted'>{esc(n.get('impact_basis') or 'qualitative')}</td></tr>"
    )


def _linkage_section(report: Any) -> str:
    l = report.linkage
    if l is None:
        return ""
    fund = report.fundamentals
    annual = getattr(fund, "annual", []) or []

    charts = "".join(C.commodity_scatter(link, annual) for link in (l.commodities or []))
    link_rows = "".join(
        f"<tr><td><strong>{esc(link.name)}</strong><div class='muted'>{esc(link.symbol)} · {esc(link.role)}</div></td>"
        f"<td class='num'>{fmt(link.correlation, 2)}</td>"
        f"<td class='num'>{fmt(link.margin_sensitivity_bps, 0, ' bps')}</td>"
        f"<td class='num'>{fmt(link.commodity_trend_pct, 1, '%')}</td>"
        f"<td><span class='status-{'good' if link.direction == 'tailwind' else 'critical' if link.direction == 'headwind' else 'muted'}'>"
        f"{esc(link.direction)}</span></td>"
        f"<td class='muted'>{esc(link.basis)} · n={link.observations}</td></tr>"
        for link in (l.commodities or [])
    )
    linkage_table = (
        f"<div class='scroll'><table><thead><tr><th>Commodity</th><th>r vs OPM</th>"
        f"<th>OPM per +10%</th><th>1-yr trend</th><th>Direction</th><th>Basis</th></tr></thead>"
        f"<tbody>{link_rows}</tbody></table></div>"
        if link_rows else "<p class='nodata'>No commodity linkage was established.</p>"
    )
    narratives = "".join(
        f"<li>{esc(link.narrative)}</li>" for link in (l.commodities or []) if link.narrative
    )

    s = l.sentiment_summary or {}
    news_rows = "".join(_news_row(n) for n in (l.news or [])[:15])
    news_block = (
        f"<div class='scroll'><table><thead><tr><th>Date</th><th>Headline</th>"
        f"<th>Sentiment</th><th>Impact basis</th></tr></thead><tbody>{news_rows}</tbody></table></div>"
        if news_rows else "<p class='nodata'>No news items were retrieved.</p>"
    )
    notes = "".join(f"<li>{esc(n)}</li>" for n in (l.notes or []))

    return f"""
<section id="linkage">
  <h2>News &amp; commodity linkage</h2>
  {charts}
  <h3>Commodity sensitivity</h3>
  {linkage_table}
  <ul class="notes">{narratives}{notes}</ul>
  <h3>Recent news — sentiment skew: {esc(s.get('skew', 'n/a'))}
    <span class="muted">({s.get('positive', 0)}+ / {s.get('neutral', 0)}= / {s.get('negative', 0)}−)</span></h3>
  {news_block}
  <p class="note">Sentiment is a transparent keyword classifier for triage, not a
    quantified price-impact model. Impact is left blank unless an event study
    backs it, rather than filled with a plausible-looking number.</p>
</section>"""


def _forensics_section(report: Any) -> str:
    f = report.forensic
    if f is None:
        return ""
    order = {"fail": 0, "watch": 1, "skipped": 2, "pass": 3}
    checks = sorted(f.checks, key=lambda c: order.get(c.status, 4))
    rows = "".join(
        f"<tr class='sev-{esc(c.status)}'>"
        f"<td><span class='ic status-{_sev_status(c.status)}' aria-hidden='true'>{STATUS_ICON.get(c.status, '?')}</span>"
        f"<span class='sr'>{esc(c.status)}</span> {esc(c.title)}</td>"
        f"<td class='num'>{fmt(c.value, 2)}</td>"
        f"<td class='num muted'>{fmt(c.threshold, 2)}</td>"
        f"<td>{esc(c.detail)}</td></tr>"
        for c in checks
    )
    blocks = "".join(
        f"<tr><td>{esc(b['from'])} → {esc(b['to'])}</td>"
        f"<td class='num'>{fmt(b['pat'])}</td><td class='num'>{fmt(b['cfo'])}</td>"
        f"<td class='num'>{fmt(b['ratio'], 2)}</td></tr>"
        for b in (f.blocks or [])
    )
    block_table = (
        f"<h3>Cash conversion in 3-year blocks</h3><div class='scroll'><table>"
        f"<thead><tr><th>Block</th><th>Cumulative PAT</th><th>Cumulative CFO</th><th>CFO/PAT</th></tr></thead>"
        f"<tbody>{blocks}</tbody></table></div>"
        if blocks else ""
    )
    cap = ""
    if f.flag == "fail":
        cap = (
            "<p class='callout critical'>A forensic check has <strong>failed</strong>. "
            "This caps the verdict and the verification score regardless of how cheap "
            "the stock looks.</p>"
        )
    elif f.flag == "unknown":
        cap = (
            "<p class='callout warning'>Too few forensic checks could run to certify "
            "accounting integrity. Treat the verdict as provisional.</p>"
        )
    return f"""
<section id="forensics">
  <h2>Forensic screen — flag: <span class="status-{_flag_status(f.flag)} biasword">{esc(f.flag)}</span>
    <span class="muted">score {fmt(f.score, 0)}/100</span></h2>
  {cap}
  <div class="scroll"><table class="checks">
    <thead><tr><th>Check</th><th>Value</th><th>Threshold</th><th>Reading</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  {block_table}
  <p class="note">Skipped checks are counted as unknown, never as a pass, and they
    reduce the data-completeness component of the verification score.</p>
</section>"""


def _sev_status(status: str) -> str:
    return {"pass": "good", "watch": "warning", "fail": "critical"}.get(status, "muted")


def _flag_status(flag: str) -> str:
    return {"pass": "good", "watch": "warning", "fail": "critical"}.get(flag, "muted")


def _verification_section(report: Any) -> str:
    v = report.verification
    if v is None:
        return ""
    rows = "".join(
        f"<tr><td>{esc(s.label)}</td><td class='num'>{s.score * 100:.0f}%</td>"
        f"<td class='num muted'>{s.weight:.2f}</td>"
        f"<td class='num'>{s.contribution * 100:.1f}</td>"
        f"<td>{esc(s.detail)}</td></tr>"
        for s in v.subscores
    )
    evidence = "".join(
        f"<li><strong>{esc(s.label)}:</strong> {esc('; '.join(s.evidence))}</li>"
        for s in v.subscores if s.evidence
    )
    caps = "".join(f"<li>{esc(c)}</li>" for c in (v.caps_applied or []))
    caps_block = f"<div class='callout warning'><strong>Caps applied</strong><ul>{caps}</ul></div>" if caps else ""
    warnings = "".join(f"<li>{esc(w)}</li>" for w in (report.warnings or []))
    warn_block = f"<h3>Data gaps recorded this run</h3><ul class='notes'>{warnings}</ul>" if warnings else ""
    return f"""
<section id="verification">
  <h2>Verification score — {v.total:.1f}%</h2>
  <p class="formula">Verification = {esc(v.formula)}</p>
  <div class="scroll"><table>
    <thead><tr><th>Component</th><th>Sub-score</th><th>Weight</th><th>Contribution</th><th>Basis</th></tr></thead>
    <tbody>{rows}</tbody>
    <tfoot><tr><th>Total</th><th></th><th></th><th class="num">{v.total:.1f}</th><th></th></tr></tfoot>
  </table></div>
  {caps_block}
  <details class="tableview"><summary>evidence behind each sub-score</summary>
    <ul class="notes">{evidence}</ul></details>
  {warn_block}
</section>"""


def _appendix(report: Any) -> str:
    db = report.db
    assumptions = db.dicts(
        "SELECT key, value, unit, rationale, overridden FROM assumption WHERE run_id = ? ORDER BY key",
        (report.config.run_id,),
    )
    arows = "".join(
        f"<tr><td>{esc(a['key'])}</td><td class='num'>{fmt(a['value'], 3)}</td>"
        f"<td class='muted'>{esc(a['unit'])}</td>"
        f"<td>{esc(a['rationale'])}</td>"
        f"<td>{'overridden' if a['overridden'] else 'default'}</td></tr>"
        for a in assumptions
    )
    coverage = db.dicts(
        "SELECT domain, present, source, source_tier, as_of, note FROM data_coverage "
        "WHERE run_id = ? ORDER BY present, domain",
        (report.config.run_id,),
    )
    crows = "".join(
        f"<tr><td>{esc(c['domain'])}</td>"
        f"<td class='status-{'good' if c['present'] else 'critical'}'>{'present' if c['present'] else 'missing'}</td>"
        f"<td>{esc(c['source'])}</td><td class='muted'>{esc(c['source_tier'] or '—')}</td>"
        f"<td class='muted'>{esc((c['as_of'] or '')[:19])}</td><td class='muted'>{esc(c['note'] or '')}</td></tr>"
        for c in coverage
    )
    queries = "".join(
        f"<details class='sqlblock'><summary>{esc(q['name'])}"
        f"<span class='muted'> — {esc(q['purpose'])}</span></summary>"
        f"<pre><code>{esc(q['sql'])}</code></pre></details>"
        for q in report.query_log.entries
    )
    return f"""
<section id="appendix">
  <h2>Appendix</h2>
  <h3>Assumptions used</h3>
  <div class="scroll"><table><thead><tr><th>Key</th><th>Value</th><th>Unit</th><th>Rationale</th><th>Status</th></tr></thead>
  <tbody>{arows}</tbody></table></div>

  <h3>Sources &amp; coverage</h3>
  <div class="scroll"><table><thead><tr><th>Domain</th><th>State</th><th>Source</th><th>Tier</th><th>As of</th><th>Note</th></tr></thead>
  <tbody>{crows}</tbody></table></div>

  <h3>SQL executed ({len(report.query_log.entries)} queries)</h3>
  {queries}

  <details class="sqlblock"><summary>Full schema (CREATE TABLE / CREATE VIEW)</summary>
    <pre><code>{esc(report.db.schema_sql)}</code></pre></details>
</section>"""


def _footer(report: Any) -> str:
    return f"""
<footer class="pagefoot">
  <p class="disclaimer"><strong>This is research and education for individual
    investors, not personalised investment advice.</strong> No figure here is a
    recommendation to buy or sell, and past results do not predict future returns.</p>
  <p class="muted">Generated by equity-analyst {esc(getattr(report.config, 'engine_version', '')) or ''}
    for {esc(report.config.ticker)} ({esc(report.config.market)}) ·
    {esc(_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat())} UTC ·
    provider: {esc(getattr(report.provider_result, 'provider', 'unknown'))}</p>
</footer>"""


# -- styles ----------------------------------------------------------------

_CSS = """
*,*::before,*::after{box-sizing:border-box}
/* Tokens live on :root, not on the .viz-root wrapper. Custom properties only
   inherit downward, so declaring them on <main> leaves <body> unable to see
   them -- which silently left the page plane and body ink un-themed in dark
   mode while the cards inside themed correctly. */
:root{
  color-scheme:light;
  --surface-1:#fcfcfb; --plane:#f9f9f7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --baseline:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a;
  --status-good:#0ca30c; --status-warning:#fab219;
  --status-serious:#ec835a; --status-critical:#d03b3b;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --surface-1:#1a1a19; --plane:#0d0d0d;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --surface-1:#1a1a19; --plane:#0d0d0d;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,0.10);
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
}
html,body{margin:0;padding:0}
body{background:var(--plane);color:var(--text-primary);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:1.9rem;margin:.1em 0 .15em;font-weight:650;letter-spacing:-.01em}
h2{font-size:1.28rem;margin:2.4em 0 .7em;font-weight:620;letter-spacing:-.005em}
h3{font-size:1rem;margin:1.5em 0 .5em;font-weight:620}
p{margin:.45em 0}
a{color:var(--series-1)}
section{border-top:1px solid var(--border);padding-top:6px}
section:first-of-type{border-top:none}

.pagehead{border:none;padding:0 0 4px}
.eyebrow{text-transform:uppercase;letter-spacing:.09em;font-size:.7rem;
  color:var(--muted);font-weight:640;margin:0}
.ticker{font-size:1rem;color:var(--text-secondary);font-weight:500;
  border:1px solid var(--border);border-radius:6px;padding:2px 8px;vertical-align:middle}
.sub{color:var(--text-secondary)}
.asof{color:var(--muted);font-size:.8rem}

/* verdict */
.verdict{border:none;margin-top:18px}
.hero{background:var(--surface-1);border:1px solid var(--border);border-radius:14px;
  padding:22px 24px;display:flex;gap:28px;align-items:center;flex-wrap:wrap}
.heronum{min-width:180px}
.herolabel{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;
  color:var(--muted);font-weight:640;margin:0}
.herovalue{font-size:3.4rem;line-height:1;margin:.06em 0;font-weight:660}
.herovalue .pct{font-size:1.4rem;margin-left:2px;color:var(--text-secondary)}
.herosub{font-size:.8rem;color:var(--text-secondary);margin:0}
.heroline{flex:1;min-width:280px}
.verdictline{font-size:1.06rem;line-height:1.5;margin:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(212px,1fr));gap:12px;margin-top:12px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tilelabel{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);font-weight:640;margin:0}
.tilevalue{font-size:1.16rem;font-weight:640;margin:.22em 0}
.tiledetail{font-size:.8rem;color:var(--text-secondary);margin:0}
.ic{margin-right:7px;font-size:.82em}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}

.status-good{color:var(--status-good)}
.status-warning{color:#8a6100}
.status-critical{color:var(--status-critical)}
.status-muted{color:var(--muted)}
.status-high{color:var(--status-good)}
.status-moderate{color:#8a6100}
.status-low{color:var(--status-critical)}
:root[data-theme="dark"] .status-warning,
:root[data-theme="dark"] .status-moderate{color:var(--status-warning)}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]) .status-warning,
  :root:not([data-theme="light"]) .status-moderate{color:var(--status-warning)}
}
.biasword{text-transform:capitalize}

/* charts */
.chart{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;
  margin:16px 0;padding:16px 18px 12px}
.chart svg{width:100%;height:auto;display:block;overflow:visible}
.fig-head h3{margin:0 0 2px;font-size:1rem}
.fig-sub{font-size:.82rem;color:var(--text-secondary);margin:0 0 8px}
.fig-note{font-size:.78rem;color:var(--muted);margin:8px 0 0}
.chart.empty .nodata{font-size:.86rem;color:var(--muted);margin:6px 0 2px}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:2px 0 10px}
.key{display:inline-flex;align-items:center;gap:7px;font-size:.8rem;color:var(--text-secondary)}
.key i{width:11px;height:11px;border-radius:3px;display:inline-block}

svg .grid{stroke:var(--grid);stroke-width:1}
svg .baseline{stroke:var(--baseline);stroke-width:1}
svg .tick{fill:var(--muted);font-size:10.5px;font-variant-numeric:tabular-nums}
svg .axtitle{fill:var(--text-secondary);font-size:11px;font-weight:600}
svg .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .line.price{stroke-width:2}
svg .dot{stroke:var(--surface-1);stroke-width:2}
svg .bar{stroke:var(--surface-1);stroke-width:2}
/* Data labels sit above gridlines and marker rules; the surface-coloured
   halo keeps them legible where they cross one, instead of a boxed label. */
svg .dlabel{fill:var(--text-primary);font-size:11px;font-weight:620;
  paint-order:stroke;stroke:var(--surface-1);stroke-width:3px;stroke-linejoin:round}
svg .markerlabel{paint-order:stroke;stroke:var(--surface-1);stroke-width:3px;
  stroke-linejoin:round}
svg .threshold{stroke:var(--status-critical);stroke-width:1.5;stroke-dasharray:5 4}
svg .thresholdlabel{fill:var(--status-critical);font-size:10.5px;font-weight:620}
svg .srline{stroke:var(--baseline);stroke-width:1}
svg .srlabel{fill:var(--muted);font-size:10px}
svg .rsiband{fill:var(--grid);opacity:.45}
svg .endpoint{fill:var(--series-1);stroke:var(--surface-1);stroke-width:2}
svg .fitline{stroke:var(--text-secondary);stroke-width:1.5;stroke-dasharray:5 4}
svg .scatter{stroke:var(--surface-1);stroke-width:2}
svg .ffname{fill:var(--text-primary);font-size:12px;font-weight:600}
svg .ffcaveat{fill:var(--muted);font-size:9.5px}
svg .marker-price{stroke:var(--text-primary);stroke-width:2}
svg .marker-mos{stroke:var(--status-good);stroke-width:2;stroke-dasharray:5 4}
svg .markerlabel{font-size:10.5px;font-weight:620}
/* Marker *lines* stroke in their own colour; marker *labels* reuse that colour
   as fill and take the surface halo instead, so they stay readable over grid. */
svg text.marker-price{fill:var(--text-primary);stroke:var(--surface-1)}
svg text.marker-mos{fill:var(--status-good);stroke:var(--surface-1)}

/* tables */
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.86rem;margin:.35em 0}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top}
thead th{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);font-weight:640;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
.muted{color:var(--muted)}
.valtable .formula{font-size:.72rem;color:var(--muted);margin-top:3px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.valtable .caveat{font-size:.78rem;color:var(--text-secondary);max-width:320px}
tr.mos td{background:color-mix(in srgb,var(--status-good) 8%,transparent)}
tr.current td{font-weight:620}
.checks .sev-fail td{background:color-mix(in srgb,var(--status-critical) 9%,transparent)}
.checks .sev-watch td{background:color-mix(in srgb,var(--status-warning) 12%,transparent)}
.tableview{margin-top:10px}
.tableview summary{cursor:pointer;font-size:.78rem;color:var(--text-secondary);
  padding:4px 0;user-select:none}
.tableview summary:hover{color:var(--text-primary)}

.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:8px 28px}
.note{font-size:.82rem;color:var(--text-secondary)}
.notes{font-size:.84rem;color:var(--text-secondary);padding-left:1.15em;margin:.4em 0}
.notes li{margin:.25em 0}
.rationale{font-size:.88rem;padding-left:1.15em}
.rationale li{margin:.3em 0}
.nodata{color:var(--muted);font-size:.88rem}
.callout{border-radius:10px;padding:11px 15px;font-size:.86rem;margin:12px 0;
  border:1px solid var(--border)}
.callout.critical{background:color-mix(in srgb,var(--status-critical) 10%,transparent);
  border-color:var(--status-critical)}
.callout.warning{background:color-mix(in srgb,var(--status-warning) 14%,transparent);
  border-color:var(--status-warning)}
.callout ul{margin:.4em 0;padding-left:1.15em}

.formula{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.8rem;
  color:var(--text-secondary);background:var(--surface-1);border:1px solid var(--border);
  border-radius:8px;padding:9px 12px;overflow-x:auto}
.sqlblock{border:1px solid var(--border);border-radius:9px;margin:7px 0;background:var(--surface-1)}
.sqlblock summary{cursor:pointer;padding:8px 13px;font-size:.83rem;font-weight:600;user-select:none}
.sqlblock pre{margin:0;padding:0 13px 13px;overflow-x:auto}
.sqlblock code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.76rem;
  color:var(--text-secondary);white-space:pre}

.pagefoot{margin-top:44px;border-top:1px solid var(--border);padding-top:16px}
.disclaimer{font-size:.85rem;color:var(--text-secondary)}
.pagefoot .muted{font-size:.75rem}
@media print{.tableview[open] summary{display:none}.sqlblock{break-inside:avoid}}
"""
