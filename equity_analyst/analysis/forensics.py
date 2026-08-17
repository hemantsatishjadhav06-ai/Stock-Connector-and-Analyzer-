"""§4 Forensic / quality-of-earnings screen.

A failed check caps the verdict no matter how cheap the stock looks, so the
severity model matters as much as the tests:

``fail``   a breach that invalidates the investment case (profits not backed by
           cash, goodwill dominating net worth, heavy pledging).
``watch``  a deterioration worth monitoring but not disqualifying on its own.
``pass``   the check ran and cleared.
``skipped`` the data needed was not available. **Never** silently a pass -- a
           skipped check lowers §8 completeness and is reported as unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import QueryLog, mean, median, stdev

SEVERITY_WEIGHT = {"pass": 1.0, "watch": 0.5, "fail": 0.0}


@dataclass
class Check:
    key: str
    title: str
    status: str            # pass | watch | fail | skipped
    detail: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    weight: float = 1.0


@dataclass
class ForensicResult:
    checks: List[Check] = field(default_factory=list)
    score: Optional[float] = None       # 0-100
    flag: str = "unknown"               # pass | watch | fail | unknown
    breaches: List[Check] = field(default_factory=list)
    cash_conversion: List[Dict[str, Any]] = field(default_factory=list)
    blocks: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def skipped(self) -> List[Check]:
        return [c for c in self.checks if c.status == "skipped"]


def analyse(db: Any, ticker: str, config: Any, log: QueryLog) -> ForensicResult:
    a = config.assumptions
    res = ForensicResult()
    add = res.checks.append

    quality = log.run(
        db, "earnings_quality",
        "SELECT * FROM v_earnings_quality WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "Cash conversion: CFO against EBITDA and PAT, year by year.",
    )
    res.cash_conversion = quality

    add(_cumulative_cash_check(quality))
    res.blocks = _three_year_blocks(quality)
    add(_block_check(res.blocks))
    add(_cfo_ebitda_check(quality, a.cfo_to_ebitda_min))
    add(_cash_yield_check(db, ticker, a, log))

    wc = log.run(
        db, "working_capital_trend",
        "SELECT * FROM v_working_capital_trend WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "Receivables and inventory growth against sales growth.",
    )
    add(_receivables_check(wc))
    add(_inventory_check(wc))

    flags = log.run(
        db, "balance_sheet_flags",
        "SELECT * FROM v_balance_sheet_flags WHERE ticker = ? ORDER BY yr_idx", (ticker,),
        "Contingent liabilities, intangibles and provisions as % of net worth.",
    )
    add(_contingent_check(flags, a.contingent_liab_max_pct))
    add(_intangibles_check(flags, a.intangibles_max_pct))
    add(_provisions_check(flags, a.receivable_provision_max_pct))
    add(_depreciation_volatility_check(flags))
    add(_pledge_check(db, ticker, log))
    add(_promoter_stake_check(db, ticker, log))

    _score(res)
    return res


# -- individual checks -----------------------------------------------------


def _cumulative_cash_check(quality: List[Dict[str, Any]]) -> Check:
    pat = [r["pat"] for r in quality if r.get("pat") is not None]
    cfo = [r["cfo"] for r in quality if r.get("cfo") is not None]
    if not pat or not cfo:
        return Check(
            "cum_pat_vs_cfo", "Cumulative PAT vs CFO", "skipped",
            "Profit or cash-flow history missing.", weight=1.5,
        )
    total_pat, total_cfo = sum(pat), sum(cfo)
    ratio = total_cfo / total_pat if total_pat > 0 else None
    if ratio is None:
        status, detail = "watch", "Cumulative PAT is not positive; ratio undefined."
    elif ratio >= 1.0:
        status = "pass"
        detail = (
            f"Over {len(pat)} years the business converted {ratio:.2f}x of "
            f"reported profit into operating cash."
        )
    elif ratio >= 0.8:
        status = "watch"
        detail = f"CFO covers only {ratio:.2f}x cumulative PAT - accrual-heavy."
    else:
        status = "fail"
        detail = (
            f"CFO covers just {ratio:.2f}x cumulative PAT over {len(pat)} years: "
            f"reported profits are not arriving as cash."
        )
    return Check(
        "cum_pat_vs_cfo", "Cumulative PAT vs CFO (10y)", status, detail,
        value=ratio, threshold=1.0, weight=1.5,
    )


def _three_year_blocks(quality: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Same test in 3-year blocks, so a good decade cannot hide a bad recent run."""
    blocks = []
    for start in range(0, max(0, len(quality) - 2), 3):
        chunk = quality[start:start + 3]
        if len(chunk) < 2:
            continue
        pat = sum(r["pat"] for r in chunk if r.get("pat") is not None)
        cfo = sum(r["cfo"] for r in chunk if r.get("cfo") is not None)
        blocks.append(
            {
                "from": chunk[0]["period_label"],
                "to": chunk[-1]["period_label"],
                "pat": pat,
                "cfo": cfo,
                "ratio": (cfo / pat) if pat > 0 else None,
            }
        )
    return blocks


