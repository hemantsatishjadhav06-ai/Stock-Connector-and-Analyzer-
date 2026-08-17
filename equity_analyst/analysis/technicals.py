"""§5 Technical analysis.

Trend (SMA/EMA 50/200 and the golden/death-cross state), momentum (RSI, MACD),
volatility (ATR, Bollinger), volume trend, and support/resistance -- then a
12-month bias with the levels that would invalidate it.

Indicators are computed here in plain Python rather than in SQL: they are
recursive (EMA, Wilder smoothing) and window-based, which SQLite expresses
badly. The price series they read still comes from the SQL layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import QueryLog, mean, stdev


@dataclass
class TechnicalResult:
    prices: List[Dict[str, Any]] = field(default_factory=list)
    last_close: Optional[float] = None
    as_of: Optional[str] = None
    sma_fast: Optional[float] = None
    sma_slow: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    cross_state: str = "unknown"        # golden | death | none | unknown
    cross_date: Optional[str] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    bollinger_upper: Optional[float] = None
    bollinger_lower: Optional[float] = None
    bollinger_mid: Optional[float] = None
    percent_b: Optional[float] = None
    volume_trend: Optional[str] = None
    volume_ratio: Optional[float] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    vwap: Optional[float] = None
    relative_volume: Optional[float] = None
    support: List[float] = field(default_factory=list)
    resistance: List[float] = field(default_factory=list)
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None
    bias: str = "unknown"               # bullish | neutral | bearish | unknown
    bias_score: Optional[float] = None
    rationale: List[str] = field(default_factory=list)
    invalidation: Dict[str, Any] = field(default_factory=dict)
    series: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def analyse(db: Any, ticker: str, config: Any, log: QueryLog) -> TechnicalResult:
    a = config.assumptions
    res = TechnicalResult()

    res.prices = log.run(
        db, "price_series",
        """
        SELECT date, open, high, low, close, adj_close, volume
        FROM price_daily WHERE ticker = ? ORDER BY date
        """,
        (ticker,),
        "Daily OHLCV series feeding every technical indicator.",
    )
    if len(res.prices) < 30:
        res.notes.append(
            f"Only {len(res.prices)} sessions available - technical read suppressed."
        )
        return res

    closes = [r["close"] for r in res.prices]
    highs = [r.get("high") or r["close"] for r in res.prices]
    lows = [r.get("low") or r["close"] for r in res.prices]
    volumes = [r.get("volume") or 0.0 for r in res.prices]
    dates = [r["date"] for r in res.prices]

    res.last_close = closes[-1]
    res.as_of = dates[-1]

    sma_f = sma_series(closes, a.sma_fast)
    sma_s = sma_series(closes, a.sma_slow)
    res.sma_fast, res.sma_slow = sma_f[-1], sma_s[-1]
    res.ema_fast = ema_series(closes, a.sma_fast)[-1]
    res.ema_slow = ema_series(closes, a.sma_slow)[-1]
    res.cross_state, res.cross_date = _cross_state(sma_f, sma_s, dates)

    res.rsi = rsi(closes, a.rsi_period)
    macd_line, signal_line, histogram = macd(closes)
    res.macd, res.macd_signal, res.macd_histogram = macd_line, signal_line, histogram

    res.atr = atr(highs, lows, closes, a.atr_period)
    if res.atr and res.last_close:
        res.atr_pct = res.atr / res.last_close * 100.0

    upper, mid, lower = bollinger(closes, a.bollinger_period, a.bollinger_sigma)
    res.bollinger_upper, res.bollinger_mid, res.bollinger_lower = upper, mid, lower
    if upper is not None and lower is not None and upper > lower:
        res.percent_b = (res.last_close - lower) / (upper - lower)

    res.volume_trend, res.volume_ratio = _volume_trend(volumes)

    adx_s, plus_s, minus_s = adx_series(highs, lows, closes, a.adx_period)
    res.adx, res.plus_di, res.minus_di = adx_s[-1], plus_s[-1], minus_s[-1]
    vwap_s = vwap_series(highs, lows, closes, volumes)
    res.vwap = vwap_s[-1]
    res.relative_volume = relative_volume(volumes)

    window = res.prices[-252:] if len(res.prices) >= 252 else res.prices
    res.fifty_two_week_high = max(r["close"] for r in window)
    res.fifty_two_week_low = min(r["close"] for r in window)
    res.support, res.resistance = _support_resistance(res.prices, res.last_close)

    res.series = {
        "date": dates,
        "close": closes,
        "sma_fast": sma_f,
        "sma_slow": sma_s,
        "volume": volumes,
        "rsi": rsi_series(closes, a.rsi_period),
    }

    _bias(res, a)
    return res


# -- indicators ------------------------------------------------------------


def sma_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        out.append(running / period if i >= period - 1 else None)
    return out


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    if len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out: List[Optional[float]] = [None] * (period - 1)
    prev = sum(values[:period]) / period
    out.append(prev)
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi_series(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's RSI."""
    if len(values) <= period:
        return [None] * len(values)
    out: List[Optional[float]] = [None] * period
    gains, losses = [], []
    for prev, cur in zip(values, values[1:]):
        change = cur - prev
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out.append(_rsi_from(avg_gain, avg_loss))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(_rsi_from(avg_gain, avg_loss))
    return out[: len(values)]


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    series = rsi_series(values, period)
    return series[-1] if series else None


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
):
    """Returns ``(macd, signal, histogram)`` at the latest bar."""
    if len(values) < slow + signal:
        return None, None, None
    ef, es = ema_series(values, fast), ema_series(values, slow)
    line = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ef, es)
    ]
    clean = [v for v in line if v is not None]
    if len(clean) < signal:
        return None, None, None
    sig = ema_series(clean, signal)
    macd_v, sig_v = clean[-1], sig[-1]
    hist = macd_v - sig_v if sig_v is not None else None
    return macd_v, sig_v, hist


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Optional[float]:
    if len(closes) <= period:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period  # Wilder smoothing
    return value


