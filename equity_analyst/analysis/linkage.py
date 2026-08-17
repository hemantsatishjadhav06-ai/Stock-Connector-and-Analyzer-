"""§5 News sentiment and commodity -> price linkage.

The commodity work is deliberately quantitative where the data allows and
*explicitly labelled qualitative where it does not*. A sensitivity printed
without a correlation behind it would be exactly the fabrication the master
prompt forbids, so every linkage carries its ``basis`` and sample size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import QueryLog, linear_slope, pearson

#: Sector/industry keyword -> commodities that drive it, and on which side of
#: the P&L. Used only to *propose* linkages; the correlation still has to earn
#: its place in the report.
COMMODITY_MAP: List[Tuple[str, List[Dict[str, str]]]] = [
    ("oil|gas|petroleum|refin|energy", [
        {"symbol": "CL=F", "name": "WTI crude oil", "role": "output"},
        {"symbol": "BZ=F", "name": "Brent crude oil", "role": "output"},
        {"symbol": "NG=F", "name": "Natural gas", "role": "output"},
    ]),
    ("airline|aviation|travel", [
        {"symbol": "CL=F", "name": "WTI crude oil", "role": "input"},
    ]),
    ("steel|metal|mining|iron", [
        {"symbol": "HG=F", "name": "Copper", "role": "output"},
        {"symbol": "GC=F", "name": "Gold", "role": "output"},
    ]),
    ("auto|automobile|vehicle|tyre|tire", [
        {"symbol": "HG=F", "name": "Copper", "role": "input"},
        {"symbol": "CL=F", "name": "WTI crude oil", "role": "input"},
    ]),
    ("chemical|fertil|paint|polymer|plastic", [
        {"symbol": "CL=F", "name": "WTI crude oil", "role": "input"},
        {"symbol": "NG=F", "name": "Natural gas", "role": "input"},
    ]),
    ("food|agri|sugar|beverage|fmcg|consumer staples", [
        {"symbol": "ZW=F", "name": "Wheat", "role": "input"},
        {"symbol": "ZC=F", "name": "Corn", "role": "input"},
    ]),
    ("cement|construction|infrastructure", [
        {"symbol": "CL=F", "name": "WTI crude oil", "role": "input"},
    ]),
    ("gold|jewel", [
        {"symbol": "GC=F", "name": "Gold", "role": "input"},
    ]),
    ("semiconductor|electronic|hardware|technology", [
        {"symbol": "HG=F", "name": "Copper", "role": "input"},
    ]),
    ("power|utility|electric", [
        {"symbol": "NG=F", "name": "Natural gas", "role": "input"},
    ]),
]

POSITIVE = (
    "beat", "beats", "record", "surge", "surged", "growth", "profit", "upgrade",
    "wins", "won", "approval", "expansion", "expands", "launch", "dividend",
    "buyback", "raises", "strong", "outperform", "rally", "high",
)
NEGATIVE = (
    "miss", "misses", "fall", "falls", "decline", "loss", "losses", "downgrade",
    "probe", "investigation", "fraud", "lawsuit", "recall", "cut", "cuts",
    "warns", "warning", "weak", "slump", "plunge", "resign", "default", "fine",
    "penalty", "strike", "delay",
)


@dataclass
class CommodityLinkage:
    symbol: str
    name: str
    role: str
    basis: str                       # 'measured' | 'qualitative'
    correlation: Optional[float] = None
    observations: int = 0
    margin_sensitivity_bps: Optional[float] = None   # per +10% commodity move
    revenue_sensitivity_pct: Optional[float] = None  # per +10% commodity move
    price_correlation: Optional[float] = None
    commodity_trend_pct: Optional[float] = None
    direction: str = "unclear"       # tailwind | headwind | neutral | unclear
    strength: str = "unknown"        # strong | moderate | weak | unknown
    narrative: str = ""
    series: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class LinkageResult:
    news: List[Dict[str, Any]] = field(default_factory=list)
    sentiment_summary: Dict[str, Any] = field(default_factory=dict)
    commodities: List[CommodityLinkage] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def analyse(db: Any, ticker: str, config: Any, log: QueryLog) -> LinkageResult:
    res = LinkageResult()
    res.news = _news(db, ticker, log)
    res.sentiment_summary = _summarise_sentiment(res.news)
    res.commodities = _commodities(db, ticker, config, log, res)
    return res


# -- news ------------------------------------------------------------------


def _news(db: Any, ticker: str, log: QueryLog) -> List[Dict[str, Any]]:
    rows = log.run(
        db, "news",
        """
        SELECT published_at, headline, url, publisher, sentiment, sentiment_score,
               est_impact_pct, impact_basis
        FROM news_item WHERE ticker = ?
        ORDER BY COALESCE(published_at, '') DESC LIMIT 40
        """,
        (ticker,),
        "Recent company and sector news, classified for sentiment.",
    )
    for row in rows:
        if row.get("sentiment") is None:
            label, score = classify_headline(row.get("headline") or "")
            row["sentiment"], row["sentiment_score"] = label, score
            # No event study behind this, so no fabricated price impact.
            row["impact_basis"] = "qualitative"
    return rows


def classify_headline(headline: str) -> Tuple[str, float]:
    """Lexicon sentiment. Deliberately simple and transparent -- it is a
    triage signal for a human reader, not a claimed alpha model."""
    words = set(re.findall(r"[a-z']+", (headline or "").lower()))
    pos = len(words & set(POSITIVE))
    neg = len(words & set(NEGATIVE))
    if pos == neg:
        return "neutral", 0.0
    total = pos + neg
    score = (pos - neg) / total
    return ("positive" if score > 0 else "negative"), round(score, 2)


def _summarise_sentiment(news: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not news:
        return {"count": 0, "net_sentiment": None, "skew": "no coverage"}
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for row in news:
        counts[row.get("sentiment") or "neutral"] = (
            counts.get(row.get("sentiment") or "neutral", 0) + 1
        )
    total = len(news)
    net = (counts["positive"] - counts["negative"]) / total
    skew = "positive" if net > 0.15 else "negative" if net < -0.15 else "balanced"
    return {
        "count": total,
        "positive": counts["positive"],
        "neutral": counts["neutral"],
        "negative": counts["negative"],
        "net_sentiment": round(net, 2),
        "skew": skew,
    }


# -- commodity linkage -----------------------------------------------------


def _commodities(
    db: Any, ticker: str, config: Any, log: QueryLog, res: LinkageResult
) -> List[CommodityLinkage]:
    company = db.dicts("SELECT * FROM company WHERE ticker = ?", (ticker,))
    profile = company[0] if company else {}
    candidates = _candidate_commodities(profile, config)
    if not candidates:
        res.notes.append(
            "No commodity linkage proposed: sector/industry not classified and "
            "none supplied via --commodity."
        )
        return []

    annual = log.run(
        db, "commodity_fundamental_base",
        """
        SELECT a.period_label, a.period_end, a.sales,
               CASE WHEN a.sales > 0 THEN a.ebit / a.sales * 100.0 END AS opm
        FROM v_annual a WHERE a.ticker = ? ORDER BY a.yr_idx
        """,
        (ticker,),
        "Annual sales and operating margin, the dependent series in the linkage.",
    )

    out: List[CommodityLinkage] = []
    for candidate in candidates:
        link = _link_one(db, ticker, candidate, annual, log)
        if link:
            out.append(link)
    return out


def _candidate_commodities(profile: Dict[str, Any], config: Any) -> List[Dict[str, str]]:
    explicit = [
        {"symbol": s, "name": s, "role": "input"}
        for s in (getattr(config, "commodities", None) or [])
    ]
    if explicit:
        return explicit

    haystack = " ".join(
        str(profile.get(k) or "") for k in ("sector", "industry", "name")
    ).lower()
    if not haystack.strip():
        return []
    picked: List[Dict[str, str]] = []
    seen = set()
    for pattern, commodities in COMMODITY_MAP:
        if re.search(pattern, haystack):
            for c in commodities:
                if c["symbol"] not in seen:
                    seen.add(c["symbol"])
                    picked.append(c)
    return picked[:3]


def _link_one(
    db: Any,
    ticker: str,
    candidate: Dict[str, str],
    annual: List[Dict[str, Any]],
    log: QueryLog,
) -> Optional[CommodityLinkage]:
    symbol = candidate["symbol"]
    link = CommodityLinkage(
        symbol=symbol,
        name=candidate.get("name") or symbol,
        role=candidate.get("role") or "input",
        basis="qualitative",
    )

    series = log.run(
        db, f"commodity_series_{symbol}",
        "SELECT date, close FROM commodity_price WHERE symbol = ? ORDER BY date",
        (symbol,),
        f"Price history for {link.name}, aligned to fiscal years for the linkage.",
    )
    link.series = series
    if not series:
        link.narrative = (
            f"{link.name} is a plausible {link.role} for this business, but no price "
            f"series could be pulled, so the relationship is asserted qualitatively "
            f"and not quantified."
        )
        return link

    # Average the commodity over each fiscal year, then regress the company's
    # margin/revenue on it. Annual averaging is the right resolution: a
    # spot-price snapshot at year-end says nothing about the cost actually
    # incurred through the year.
    yearly = _annual_average(series, annual)
    paired = [
        (yearly[row["period_label"]], row)
        for row in annual
        if row["period_label"] in yearly
    ]
    link.observations = len(paired)
    if len(paired) < 4:
        link.narrative = (
            f"Only {len(paired)} overlapping fiscal years with {link.name} - too few "
            f"to measure a relationship. Treated as qualitative."
        )
        return link

    commodity_changes, margin_changes, revenue_changes = [], [], []
    for (prev_c, prev_r), (cur_c, cur_r) in zip(paired, paired[1:]):
        if not prev_c or prev_c <= 0:
            continue
        c_chg = (cur_c - prev_c) / prev_c * 100.0
        if prev_r.get("opm") is not None and cur_r.get("opm") is not None:
            commodity_changes.append(c_chg)
            margin_changes.append(cur_r["opm"] - prev_r["opm"])  # in pp
        if prev_r.get("sales") and cur_r.get("sales") and prev_r["sales"] > 0:
            revenue_changes.append((cur_r["sales"] - prev_r["sales"]) / prev_r["sales"] * 100.0)

    if len(commodity_changes) >= 3:
        link.basis = "measured"
        link.correlation = pearson(commodity_changes, margin_changes)
        slope = linear_slope(commodity_changes, margin_changes)
        if slope is not None:
            # pp of margin per 1% commodity move -> bps per +10% move
            link.margin_sensitivity_bps = slope * 10.0 * 100.0
        if len(revenue_changes) == len(commodity_changes):
            rev_slope = linear_slope(commodity_changes, revenue_changes)
            if rev_slope is not None:
                link.revenue_sensitivity_pct = rev_slope * 10.0

    link.commodity_trend_pct = _trend(series)
    _interpret(link)
    return link


def _annual_average(
    series: List[Dict[str, Any]], annual: List[Dict[str, Any]]
) -> Dict[str, float]:
    """Mean commodity close within each fiscal year."""
    ends = [(r["period_label"], r.get("period_end")) for r in annual if r.get("period_end")]
    out: Dict[str, float] = {}
    for i, (label, end) in enumerate(ends):
        start = ends[i - 1][1] if i else None
        window = [
            p["close"] for p in series
            if p.get("close") is not None
            and p["date"] <= end
            and (start is None or p["date"] > start)
        ]
        if window:
            out[label] = sum(window) / len(window)
    return out


def _trend(series: List[Dict[str, Any]]) -> Optional[float]:
    """Percentage change over the last ~12 months of the commodity series."""
    closes = [p["close"] for p in series if p.get("close") is not None]
    if len(closes) < 8:
        return None
    window = closes[-52:] if len(closes) >= 52 else closes
    if not window[0]:
        return None
    return (window[-1] - window[0]) / window[0] * 100.0


def _interpret(link: CommodityLinkage) -> None:
    corr = link.correlation
    if corr is not None:
        magnitude = abs(corr)
        link.strength = (
            "strong" if magnitude >= 0.6
            else "moderate" if magnitude >= 0.35
            else "weak"
        )

    # A near-zero correlation carries no directional information. Reporting a
    # tailwind/headwind off a slope fitted through noise would be asserting a
    # relationship the data does not support, so the finding is demoted to
    # "no measurable linkage" instead.
    negligible = corr is not None and abs(corr) < 0.20
    if negligible:
        link.strength = "negligible"

    trend = link.commodity_trend_pct
    if trend is None or negligible:
        link.direction = "unclear"
    elif link.basis == "measured" and link.margin_sensitivity_bps is not None:
        # The sign of the measured sensitivity decides, not the assumed role:
        # an integrated refiner can be long crude even though it "consumes" it.
        helps = link.margin_sensitivity_bps > 0
        rising = trend > 2.0
        falling = trend < -2.0
        if (helps and rising) or (not helps and falling):
            link.direction = "tailwind"
        elif (helps and falling) or (not helps and rising):
            link.direction = "headwind"
        else:
            link.direction = "neutral"
    else:
        # No measured sensitivity: fall back to the textbook role, and say so.
        if abs(trend) <= 2.0:
            link.direction = "neutral"
        elif link.role == "input":
            link.direction = "headwind" if trend > 0 else "tailwind"
        else:
            link.direction = "tailwind" if trend > 0 else "headwind"

    parts = []
    if link.basis == "measured" and negligible:
        link.narrative = (
            f"{link.name} shows no measurable relationship with operating margin "
            f"over {link.observations} fiscal years (r = {corr:+.2f}). The fitted "
            f"sensitivity is indistinguishable from noise, so no tailwind or "
            f"headwind is claimed even though the commodity is "
            f"{'rising' if (trend or 0) > 0 else 'falling'}"
            + (f" ({trend:+.1f}% over the last year)." if trend is not None else ".")
        )
        return
    if link.basis == "measured":
        parts.append(
            f"{link.name} treated as an {link.role}; measured over "
            f"{link.observations} fiscal years"
        )
        if corr is not None:
            parts.append(
                f"correlation with operating margin {corr:+.2f} ({link.strength})"
            )
        if link.margin_sensitivity_bps is not None:
            verb = "lifts" if link.margin_sensitivity_bps > 0 else "compresses"
            parts.append(
                f"a +10% move in {link.name} historically {verb} OPM by "
                f"~{abs(link.margin_sensitivity_bps):.0f} bps"
            )
        if link.revenue_sensitivity_pct is not None:
            verb = "lifts" if link.revenue_sensitivity_pct > 0 else "reduces"
            parts.append(
                f"and {verb} revenue by ~{abs(link.revenue_sensitivity_pct):.1f}%"
            )
    else:
        parts.append(
            f"{link.name} is a qualitative {link.role} linkage - not enough "
            f"overlapping data to quantify"
        )

    if trend is not None:
        parts.append(
            f"{link.name} is {'rising' if trend > 0 else 'falling'} "
            f"({trend:+.1f}% over the last year) -> {link.direction}"
        )
    link.narrative = "; ".join(parts) + "."


def default_commodities_for(profile: Dict[str, Any]) -> List[Dict[str, str]]:
    """Exposed for the pipeline so it knows which series to fetch."""
    return _candidate_commodities(profile, type("C", (), {"commodities": []})())