def _block_check(blocks: List[Dict[str, Any]]) -> Check:
    if not blocks:
        return Check(
            "block_cash_conversion", "3-year block cash conversion", "skipped",
            "Not enough history to form 3-year blocks.",
        )
    weak = [b for b in blocks if b["ratio"] is not None and b["ratio"] < 0.8]
    latest = blocks[-1]["ratio"]
    if not weak:
        status, detail = "pass", f"All {len(blocks)} blocks converted >= 0.8x PAT into CFO."
    elif latest is not None and latest < 0.8:
        status = "fail"
        detail = (
            f"Most recent block ({blocks[-1]['from']}-{blocks[-1]['to']}) converted "
            f"{latest:.2f}x - the deterioration is current, not historic."
        )
    else:
        status = "watch"
        detail = f"{len(weak)} of {len(blocks)} blocks converted below 0.8x, but the latest is fine."
    return Check(
        "block_cash_conversion", "3-year block cash conversion", status, detail,
        value=latest, threshold=0.8,
    )


def _cfo_ebitda_check(quality: List[Dict[str, Any]], minimum: float) -> Check:
    series = [r["cfo_to_ebitda"] for r in quality if r.get("cfo_to_ebitda") is not None]
    if not series:
        return Check(
            "cfo_to_ebitda", "CFO / EBITDA > 0.7", "skipped",
            "EBITDA or CFO unavailable.", threshold=minimum, weight=1.5,
        )
    med = median(series)
    below = [v for v in series if v < minimum]
    if med is not None and med >= minimum and len(below) <= len(series) // 4:
        status = "pass"
        detail = f"Median CFO/EBITDA {med:.2f} across {len(series)} years."
    elif med is not None and med >= minimum:
        status = "watch"
        detail = f"Median {med:.2f} clears {minimum}, but {len(below)}/{len(series)} years fall short."
    else:
        status = "fail"
        detail = (
            f"Median CFO/EBITDA {med:.2f} is below the {minimum} floor - accrual "
            f"profit is not cash-backed."
        )
    return Check(
        "cfo_to_ebitda", f"CFO / EBITDA > {minimum}", status, detail,
        value=med, threshold=minimum, weight=1.5,
    )