def bollinger(values: Sequence[float], period: int = 20, sigma: float = 2.0):
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    mid = sum(window) / period
    sd = stdev(window) or 0.0
    return mid + sigma * sd, mid, mid - sigma * sd


def adx_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
):
    """Wilder's ADX with +DI / -DI.

    Returns ``(adx, plus_di, minus_di)`` as full-length lists so the caller can
    align them with dates. Directional movement is only counted when one side
    genuinely exceeds the other, which is what separates trend from chop.
    """
    n = len(closes)
    empty = [None] * n
    if n <= period + 1:
        return empty, empty, empty

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    def wilder(seq):
        out, value = [], sum(seq[:period]) / period
        out.append(value)
        for v in seq[period:]:
            value = (value * (period - 1) + v) / period
            out.append(value)
        return out

    atr_s, plus_s, minus_s = wilder(trs), wilder(plus_dm), wilder(minus_dm)

    plus_di, minus_di, dx = [], [], []
    for a, p, m in zip(atr_s, plus_s, minus_s):
        if not a:
            plus_di.append(None); minus_di.append(None); dx.append(None)
            continue
        pdi, mdi = 100.0 * p / a, 100.0 * m / a
        plus_di.append(pdi)
        minus_di.append(mdi)
        total = pdi + mdi
        dx.append(abs(pdi - mdi) / total * 100.0 if total else None)

    clean = [d for d in dx if d is not None]
    adx_vals: List[Optional[float]] = []
    if len(clean) >= period:
        value = sum(clean[:period]) / period
        adx_vals = [None] * (period - 1) + [value]
        for d in clean[period:]:
            value = (value * (period - 1) + d) / period
            adx_vals.append(value)

    # Left-pad each series back to the length of the input.
    def pad(seq):
        return [None] * (n - len(seq)) + list(seq) if len(seq) <= n else list(seq)[-n:]

    return pad(adx_vals), pad(plus_di), pad(minus_di)


def vwap_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    window: Optional[int] = None,
) -> List[Optional[float]]:
    """Volume-weighted average price.

    ``window=None`` gives the cumulative VWAP over the whole series; a window
    gives a rolling VWAP. On daily bars this is an anchored approximation --
    true intraday VWAP needs intraday bars, and the report says so.
    """
    n = len(closes)
    out: List[Optional[float]] = []
    pv_cum = vol_cum = 0.0
    typical = [
        (highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)
    ]
    for i in range(n):
        vol = volumes[i] or 0.0
        pv_cum += typical[i] * vol
        vol_cum += vol
        if window and i >= window:
            j = i - window
            pv_cum -= typical[j] * (volumes[j] or 0.0)
            vol_cum -= volumes[j] or 0.0
        out.append(pv_cum / vol_cum if vol_cum > 0 else None)
    return out


def relative_volume(volumes: Sequence[float], period: int = 20) -> Optional[float]:
    """Latest volume against its recent average -- the RVOL several strategies gate on."""
    if len(volumes) < period + 1:
        return None
    base = sum(volumes[-(period + 1):-1]) / period
    return (volumes[-1] / base) if base > 0 else None


# -- derived reads ---------------------------------------------------------


def _cross_state(
    fast: List[Optional[float]], slow: List[Optional[float]], dates: List[str]
):
    """Golden/death cross state and the date it last flipped."""
    pairs = [
        (i, f, s)
        for i, (f, s) in enumerate(zip(fast, slow))
        if f is not None and s is not None
    ]
    if not pairs:
        return "unknown", None
    state = "golden" if pairs[-1][1] > pairs[-1][2] else "death"
    cross_date = None
    for (i, f, s), (_, pf, ps) in zip(reversed(pairs), reversed(pairs[:-1])):
        if (f > s) != (pf > ps):
            cross_date = dates[i]
            break
    return state, cross_date


