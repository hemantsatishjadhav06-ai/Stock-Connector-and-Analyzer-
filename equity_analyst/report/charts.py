"""§7 Hand-rolled SVG chart toolkit.

Self-contained by design: no chart library, no CDN, no JS required. Charts are
plain SVG that inherits theme colours from CSS custom properties, so light and
dark mode are one variable swap.

Design rules this module enforces (see docs/METHODOLOGY.md § Charts):

* **No dual-axis plots.** Where the reference layout asks for a currency series
  and a percentage series "overlaid", they are drawn as two stacked panels
  sharing one x-axis. Two y-scales on one plot invent a correlation that is not
  in the data.
* **Categorical hue by slot, never by rank.** Series keep their colour when the
  set changes.
* **Every chart ships a table view.** It is the accessible twin and the relief
  for the one palette slot that sits below 3:1 on the light surface.
* **Selective direct labels**, hairline grid, thin marks, 2px surface gaps.
"""

from __future__ import annotations

import html
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

W = 760           # viewBox width; the SVG scales to its container
PAD_L = 62
PAD_R = 22
PAD_T = 20
PAD_B = 40

Number = Optional[float]


# -- primitives ------------------------------------------------------------


def esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def fmt(value: Number, dp: int = 1, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if abs(value) >= 1e12:
        return f"{value / 1e12:,.{dp}f}T{suffix}"
    if abs(value) >= 1e9:
        return f"{value / 1e9:,.{dp}f}B{suffix}"
    if abs(value) >= 1e7:
        return f"{value / 1e6:,.{dp}f}M{suffix}"
    if abs(value) >= 1e4:
        return f"{value:,.0f}{suffix}"
    return f"{value:,.{dp}f}{suffix}"


def _nice_ticks(lo: float, hi: float, count: int = 5) -> List[float]:
    """Human-readable tick positions spanning [lo, hi]."""
    if hi == lo:
        hi = lo + 1.0
    raw = (hi - lo) / max(1, count)
    magnitude = 10 ** math.floor(math.log10(abs(raw))) if raw else 1
    for multiple in (1, 2, 2.5, 5, 10):
        step = magnitude * multiple
        if step >= raw:
            break
    start = math.floor(lo / step) * step
    ticks, value = [], start
    while value <= hi + step * 0.5:
        ticks.append(round(value, 10))
        value += step
    # Keep only ticks inside the plotted range. The first candidate is always
    # <= lo by construction, and drawing it would place a gridline and its
    # label outside the panel, where they spill over whatever sits below.
    return [t for t in ticks if lo <= t <= hi]


def _bounds(values: Sequence[Number], include_zero: bool = True) -> Tuple[float, float]:
    clean = [v for v in values if v is not None]
    if not clean:
        return 0.0, 1.0
    lo, hi = min(clean), max(clean)
    if include_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if lo == hi:
        pad = abs(lo) * 0.1 or 1.0
        return lo - pad, hi + pad
    span = hi - lo
    return lo - span * 0.08, hi + span * 0.08


def _figure(
    title: str, svg: str, caption: str, table: str, subtitle: str = ""
) -> str:
    sub = f'<p class="fig-sub">{esc(subtitle)}</p>' if subtitle else ""
    return f"""
<figure class="chart">
  <figcaption class="fig-head"><h3>{esc(title)}</h3>{sub}</figcaption>
  {svg}
  <p class="fig-note">{esc(caption)}</p>
  {table}
</figure>"""


def _table_view(headers: Sequence[str], rows: Sequence[Sequence[str]], label: str = "table view") -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    return (
        f'<details class="tableview"><summary>{esc(label)}</summary>'
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></details>"
    )


def _grid_and_axis(
    x0: float, x1: float, y_of, ticks: Sequence[float], suffix: str = ""
) -> str:
    parts = []
    for t in ticks:
        y = y_of(t)
        parts.append(
            f'<line class="grid" x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{x0 - 8}" y="{y + 3.5:.1f}" text-anchor="end">'
            f"{esc(fmt(t, 0 if abs(t) >= 100 else 1, suffix))}</text>"
        )
    return "".join(parts)


def _x_labels(labels: Sequence[str], x_of, y: float, max_labels: int = 12) -> str:
    if not labels:
        return ""
    step = max(1, math.ceil(len(labels) / max_labels))
    out = []
    for i, label in enumerate(labels):
        if i % step and i != len(labels) - 1:
            continue
        out.append(
            f'<text class="tick" x="{x_of(i):.1f}" y="{y}" text-anchor="middle">'
            f"{esc(label)}</text>"
        )
    return "".join(out)


def _empty(title: str, reason: str) -> str:
    return (
        f'<figure class="chart empty"><figcaption class="fig-head"><h3>{esc(title)}</h3>'
        f'</figcaption><p class="nodata">Not charted: {esc(reason)}</p></figure>'
    )


def _legend(entries: Sequence[Tuple[str, str]]) -> str:
    """entries: [(label, css-var-name)] -- always present for >= 2 series."""
    items = "".join(
        f'<span class="key"><i style="background:var({var})"></i>{esc(label)}</span>'
        for label, var in entries
    )
    return f'<div class="legend">{items}</div>'


# =====================================================================
# 1. Revenue & profit with margins -- two stacked panels, ONE x-axis
# =====================================================================


def revenue_profit_margins(
    annual: List[Dict[str, Any]], margins: List[Dict[str, Any]], unit: str
) -> str:
    title = "Revenue, profit and margins"
    if not annual:
        return _empty(title, "no annual statements were loaded")

    labels = [r["period_label"] for r in annual]
    sales = [r.get("sales") for r in annual]
    pat = [r.get("pat") for r in annual]
    margin_by = {m["period_label"]: m for m in margins}
    opm = [(margin_by.get(l) or {}).get("opm") for l in labels]
    npm = [(margin_by.get(l) or {}).get("npm") for l in labels]

    top_h, gap, bot_h = 190, 34, 130
    height = PAD_T + top_h + gap + bot_h + PAD_B
    x1 = W - PAD_R
    n = len(labels)
    slot = (x1 - PAD_L) / max(1, n)

    def x_of(i: float) -> float:
        return PAD_L + slot * (i + 0.5)

    # --- panel 1: sales & profit bars (currency) ---
    lo, hi = _bounds(list(sales) + list(pat))
    top_y0, top_y1 = PAD_T, PAD_T + top_h

    def y_top(v: float) -> float:
        return top_y1 - (v - lo) / (hi - lo) * (top_h)

    parts = [_grid_and_axis(PAD_L, x1, y_top, _nice_ticks(lo, hi, 4))]
    bar_w = max(3.0, slot * 0.34)          # thin marks
    zero = y_top(0)
    for i, (s, p) in enumerate(zip(sales, pat)):
        cx = x_of(i)
        for value, offset, var, name in (
            (s, -bar_w / 2 - 1, "--series-1", "Revenue"),
            (p, bar_w / 2 + 1, "--series-2", "Net profit"),
        ):
            if value is None:
                continue
            y = y_top(value)
            top, h = (y, zero - y) if value >= 0 else (zero, y - zero)
            parts.append(
                f'<rect class="bar" x="{cx + offset - bar_w / 2:.1f}" y="{top:.1f}" '
                f'width="{bar_w:.1f}" height="{max(1.0, h):.1f}" rx="3" '
                f'fill="var({var})"><title>{esc(labels[i])} · {name}: '
                f"{esc(fmt(value))} {esc(unit)}</title></rect>"
            )
    # direct-label the latest revenue bar only
    if sales and sales[-1] is not None:
        parts.append(
            f'<text class="dlabel" x="{x_of(n - 1) - bar_w / 2 - 1:.1f}" '
            f'y="{y_top(sales[-1]) - 7:.1f}" text-anchor="middle">'
            f"{esc(fmt(sales[-1]))}</text>"
        )

    # --- panel 2: margin lines (%) ---
    mlo, mhi = _bounds(opm + npm)
    bot_y0 = top_y1 + gap
    bot_y1 = bot_y0 + bot_h

    def y_bot(v: float) -> float:
        return bot_y1 - (v - mlo) / (mhi - mlo) * bot_h

    parts.append(_grid_and_axis(PAD_L, x1, y_bot, _nice_ticks(mlo, mhi, 3), "%"))
    for series, var, name in ((opm, "--series-1", "OPM"), (npm, "--series-3", "NPM")):
        pts = [(x_of(i), y_bot(v)) for i, v in enumerate(series) if v is not None]
        if len(pts) < 2:
            continue
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<path class="line" d="{d}" stroke="var({var})"/>')
        for i, v in enumerate(series):
            if v is None:
                continue
            parts.append(
                f'<circle class="dot" cx="{x_of(i):.1f}" cy="{y_bot(v):.1f}" r="4.5" '
                f'fill="var({var})"><title>{esc(labels[i])} · {name}: {v:.1f}%</title></circle>'
            )
        last = next((i for i in range(n - 1, -1, -1) if series[i] is not None), None)
        if last is not None:
            parts.append(
                f'<text class="dlabel" x="{x_of(last):.1f}" y="{y_bot(series[last]) - 9:.1f}" '
                f'text-anchor="middle">{name} {series[last]:.1f}%</text>'
            )

    parts.append(
        f'<text class="axtitle" x="{PAD_L}" y="{top_y0 - 6}">Revenue &amp; net profit ({esc(unit)})</text>'
    )
    parts.append(f'<text class="axtitle" x="{PAD_L}" y="{bot_y0 - 8}">Operating &amp; net margin (%)</text>')
    parts.append(_x_labels(labels, x_of, height - PAD_B + 18))

    svg = (
        f'<svg viewBox="0 0 {W} {height}" role="img" '
        f'aria-label="Revenue and net profit bars above operating and net margin lines">'
        + "".join(parts)
        + "</svg>"
    )
    legend = _legend(
        [("Revenue", "--series-1"), ("Net profit", "--series-2"), ("NPM", "--series-3")]
    )
    table = _table_view(
        ["Fiscal year", f"Revenue ({unit})", f"Net profit ({unit})", "OPM %", "NPM %"],
        [
            [labels[i], fmt(sales[i]), fmt(pat[i]),
             fmt(opm[i], 1), fmt(npm[i], 1)]
            for i in range(n)
        ],
    )
    return _figure(
        title,
        legend + svg,
        "Two panels share one x-axis rather than sharing a y-axis: currency and "
        "percentage on a single plot would imply a scale alignment that does not exist.",
        table,
        "Margins that hold or widen while revenue grows are the observable footprint of pricing power.",
    )


# =====================================================================
# 2. ROE / ROCE / ROIC vs the 15% quality line
# =====================================================================


def returns_vs_gate(returns: List[Dict[str, Any]], gate: float) -> str:
    title = "Returns on capital vs the quality gate"
    usable = [r for r in returns if any(r.get(k) is not None for k in ("roe", "roce", "roic"))]
    if not usable:
        return _empty(title, "returns could not be computed from the loaded data")

    labels = [r["period_label"] for r in usable]
    series = {k: [r.get(k) for r in usable] for k in ("roe", "roce", "roic")}
    height = 300
    x1, y1 = W - PAD_R, height - PAD_B
    plot_h = y1 - PAD_T
    n = len(labels)
    slot = (x1 - PAD_L) / max(1, n)

    def x_of(i: float) -> float:
        return PAD_L + slot * (i + 0.5)

    everything = [v for vals in series.values() for v in vals] + [gate]
    lo, hi = _bounds(everything)

    def y_of(v: float) -> float:
        return y1 - (v - lo) / (hi - lo) * plot_h

    parts = [_grid_and_axis(PAD_L, x1, y_of, _nice_ticks(lo, hi, 5), "%")]

    # The gate is a threshold, so it is the one dashed rule on the page.
    gy = y_of(gate)
    parts.append(
        f'<line class="threshold" x1="{PAD_L}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}"/>'
    )
    # Anchored left: the right edge is where the series' direct labels live.
    parts.append(
        f'<text class="thresholdlabel" x="{PAD_L + 4}" y="{gy - 7:.1f}" text-anchor="start">'
        f"quality gate {gate:.0f}%</text>"
    )

    palette = {"roe": "--series-1", "roce": "--series-2", "roic": "--series-3"}
    names = {"roe": "ROE", "roce": "ROCE", "roic": "ROIC"}
    for key, var in palette.items():
        values = series[key]
        pts = [(x_of(i), y_of(v)) for i, v in enumerate(values) if v is not None]
        if len(pts) < 2:
            continue
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<path class="line" d="{d}" stroke="var({var})"/>')
        for i, v in enumerate(values):
            if v is None:
                continue
            parts.append(
                f'<circle class="dot" cx="{x_of(i):.1f}" cy="{y_of(v):.1f}" r="4.5" '
                f'fill="var({var})"><title>{esc(labels[i])} · {names[key]}: {v:.1f}%'
                f"</title></circle>"
            )
        last = next((i for i in range(n - 1, -1, -1) if values[i] is not None), None)
        if last is not None:
            parts.append(
                f'<text class="dlabel" x="{x_of(last) + 6:.1f}" y="{y_of(values[last]) + 3.5:.1f}" '
                f'text-anchor="start">{names[key]}</text>'
            )

    parts.append(_x_labels(labels, x_of, height - PAD_B + 18))
    svg = (
        f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="ROE, ROCE and ROIC '
        f'plotted against a {gate:.0f} percent quality gate">' + "".join(parts) + "</svg>"
    )
    legend = _legend([("ROE", "--series-1"), ("ROCE", "--series-2"), ("ROIC", "--series-3")])
    table = _table_view(
        ["Fiscal year", "ROE %", "ROCE %", "ROIC %"],
        [
            [labels[i], fmt(series["roe"][i]), fmt(series["roce"][i]), fmt(series["roic"][i])]
            for i in range(n)
        ],
    )
    return _figure(
        title, legend + svg,
        f"A business clearing {gate:.0f}% on all three measures across most years is "
        f"flagged high-quality; one metric clearing alone is not enough.",
        table,
    )


# =====================================================================
# 3. FCF and reinvestment rate -- stacked panels, one x-axis
# =====================================================================


def fcf_and_reinvestment(capital: List[Dict[str, Any]], unit: str) -> str:
    title = "Free cash flow and reinvestment"
    usable = [r for r in capital if r.get("fcf") is not None or r.get("reinvestment_rate") is not None]
    if not usable:
        return _empty(title, "cash-flow detail was not available")

    labels = [r["period_label"] for r in usable]
    fcf = [r.get("fcf") for r in usable]
    cfo = [r.get("cfo") for r in usable]
    reinv = [
        (r.get("reinvestment_rate") * 100.0) if r.get("reinvestment_rate") is not None else None
        for r in usable
    ]

    top_h, gap, bot_h = 170, 34, 110
    height = PAD_T + top_h + gap + bot_h + PAD_B
    x1 = W - PAD_R
    n = len(labels)
    slot = (x1 - PAD_L) / max(1, n)

    def x_of(i: float) -> float:
        return PAD_L + slot * (i + 0.5)

    lo, hi = _bounds(fcf + cfo)
    top_y1 = PAD_T + top_h

    def y_top(v: float) -> float:
        return top_y1 - (v - lo) / (hi - lo) * top_h

    parts = [_grid_and_axis(PAD_L, x1, y_top, _nice_ticks(lo, hi, 4))]
    zero = y_top(0)
    bar_w = max(3.0, slot * 0.42)
    for i, v in enumerate(fcf):
        if v is None:
            continue
        y = y_top(v)
        top, h = (y, zero - y) if v >= 0 else (zero, y - zero)
        # Negative FCF is a state, not a series -> status colour, with the sign
        # also carried by position below the zero line.
        fill = "var(--series-1)" if v >= 0 else "var(--status-critical)"
        parts.append(
            f'<rect class="bar" x="{x_of(i) - bar_w / 2:.1f}" y="{top:.1f}" '
            f'width="{bar_w:.1f}" height="{max(1.0, h):.1f}" rx="3" fill="{fill}">'
            f"<title>{esc(labels[i])} · FCF {esc(fmt(v))} {esc(unit)}</title></rect>"
        )
    parts.append(
        f'<line class="baseline" x1="{PAD_L}" y1="{zero:.1f}" x2="{x1}" y2="{zero:.1f}"/>'
    )

    rlo, rhi = _bounds(reinv)
    bot_y0 = top_y1 + gap
    bot_y1 = bot_y0 + bot_h

    def y_bot(v: float) -> float:
        return bot_y1 - (v - rlo) / (rhi - rlo) * bot_h

    parts.append(_grid_and_axis(PAD_L, x1, y_bot, _nice_ticks(rlo, rhi, 3), "%"))
    pts = [(x_of(i), y_bot(v)) for i, v in enumerate(reinv) if v is not None]
    if len(pts) >= 2:
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<path class="line" d="{d}" stroke="var(--series-2)"/>')
    for i, v in enumerate(reinv):
        if v is None:
            continue
        parts.append(
            f'<circle class="dot" cx="{x_of(i):.1f}" cy="{y_bot(v):.1f}" r="4.5" '
            f'fill="var(--series-2)"><title>{esc(labels[i])} · reinvestment '
            f"{v:.0f}% of CFO</title></circle>"
        )

    parts.append(f'<text class="axtitle" x="{PAD_L}" y="{PAD_T - 6}">Free cash flow ({esc(unit)})</text>')
    parts.append(f'<text class="axtitle" x="{PAD_L}" y="{bot_y0 - 8}">Reinvestment rate (capex ÷ CFO)</text>')
    parts.append(_x_labels(labels, x_of, height - PAD_B + 18))

    svg = (
        f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="Free cash flow bars '
        f'above the reinvestment rate line">' + "".join(parts) + "</svg>"
    )
    table = _table_view(
        ["Fiscal year", f"CFO ({unit})", f"FCF ({unit})", "Reinvestment %"],
        [[labels[i], fmt(cfo[i]), fmt(fcf[i]), fmt(reinv[i], 0)] for i in range(n)],
    )
    return _figure(
        title, svg,
        "FCF = CFO − capex, derived in SQL. A high reinvestment rate is only good "
        "news when the return on that incremental capital is high.",
        table,
    )


# =====================================================================
# 4. Price chart with SMA50/200, support/resistance, and an RSI panel
# =====================================================================


def price_chart(tech: Any, currency: str) -> str:
    title = "Price, moving averages and momentum"
    series = getattr(tech, "series", None) or {}
    closes = series.get("close") or []
    if len(closes) < 30:
        return _empty(title, "not enough price history was available")

    dates = series["date"]
    sma_f, sma_s, rsi = series.get("sma_fast") or [], series.get("sma_slow") or [], series.get("rsi") or []

    top_h, gap, bot_h = 230, 30, 96
    height = PAD_T + top_h + gap + bot_h + PAD_B
    x1 = W - PAD_R
    n = len(closes)

    def x_of(i: float) -> float:
        return PAD_L + (x1 - PAD_L) * (i / max(1, n - 1))

    levels = list(getattr(tech, "support", []) or []) + list(getattr(tech, "resistance", []) or [])
    lo, hi = _bounds(
        closes + [v for v in sma_s if v is not None] + levels, include_zero=False
    )
    top_y1 = PAD_T + top_h

    def y_top(v: float) -> float:
        return top_y1 - (v - lo) / (hi - lo) * top_h

    parts = [_grid_and_axis(PAD_L, x1, y_top, _nice_ticks(lo, hi, 5))]

    for level, kind in (
        [(lv, "support") for lv in (getattr(tech, "support", []) or [])]
        + [(lv, "resistance") for lv in (getattr(tech, "resistance", []) or [])]
    ):
        y = y_top(level)
        parts.append(
            f'<line class="srline {kind}" x1="{PAD_L}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}">'
            f"<title>{kind} ≈ {esc(fmt(level, 2))} {esc(currency)}</title></line>"
        )
        parts.append(
            f'<text class="srlabel" x="{PAD_L + 4}" y="{y - 4:.1f}">{kind} {esc(fmt(level, 0))}</text>'
        )

    def path_of(values: Sequence[Number]) -> str:
        pts = [(x_of(i), y_top(v)) for i, v in enumerate(values) if v is not None]
        return ("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)) if len(pts) > 1 else ""

    for values, var, klass in (
        (closes, "--series-1", "line price"),
        (sma_f, "--series-2", "line"),
        (sma_s, "--series-3", "line"),
    ):
        d = path_of(values)
        if d:
            parts.append(f'<path class="{klass}" d="{d}" stroke="var({var})"/>')

    last_x, last_y = x_of(n - 1), y_top(closes[-1])
    parts.append(f'<circle class="endpoint" cx="{last_x:.1f}" cy="{last_y:.1f}" r="5"/>')
    parts.append(
        f'<text class="dlabel" x="{last_x - 6:.1f}" y="{last_y - 9:.1f}" text-anchor="end">'
        f"{esc(fmt(closes[-1], 2))} {esc(currency)}</text>"
    )

    # RSI panel
    bot_y0 = top_y1 + gap
    bot_y1 = bot_y0 + bot_h

    def y_bot(v: float) -> float:
        return bot_y1 - (v / 100.0) * bot_h

    parts.append(_grid_and_axis(PAD_L, x1, y_bot, [0, 30, 50, 70, 100]))
    parts.append(
        f'<rect class="rsiband" x="{PAD_L}" y="{y_bot(70):.1f}" width="{x1 - PAD_L}" '
        f'height="{y_bot(30) - y_bot(70):.1f}"/>'
    )
    d = "M" + " L".join(
        f"{x_of(i):.1f},{y_bot(v):.1f}" for i, v in enumerate(rsi) if v is not None
    )
    if len(d) > 3:
        parts.append(f'<path class="line" d="{d}" stroke="var(--series-1)"/>')
    parts.append(f'<text class="axtitle" x="{PAD_L}" y="{bot_y0 - 8}">RSI (14)</text>')
    parts.append(f'<text class="axtitle" x="{PAD_L}" y="{PAD_T - 6}">Close ({esc(currency)})</text>')

    step = max(1, n // 8)
    for i in range(0, n, step):
        parts.append(
            f'<text class="tick" x="{x_of(i):.1f}" y="{height - PAD_B + 18}" '
            f'text-anchor="middle">{esc(dates[i][:7])}</text>'
        )

    svg = (
        f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="Daily close with 50 '
        f'and 200 day moving averages, support and resistance, and an RSI panel">'
        + "".join(parts) + "</svg>"
    )
    legend = _legend(
        [("Close", "--series-1"), ("SMA 50", "--series-2"), ("SMA 200", "--series-3")]
    )
    recent = list(range(max(0, n - 12), n))
    table = _table_view(
        ["Date", f"Close ({currency})", "SMA 50", "SMA 200", "RSI"],
        [
            [
                dates[i], fmt(closes[i], 2),
                fmt(sma_f[i], 2) if i < len(sma_f) else "n/a",
                fmt(sma_s[i], 2) if i < len(sma_s) else "n/a",
                fmt(rsi[i], 0) if i < len(rsi) else "n/a",
            ]
            for i in recent
        ],
        "table view (last 12 sessions)",
    )
    return _figure(
        title, legend + svg,
        "Support and resistance are swing pivots clustered within 2% of each other, "
        "not round numbers. The shaded RSI band marks 30–70.",
        table,
    )


# =====================================================================
# 5. Valuation football field
# =====================================================================


def football_field(valuation: Any) -> str:
    from ..valuation.models import IV_MODELS

    title = "Valuation football field"
    all_models = getattr(valuation, "models", []) or []
    # Only true intrinsic-value estimates share the bar scale. Buffetology's
    # headline output is an expected annual *return*; putting its derived
    # per-share figure on the same axis would invite a comparison between two
    # different quantities. It stays in the table below with its own units.
    models = [m for m in all_models if m.name in IV_MODELS and m.value]
    if not models:
        return _empty(title, "no model produced an intrinsic value")

    price = getattr(valuation, "current_price", None)
    buy_below = getattr(valuation, "buy_below", None)
    currency = getattr(valuation, "currency", "") or ""

    head = 44                                  # band for the two marker labels
    row_h, gap, bar_h = 30, 16, 18
    height = head + len(models) * (row_h + gap) + 40
    x0, x1 = 210, W - PAD_R
    hi = max([m.value for m in models] + [v for v in (price, buy_below) if v]) * 1.14

    def x_of(v: float) -> float:
        return x0 + (v / hi) * (x1 - x0)

    plot_bottom = head + len(models) * (row_h + gap) - gap + 4
    parts = []
    for t in _nice_ticks(0, hi, 5):
        if t < 0:
            continue
        x = x_of(t)
        parts.append(
            f'<line class="grid" x1="{x:.1f}" y1="{head - 8}" x2="{x:.1f}" y2="{plot_bottom:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{x:.1f}" y="{plot_bottom + 17:.1f}" text-anchor="middle">'
            f"{esc(fmt(t, 0))}</text>"
        )

    for i, model in enumerate(models):
        y = head + i * (row_h + gap)
        parts.append(
            f'<text class="ffname" x="{x0 - 12}" y="{y + bar_h / 2 + 4:.1f}" '
            f'text-anchor="end">{esc(model.name)}</text>'
        )
        parts.append(
            f'<rect class="bar" x="{x_of(0):.1f}" y="{y:.1f}" '
            f'width="{max(2.0, x_of(model.value) - x_of(0)):.1f}" height="{bar_h}" '
            f'rx="4" fill="var(--series-1)"><title>{esc(model.name)}: '
            f"{esc(fmt(model.value, 2))} {esc(currency)}</title></rect>"
        )
        parts.append(
            f'<text class="dlabel" x="{x_of(model.value) + 8:.1f}" y="{y + bar_h / 2 + 4:.1f}">'
            f"{esc(fmt(model.value, 2))}</text>"
        )

    # Marker labels live in their own band above the plot, on two rows, so they
    # cannot collide with each other or with the x-axis ticks below.
    markers = [
        (price, "marker-price", f"current price {fmt(price, 2)}"),
        (buy_below, "marker-mos", f"buy-below ({getattr(valuation, 'margin_of_safety', 0.5):.0%} MoS) {fmt(buy_below, 2)}"),
    ]
    for row, (value, klass, label) in enumerate(m for m in markers if m[0]):
        x = x_of(value)
        label_y = 14 + row * 16
        # Every marker line starts below the whole label band, so a line for one
        # marker can never strike through the other marker's label.
        parts.append(
            f'<line class="{klass}" x1="{x:.1f}" y1="{head - 8}" x2="{x:.1f}" '
            f'y2="{plot_bottom:.1f}"/>'
        )
        anchor = "start" if x < x0 + 90 else "end" if x > x1 - 90 else "middle"
        dx = 6 if anchor == "start" else -6 if anchor == "end" else 0
        parts.append(
            f'<text class="markerlabel {klass}" x="{x + dx:.1f}" y="{label_y}" '
            f'text-anchor="{anchor}">{esc(label)}</text>'
        )

    svg = (
        f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="Intrinsic value per '
        f'model with the current price and the margin-of-safety line marked">'
        + "".join(parts) + "</svg>"
    )
    rows = []
    for m in all_models:
        if m.value:
            vs = (
                f"{(m.value - price) / price * 100:+.0f}%" if price else "n/a"
            )
            rows.append([m.name, f"{fmt(m.value, 2)} {currency}", vs])
        else:
            rows.append([m.name, "not available", m.unavailable_reason or ""])
    if buy_below:
        rows.append([f"Margin of safety @ {getattr(valuation, 'margin_of_safety', 0.5):.0%}",
                     f"{fmt(buy_below, 2)} {currency}", "buy-below line"])
    if price:
        rows.append(["Current price", f"{fmt(price, 2)} {currency}", "—"])
    table = _table_view(["Model", "Intrinsic value / share", "vs current price"], rows)
    return _figure(
        title, svg,
        "Graham reads as the floor, Bharat Shah as the quality-adjusted ceiling. "
        "Buffetology is charted nowhere here because its headline output is an "
        "expected annual return, not a per-share value — see the table below.",
        table,
        "All bars start at zero and share one scale, so their lengths are directly comparable.",
    )


# =====================================================================
# 6. Commodity linkage scatter
# =====================================================================


def commodity_scatter(link: Any, annual: List[Dict[str, Any]]) -> str:
    title = f"{getattr(link, 'name', 'Commodity')} vs operating margin"
    if getattr(link, "basis", "") != "measured":
        return _empty(
            title,
            getattr(link, "narrative", "") or "the relationship could not be quantified",
        )

    series = getattr(link, "series", []) or []
    from ..analysis.linkage import _annual_average

    yearly = _annual_average(series, annual)
    points = [
        (yearly[r["period_label"]], r.get("opm"), r["period_label"])
        for r in annual
        if r["period_label"] in yearly and r.get("opm") is not None
    ]
    # v_annual has no opm column; recompute from the row when absent.
    if not points:
        points = [
            (
                yearly[r["period_label"]],
                (r["ebit"] / r["sales"] * 100.0) if r.get("sales") and r.get("ebit") else None,
                r["period_label"],
            )
            for r in annual
            if r["period_label"] in yearly
        ]
        points = [p for p in points if p[1] is not None]
    if len(points) < 3:
        return _empty(title, "too few overlapping fiscal years to plot")

    height = 300
    x1, y1 = W - PAD_R, height - PAD_B
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xlo, xhi = _bounds(xs, include_zero=False)
    ylo, yhi = _bounds(ys, include_zero=False)

    def x_of(v: float) -> float:
        return PAD_L + (v - xlo) / (xhi - xlo) * (x1 - PAD_L)

    def y_of(v: float) -> float:
        return y1 - (v - ylo) / (yhi - ylo) * (y1 - PAD_T)

    parts = [_grid_and_axis(PAD_L, x1, y_of, _nice_ticks(ylo, yhi, 4), "%")]
    for t in _nice_ticks(xlo, xhi, 5):
        parts.append(
            f'<text class="tick" x="{x_of(t):.1f}" y="{height - PAD_B + 18}" '
            f'text-anchor="middle">{esc(fmt(t, 0))}</text>'
        )

    slope_note = ""
    corr = getattr(link, "correlation", None)
    if corr is not None and len(points) >= 3:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx > 0:
            b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
            a = my - b * mx
            parts.append(
                f'<line class="fitline" x1="{x_of(xlo):.1f}" y1="{y_of(a + b * xlo):.1f}" '
                f'x2="{x_of(xhi):.1f}" y2="{y_of(a + b * xhi):.1f}"/>'
            )
            slope_note = f" Fitted slope shown; r = {corr:+.2f}."

    for cx, cy, label in points:
        parts.append(
            f'<circle class="scatter" cx="{x_of(cx):.1f}" cy="{y_of(cy):.1f}" r="6" '
            f'fill="var(--series-1)"><title>{esc(label)}: {esc(link.name)} '
            f"{esc(fmt(cx, 1))}, OPM {cy:.1f}%</title></circle>"
        )
    # Label the endpoints only, not every point.
    for cx, cy, label in (points[0], points[-1]):
        parts.append(
            f'<text class="dlabel" x="{x_of(cx) + 9:.1f}" y="{y_of(cy) + 4:.1f}">{esc(label)}</text>'
        )

    parts.append(
        f'<text class="axtitle" x="{PAD_L}" y="{PAD_T - 6}">Operating margin (%)</text>'
    )
    parts.append(
        f'<text class="axtitle" x="{x1}" y="{height - 6}" text-anchor="end">'
        f"{esc(link.name)} — fiscal-year average price</text>"
    )

    svg = (
        f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="Scatter of operating '
        f'margin against the fiscal-year average {esc(link.name)} price">'
        + "".join(parts) + "</svg>"
    )
    table = _table_view(
        ["Fiscal year", f"{link.name} (FY avg)", "Operating margin %"],
        [[label, fmt(cx, 1), f"{cy:.1f}"] for cx, cy, label in points],
    )
    return _figure(
        title, svg,
        f"Each point is one fiscal year. The commodity is averaged across the year, "
        f"not sampled at year-end, because the year's average is what the cost base "
        f"actually saw.{slope_note}",
        table,
    )