def _cash_yield_check(db: Any, ticker: str, a: Any, log: QueryLog) -> Check:
    """Is the cash pile earning a real return, or quietly not there?"""
    rows = log.run(
        db, "cash_yield",
        """
        SELECT period_label, cash, other_income
        FROM v_annual WHERE ticker = ? AND cash IS NOT NULL
        ORDER BY yr_idx DESC LIMIT 3
        """,
        (ticker,),
        "Other income against the cash pile -> implied yield on treasury assets.",
    )
    usable = [r for r in rows if r.get("cash") and r.get("other_income") is not None]
    if not usable:
        return Check(
            "cash_yield", "Cash yield > 5%", "skipped",
            "Cash balance or other income not separately reported.",
            threshold=a.cash_yield_min,
        )
    yields = [r["other_income"] / r["cash"] for r in usable if r["cash"] > 0]
    avg = mean(yields)
    if avg is None:
        return Check(
            "cash_yield", "Cash yield > 5%", "skipped",
            "Cash balance is zero or negative.", threshold=a.cash_yield_min,
        )
    if avg >= a.cash_yield_min:
        status = "pass"
        detail = f"Implied yield on cash {avg * 100:.1f}% vs {a.cash_yield_min * 100:.0f}% floor."
    elif avg >= a.cash_yield_min * 0.5:
        status = "watch"
        detail = (
            f"Implied yield {avg * 100:.1f}% is under the {a.cash_yield_min * 100:.0f}% "
            f"floor; other income is a rough proxy for treasury income."
        )
    else:
        status = "watch"
        detail = (
            f"Implied yield {avg * 100:.1f}% is far below the risk-free rate "
            f"({a.risk_free_rate * 100:.1f}%) - the stated cash may be encumbered, "
            f"or other income is not a clean proxy here."
        )
    return Check(
        "cash_yield", "Cash yield > 5%", status, detail,
        value=avg, threshold=a.cash_yield_min, weight=0.75,
    )


def _receivables_check(wc: List[Dict[str, Any]]) -> Check:
    usable = [
        r for r in wc
        if r.get("receivables_growth") is not None and r.get("sales_growth") is not None
    ]
    if not usable:
        return Check(
            "receivables_growth", "Receivables vs sales growth", "skipped",
            "Receivables not separately reported.", weight=1.25,
        )
    recent = usable[-5:]
    gaps = [r["receivables_growth"] - r["sales_growth"] for r in recent]
    avg_gap = mean(gaps)
    outpacing = sum(1 for g in gaps if g > 10.0)
    if avg_gap is None:
        status, detail = "skipped", "Growth gap undefined."
    elif avg_gap <= 5.0 and outpacing <= 1:
        status = "pass"
        detail = f"Receivables grew broadly in line with sales (avg gap {avg_gap:+.1f}pp)."
    elif avg_gap <= 15.0:
        status = "watch"
        detail = (
            f"Receivables outgrew sales by {avg_gap:+.1f}pp on average over "
            f"{len(recent)} years - watch collection quality."
        )
    else:
        status = "fail"
        detail = (
            f"Receivables outgrew sales by {avg_gap:+.1f}pp on average: revenue may "
            f"be booked well ahead of collection."
        )
    return Check(
        "receivables_growth", "Receivables vs sales growth", status, detail,
        value=avg_gap, threshold=5.0, weight=1.25,
    )