def _volume_trend(volumes: Sequence[float]):
    recent, base = volumes[-20:], volumes[-120:-20]
    if not recent or not base:
        return None, None
    r, b = mean(recent), mean(base)
    if not b:
        return None, None
    ratio = r / b
    if ratio >= 1.25:
        return "expanding", ratio
    if ratio <= 0.8:
        return "contracting", ratio
    return "steady", ratio


def _support_resistance(prices: List[Dict[str, Any]], last: float):
    """Swing pivots clustered into levels, nearest-to-price first.

    A fractal pivot (a high with lower highs either side) is where supply and
    demand actually turned, which is more defensible than round numbers.
    """
    window = prices[-500:]
    if len(window) < 21:
        return [], []

    highs, lows = [], []
    span = 5
    for i in range(span, len(window) - span):
        hi = window[i].get("high") or window[i]["close"]
        lo = window[i].get("low") or window[i]["close"]
        neighbourhood = window[i - span: i + span + 1]
        if hi >= max((r.get("high") or r["close"]) for r in neighbourhood):
            highs.append(hi)
        if lo <= min((r.get("low") or r["close"]) for r in neighbourhood):
            lows.append(lo)

    def cluster(levels: List[float]) -> List[float]:
        if not levels:
            return []
        levels = sorted(levels)
        tolerance = last * 0.02  # merge pivots within 2% into one level
        groups, current = [], [levels[0]]
        for lv in levels[1:]:
            if lv - current[-1] <= tolerance:
                current.append(lv)
            else:
                groups.append(sum(current) / len(current))
                current = [lv]
        groups.append(sum(current) / len(current))
        return groups

    support = [lv for lv in cluster(lows) if lv < last]
    resistance = [lv for lv in cluster(highs) if lv > last]
    support.sort(key=lambda x: last - x)
    resistance.sort(key=lambda x: x - last)
    return support[:3], resistance[:3]


def _bias(res: TechnicalResult, a: Any) -> None:
    """Score the evidence into a 12-month bias, and say what breaks it."""
    score = 0.0
    why = res.rationale
    close = res.last_close

    if res.sma_slow and close:
        if close > res.sma_slow:
            score += 1.5
            why.append(f"Price is above the {a.sma_slow}-day SMA (primary uptrend).")
        else:
            score -= 1.5
            why.append(f"Price is below the {a.sma_slow}-day SMA (primary downtrend).")

    if res.cross_state == "golden":
        score += 1.0
        why.append(
            f"{a.sma_fast}/{a.sma_slow} SMA in a golden-cross configuration"
            + (f" since {res.cross_date}." if res.cross_date else ".")
        )
    elif res.cross_state == "death":
        score -= 1.0
        why.append(
            f"{a.sma_fast}/{a.sma_slow} SMA in a death-cross configuration"
            + (f" since {res.cross_date}." if res.cross_date else ".")
        )

    if res.rsi is not None:
        if res.rsi >= 70:
            score -= 0.5
            why.append(f"RSI {res.rsi:.0f} - overbought, vulnerable to mean reversion.")
        elif res.rsi <= 30:
            score += 0.5
            why.append(f"RSI {res.rsi:.0f} - oversold, stretched to the downside.")
        else:
            why.append(f"RSI {res.rsi:.0f} - neutral momentum.")

    if res.macd_histogram is not None:
        if res.macd_histogram > 0:
            score += 0.75
            why.append("MACD above its signal line - momentum improving.")
        else:
            score -= 0.75
            why.append("MACD below its signal line - momentum fading.")

    if res.volume_trend == "expanding" and score > 0:
        score += 0.5
        why.append("Volume expanding into the advance - participation confirms it.")
    elif res.volume_trend == "contracting" and score > 0:
        score -= 0.25
        why.append("Volume contracting into the advance - thin participation.")

    res.bias_score = score
    res.bias = "bullish" if score >= 1.5 else "bearish" if score <= -1.5 else "neutral"

    # What would prove this read wrong.
    if res.bias == "bullish":
        level = res.support[0] if res.support else res.sma_slow
        res.invalidation = {
            "direction": "close below",
            "level": level,
            "note": (
                "A weekly close below this level breaks the structure the bullish "
                "read rests on."
            ),
        }
    elif res.bias == "bearish":
        level = res.resistance[0] if res.resistance else res.sma_slow
        res.invalidation = {
            "direction": "close above",
            "level": level,
            "note": (
                "A weekly close above this level would end the lower-high sequence "
                "and void the bearish read."
            ),
        }
    else:
        res.invalidation = {
            "direction": "range break",
            "level": res.resistance[0] if res.resistance else None,
            "lower": res.support[0] if res.support else None,
            "note": "Neutral until the range resolves; trade the break, not the middle.",
        }