def _inventory_check(wc: List[Dict[str, Any]]) -> Check:
    usable = [r for r in wc if r.get("inventory_pct_sales") is not None]
    if len(usable) < 3:
        return Check(
            "inventory_creep", "Inventory / sales creep", "skipped",
            "Inventory not separately reported.", weight=0.75,
        )
    series = [r["inventory_pct_sales"] for r in usable]
    first_half, second_half = series[: len(series) // 2], series[len(series) // 2:]
    delta = (mean(second_half) or 0) - (mean(first_half) or 0)
    if delta <= 1.0:
        status, detail = "pass", f"Inventory intensity stable ({delta:+.1f}pp of sales)."
    elif delta <= 4.0:
        status, detail = "watch", f"Inventory intensity up {delta:+.1f}pp of sales."
    else:
        status = "fail"
        detail = f"Inventory intensity up {delta:+.1f}pp of sales - possible unsold build-up."
    return Check(
        "inventory_creep", "Inventory / sales creep", status, detail,
        value=delta, threshold=1.0, weight=0.75,
    )


def _contingent_check(flags: List[Dict[str, Any]], maximum: float) -> Check:
    series = [r["contingent_pct_networth"] for r in flags if r.get("contingent_pct_networth") is not None]
    if not series:
        return Check(
            "contingent_liabilities", "Contingent liabilities < 5% of net worth", "skipped",
            "Contingent liabilities not disclosed in the pulled data.",
            threshold=maximum, weight=1.25,
        )
    latest = series[-1]
    if latest <= maximum:
        status, detail = "pass", f"Contingent liabilities at {latest:.1f}% of net worth."
    elif latest <= maximum * 3:
        status, detail = "watch", f"Contingent liabilities at {latest:.1f}% of net worth (limit {maximum}%)."
    else:
        status = "fail"
        detail = f"Contingent liabilities at {latest:.1f}% of net worth - far above the {maximum}% limit."
    return Check(
        "contingent_liabilities", f"Contingent liabilities < {maximum}% of net worth",
        status, detail, value=latest, threshold=maximum, weight=1.25,
    )


def _intangibles_check(flags: List[Dict[str, Any]], maximum: float) -> Check:
    series = [r["intangibles_pct_networth"] for r in flags if r.get("intangibles_pct_networth") is not None]
    if not series:
        return Check(
            "intangibles", "Intangibles + goodwill < 10% of net worth", "skipped",
            "Intangibles/goodwill not separately reported.", threshold=maximum, weight=1.25,
        )
    latest = series[-1]
    if latest <= maximum:
        status, detail = "pass", f"Intangibles + goodwill at {latest:.1f}% of net worth."
    elif latest <= maximum * 2.5:
        status = "watch"
        detail = (
            f"Intangibles + goodwill at {latest:.1f}% of net worth, above the "
            f"{maximum}% screen - book value leans on acquisition accounting."
        )
    else:
        status = "fail"
        detail = (
            f"Intangibles + goodwill at {latest:.1f}% of net worth: net worth is "
            f"largely acquisition goodwill and is impairment-exposed."
        )
    return Check(
        "intangibles", f"Intangibles + goodwill < {maximum}% of net worth",
        status, detail, value=latest, threshold=maximum, weight=1.25,
    )


def _provisions_check(flags: List[Dict[str, Any]], maximum: float) -> Check:
    series = [r["provisions_pct_receivables"] for r in flags if r.get("provisions_pct_receivables") is not None]
    if not series:
        return Check(
            "receivable_provisions", "Receivable provisions < 5-10%", "skipped",
            "Provision detail not disclosed in the pulled data.",
            threshold=maximum, weight=0.75,
        )
    latest = series[-1]
    if latest <= maximum / 2:
        status, detail = "pass", f"Provisions at {latest:.1f}% of receivables."
    elif latest <= maximum:
        status, detail = "watch", f"Provisions at {latest:.1f}% of receivables."
    else:
        status = "fail"
        detail = f"Provisions at {latest:.1f}% of receivables - above the {maximum}% avoid line."
    return Check(
        "receivable_provisions", f"Receivable provisions < {maximum}%",
        status, detail, value=latest, threshold=maximum, weight=0.75,
    )


def _depreciation_volatility_check(flags: List[Dict[str, Any]]) -> Check:
    series = [r["depreciation_rate"] for r in flags if r.get("depreciation_rate") is not None]
    if len(series) < 4:
        return Check(
            "depreciation_volatility", "Depreciation-rate stability", "skipped",
            "Gross block not reported for enough years.", weight=0.75,
        )
    sd, avg = stdev(series), mean(series)
    if not avg:
        return Check(
            "depreciation_volatility", "Depreciation-rate stability", "skipped",
            "Average depreciation rate is zero.", weight=0.75,
        )
    cv = (sd or 0) / avg
    if cv <= 0.20:
        status, detail = "pass", f"Depreciation rate steady (CV {cv:.2f})."
    elif cv <= 0.40:
        status, detail = "watch", f"Depreciation rate moves about (CV {cv:.2f})."
    else:
        status = "fail"
        detail = (
            f"Depreciation rate is erratic (CV {cv:.2f}) - a lever often used to "
            f"smooth reported profit."
        )
    return Check(
        "depreciation_volatility", "Depreciation-rate stability", status, detail,
        value=cv, threshold=0.20, weight=0.75,
    )


def _pledge_check(db: Any, ticker: str, log: QueryLog) -> Check:
    rows = log.run(
        db, "promoter_pledge",
        """
        SELECT period_label, promoter_pledge_pct
        FROM shareholding
        WHERE ticker = ? AND promoter_pledge_pct IS NOT NULL
        ORDER BY period_label DESC LIMIT 1
        """,
        (ticker,),
        "Latest disclosed promoter share pledging.",
    )
    if not rows:
        return Check(
            "share_pledging", "Promoter share pledging", "skipped",
            "Pledge data not published for this market/company.", weight=1.25,
        )
    pledge = rows[0]["promoter_pledge_pct"]
    if pledge <= 5.0:
        status, detail = "pass", f"Promoter pledge at {pledge:.1f}%."
    elif pledge <= 25.0:
        status, detail = "watch", f"Promoter pledge at {pledge:.1f}% - monitor."
    else:
        status = "fail"
        detail = f"Promoter pledge at {pledge:.1f}% - forced-sale risk on a price fall."
    return Check(
        "share_pledging", "Promoter share pledging", status, detail,
        value=pledge, threshold=5.0, weight=1.25,
    )


def _promoter_stake_check(db: Any, ticker: str, log: QueryLog) -> Check:
    rows = log.run(
        db, "promoter_trend",
        """
        SELECT period_label, promoter_pct FROM shareholding
        WHERE ticker = ? AND promoter_pct IS NOT NULL
        ORDER BY period_label
        """,
        (ticker,),
        "Promoter holding trend - sustained selling is a governance signal.",
    )
    if len(rows) < 2:
        return Check(
            "promoter_stake", "Promoter stake trend", "skipped",
            "Shareholding history unavailable.", weight=0.75,
        )
    delta = rows[-1]["promoter_pct"] - rows[0]["promoter_pct"]
    if delta >= -1.0:
        status, detail = "pass", f"Promoter stake {delta:+.1f}pp over the disclosed period."
    elif delta >= -5.0:
        status, detail = "watch", f"Promoter stake down {abs(delta):.1f}pp."
    else:
        status = "fail"
        detail = f"Promoter stake down {abs(delta):.1f}pp - sustained insider selling."
    return Check(
        "promoter_stake", "Promoter stake trend", status, detail,
        value=delta, threshold=-1.0, weight=0.75,
    )


# -- scoring ---------------------------------------------------------------


def _score(res: ForensicResult) -> None:
    """Weighted score over the checks that actually ran.

    Skipped checks are excluded from the denominator (they are an unknown, not
    a failure) but they are surfaced separately and drag the §8 completeness
    sub-score, so ignorance can never be laundered into a clean bill of health.
    """
    scored = [c for c in res.checks if c.status in SEVERITY_WEIGHT]
    res.breaches = [c for c in res.checks if c.status in ("fail", "watch")]

    if not scored:
        res.score, res.flag = None, "unknown"
        return

    total_weight = sum(c.weight for c in scored)
    earned = sum(SEVERITY_WEIGHT[c.status] * c.weight for c in scored)
    res.score = earned / total_weight * 100.0

    fails = [c for c in scored if c.status == "fail"]
    watches = [c for c in scored if c.status == "watch"]
    if fails:
        res.flag = "fail"
    elif len(watches) >= 3 or res.score < 80:
        res.flag = "watch"
    else:
        res.flag = "pass"

    # Too little evidence to certify anything.
    if len(scored) < 4:
        res.flag = "unknown"
