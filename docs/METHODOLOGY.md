# Methodology

Every formula, threshold and judgement call the engine makes, and why. Section
numbers refer to [`MASTER_PROMPT.md`](../MASTER_PROMPT.md).

---

## §2 The SQL layer

All figures are staged into SQLite before anything reads them. Derived metrics
are SQL **views**, not Python, so the report can print the definition of every
number back to the reader (`schema.sql` is embedded in the appendix).

**Fiscal labels are preserved as reported.** `Mar-24` stays `Mar-24`; the engine
never renormalises an Indian March year-end into a December one. Markets differ
and a silently shifted year is a silently wrong CAGR.

**The units invariant.** Monetary amounts are stored in the source's natural
major unit (INR crore from Screener.in, absolute USD from Yahoo), recorded on
`company.unit_scale` / `unit_label`. Critically, **`shares_outstanding` is stored
in the same scale as the money** — "crore of shares" alongside "crore of
rupees". Every per-share view is then a plain division with no scale factor to
forget. The Screener loader cross-checks this: shares derived from equity
capital ÷ face value must reproduce the quoted market cap within 5%, else it
falls back to market cap ÷ price (which is what multiple share classes require).

**TTM is not a fiscal year.** Screener.in appends a trailing-twelve-month column
whose header carries no parseable date. Loaded as an annual period it would give
the newest "year" a P&L with no balance sheet behind it — which blanks out book
value and kills the Graham model — and would add a phantom period to every CAGR
window. It is classified `period_type = 'ttm'` and `v_annual` excludes it.

**Sign conventions.** `capex` and `dividends_paid` are stored positive, so
`fcf = cfo − capex` reads the way the formula does. Yahoo reports both negative;
the provider normalises on load.

---

## §3 Fundamentals

### Growth

CAGR over 10 / 7 / 5 / 3-year windows. **CAGR returns `None` when the starting
base is zero or negative** rather than a number: you cannot compound out of a
loss, and a growth *rate* off a negative base is meaningless. Best- and
worst-case growth come from the upper and lower halves of the year-on-year
distribution, so one freak year cannot set the forecast alone.

### Returns and the quality gate

```
ROE  = PAT / net worth
ROCE = EBIT / (net worth + borrowings)
ROIC = EBIT × (1 − tax rate) / (net worth + borrowings − cash)
```

ROIC nets out surplus cash, because cash sitting on the balance sheet is not
capital the operating business is earning a return on.

The gate is **15%**. A metric "clears" it when at least 70% of the reported
years are above the line — a single bad cyclical year should not disqualify a
franchise, but a business below the line in half its history has not earned the
label. A company is flagged high-quality only when all three clear.

### Pricing power

Read off the operating-margin trend: the mean of the second half of the record
against the first, classified expanding (> +100 bps), stable, or compressing
(< −100 bps). Margins that hold or widen through a decade of cost cycles are the
observable footprint of pricing power.

### DuPont

`ROE = NPM × (Sales / Total assets) × (Total assets / Net worth)` and
`ROA = NPM × (Sales / Total assets)` — computed in `v_dupont` so a reader can
see whether returns come from margin, turnover, or leverage.

### EVA and ROIIC

```
EVA   = NOPAT − (capital employed × cost of capital)      [default 10%]
ROIIC = Δ NOPAT / Δ invested capital
```

**ROIIC is measured over a multi-year span, not year-on-year.** One year's capex
rarely produces that same year's profit; the annual ratio is noise. The engine
uses a five-year span (or the longest available) and reports the endpoints.

`intrinsic compounding rate = ROIIC × reinvestment rate` — the rate at which a
business compounds if it keeps reinvesting at the incremental return it has
actually earned. This feeds the Bharat Shah model.

### Buffett's one-dollar test

Cumulative retained earnings against the change in market value across the same
span. Passing means each unit retained created at least one unit of market
value. It is skipped, with a reason, when the price history does not span the
statement history.

---

## §4 Forensic screen

Four severities. The distinction that matters is the fourth:

| Status | Meaning |
|---|---|
| `pass` | the check ran and cleared |
| `watch` | deterioration worth monitoring, not disqualifying alone |
| `fail` | invalidates the investment case |
| `skipped` | **the data was not available — an unknown, never a pass** |

Skipped checks are excluded from the score's denominator (scoring them as
failures would punish a company for its data source's gaps) but they are
surfaced separately and drag the §8 completeness sub-score. Ignorance cannot be
laundered into a clean bill of health. Fewer than four checks running at all
yields flag `unknown`, not `pass`.

### The checks

| Check | Threshold |
|---|---|
| Cumulative PAT vs CFO (10y) | ≥ 1.0× passes, < 0.8× fails |
| Same, in 3-year blocks | a weak *latest* block fails; a weak historic one watches |
| CFO / EBITDA | median ≥ 0.7 |
| Cash yield | ≥ 5%, using other income as a treasury-income proxy |
| Receivables vs sales growth | ≤ +5pp average passes, > +15pp fails |
| Inventory / sales creep | ≤ +1pp of sales |
| Contingent liabilities | < 5% of net worth |
| Intangibles + goodwill | < 10% of net worth (> 25% fails) |
| Receivable provisions | < 10% of receivables |
| Depreciation-rate stability | coefficient of variation ≤ 0.20 |
| Promoter pledging | ≤ 5% |
| Promoter stake trend | no sustained selling |

A `fail` caps the verification score at 60% regardless of everything else.

---

## §5 Technicals and linkage

Indicators are computed in Python, not SQL: EMA and Wilder smoothing are
recursive, which SQLite expresses badly. The price series they read still comes
from the SQL layer.

SMA/EMA 50 and 200 with golden/death-cross state and the date it last flipped;
Wilder RSI(14); MACD(12/26/9); Wilder ATR(14); Bollinger(20, 2σ); volume trend
as the 20-day mean against the preceding 100 days.

**Support and resistance are fractal swing pivots**, clustered into levels within
2% of price — a high with lower highs either side is where supply and demand
actually turned, which is more defensible than round numbers.

The 12-month bias is a weighted score over trend, cross state, RSI, MACD and
volume, and it always ships with the level that would invalidate it.

### Commodity linkage

Candidate commodities are proposed from sector/industry keywords, then have to
earn their place. The commodity is **averaged across each fiscal year**, not
sampled at year-end — the year's average is what the cost base actually saw.
Margin changes are regressed on commodity changes to give a sensitivity in bps
per +10% move, reported alongside the correlation and the sample size.

Three guards keep this honest:

- Fewer than four overlapping fiscal years → labelled `qualitative`, no number.
- **|r| < 0.20 → no direction is claimed at all.** A slope fitted through noise
  would assert a relationship the data does not support, so the finding is
  demoted to "no measurable linkage".
- Where a sensitivity *is* measured, its **sign decides** the tailwind/headwind
  call, not the assumed input/output role — an integrated refiner can be long
  crude even though it consumes it.

News sentiment is a transparent keyword classifier for triage. Estimated price
impact is left blank unless an event study backs it, rather than filled with a
plausible-looking number.

---

## §6 Valuation

Defaults from the reference workbook: FCF growth 20% (years 1–10), discount 7%,
terminal growth 2%, average sustainable P/E 20, cost of capital 10%, margin of
safety 50%. **See the README's warning about the growth default.**

### Warren Buffett Way

```
PV = Σ[t=1..10] FCF₀(1+g)ᵗ / (1+d)ᵗ
   + [FCF₁₀(1+g_term) / (d − g_term)] / (1+d)¹⁰
   + net cash,  ÷ shares
```

Base FCF is the **median of the last three years**, so one capex-light or
capex-heavy year cannot set a decade's forecast. The model refuses to run on
negative base FCF (compounding a loss produces a fictitious value) and when
`d ≤ g_term` (the Gordon formula diverges). It reports the terminal value's
share of the total and warns above 75%.

### Benjamin Graham Way

`√(22.5 × EPS × BVPS)`, cross-checked against `EPS × (8.5 + 2g)` with growth
capped at 15%. **The lower of the two is taken** — this model's job is to be the
floor, so where the two forms disagree the conservative read wins.

### Bharat Shah Way

Earnings power compounded at `ROIIC × reinvestment rate`, exited at a
quality-adjusted multiple (+25% for clearing the 15% gate, −20% for failing it),
discounted back. The compounding rate is **capped at the DCF growth ceiling**:
an uncapped ROIIC × reinvestment on one good stretch produces absurd numbers.

### Buffetology

EPS projected ten years on two legs — historical growth, and sustainable growth
= RoE × retention — then:

```
Projected price = avg sustainable P/E × EPS₁₀
Total gain      = projected price + cumulative dividends
Annual return   = (Total gain / Current price)^(1/n) − 1
```

**On the exponent:** the reference workbook projects ten years but annualises
over **nine** intervals. The engine follows the workbook exactly rather than
silently "correcting" it, exposes it as `return_annualisation_years`, and prints
a caveat whenever it is not 10. Set it to 10 to annualise over the full decade.

Its headline output is an expected annual *return*, not a value, so it is
excluded from the football field — putting a derived per-share figure on the
same bar scale as true intrinsic values would invite a comparison between two
different quantities.

### Margin of safety

The selected intrinsic value is the **median** of the available intrinsic-value
models, halved to set the buy-below line:

- price ≤ buy-below → **buy zone**
- price ≤ buy-below × 1.15 → **near**
- otherwise → **no margin of safety**

Where dispersion exceeds 50% the engine says so explicitly, because a wide
spread usually means the DCF's growth assumption and Graham's asset floor are
describing different businesses.

**Inputs are resolved field-by-field** from the most recent year reporting each
one. Statements do not all land together; reading the newest *row* would drop
book value whenever the P&L is filed ahead of the balance sheet.

---

## §7 Charts

Hand-rolled SVG — no chart library, no CDN, no JavaScript required. Colours come
from CSS custom properties declared on `:root` for light, `prefers-color-scheme`
and an explicit `data-theme` stamp.

**No dual-axis plots.** Where the brief asks for revenue "with margins
overlaid", the engine draws **two stacked panels sharing one x-axis** instead.
Currency and percentage on one plot means two y-scales whose alignment is
arbitrary, which invents a correlation that is not in the data. The panels show
the same relationship without the lie.

Other rules: categorical hue by fixed slot so a series never changes colour when
the set changes; thin marks with hairline grid; selective direct labels (the
endpoint, the extreme) rather than a number on every point; a surface-coloured
halo on labels that cross a rule; and **a table view on every chart** — the
accessible twin, and the required relief for the one palette slot that sits
below 3:1 contrast on the light surface.

The palette is the validated reference instance (blue `#2a78d6`, orange
`#eb6834`, aqua `#1baf7a`, with dark-mode steps), checked with the
colour-vision-deficiency validator in both modes before use.

---

## §8 Verification score

```
Verification = 0.30·completeness + 0.25·source + 0.20·model_agreement
             + 0.15·forensic + 0.10·recency
```

| Component | How it is measured |
|---|---|
| **Completeness** | weighted required fields present (75%) + history depth against a 10-year horizon (25%). Price history is weighted ×4 — without it there is no technical read, no historic multiple band and no $1 test, so its absence must not be diluted by a long list of ratios. |
| **Source quality** | each domain weighted by tier: filing 1.00, screener 0.90, vendor 0.75, estimated 0.45, interpolated 0.25. |
| **Model agreement** | `1 − dispersion` across the intrinsic values, scaled by how many of the three could be computed. |
| **Forensic** | the weighted check score, capped at 0.35 on a `fail` and 0.75 on a `watch`. |
| **Recency** | statement freshness (60%) and price freshness (40%), decaying past a normal reporting lag. |

Three caps sit on top, because some failures cannot be averaged away:

- forensic `fail` → **max 60%**
- forensic `unknown` → max 70%
- no intrinsic value computed → max 55% (there is no call to be confident about)

Bands: ≥ 75% high, ≥ 55% moderate, below that low.

**The score measures rigor, not conviction.** A high number means the analysis
rests on complete, fresh, audited data whose models agree. It does not mean the
stock will go up.

---

## §S Signals, backtesting and the paper book

Merged from [`indian-stock-signal-ai`](https://github.com/hemantsatishjadhav06-ai/indian-stock-signal-ai)
and reimplemented on the standard library. See
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md) for what came across and what did not.

### Market regime

Classified from the market's benchmark (`^NSEI` for India, `^GSPC` for the US,
overridable with `--benchmark`):

| Regime | Condition |
|---|---|
| bullish | price > 50DMA > 200DMA |
| bearish | price < 50DMA < 200DMA |
| range | moving averages intertwined |
| volatile | ATR > 3.5%/day **or** a 5-day move beyond ±6% |

**Volatility overrides direction.** A 6% weekly swing is its own regime whatever
the moving averages say, because position sizing should react to it before
trend-following does.

When the benchmark cannot be fetched the regime is `unknown`, and `unknown`
**gates every strategy off**. That is deliberate: a strategy library that fires
without knowing the market state is worse than one that stays silent.

### The 0-100 scores

Two transparent heuristics, each point attributable to a named reason:

* **Technical** — two archetypes. `trend_momentum` rewards price above the
  200/50DMA, an EMA20>EMA50 stack, RSI 50-70, MACD above signal, ADX>20 with
  +DI>-DI, and price above VWAP. `mean_reversion` rewards a long-term uptrend
  with RSI<35, a tag of the lower Bollinger band, and ADX<20.
* **Fundamental** — growth, profitability, leverage and cash generation banded
  onto 0-100 and averaged. Below three sub-scores it is marked
  `data_complete: false` and the report says the fundamental leg is thin.

These are **tuning heuristics, not validated alpha**. They are scored, gated and
then backtested precisely so a subscriber can see whether they pay.

### Score fusion and gating

```
fused = w_tech·technical + w_fund·fundamental + w_regime·regime + w_news·sentiment
```

Weights come per strategy from `strategies.json`. The library's own rule
governs the outcome:

> Trade only when (a) the fused score clears the threshold **and** (b) all
> required gates pass **and** (c) the risk model approves. Any single hard-gate
> failure is no-trade regardless of score.

Two consequences the code enforces:

* A blocked setup is reported as `no_trade` **with the failing gate named**, not
  hidden. Why a setup was rejected is as useful as the ones that passed.
* An absent fundamental score fuses at a **neutral 50**, never 0 — missing data
  must not read as bad data. The same applies to news sentiment, which has no
  quantified feed wired in and says so rather than inventing one.

### Backtesting

Daily bars, one round-trip cost in basis points (default 30, covering
brokerage + STT + exchange + GST + stamp + slippage).

* **No look-ahead.** Indicators are recomputed on expanding prefixes so each bar
  sees only what was available then; the entry decision and its fill both use
  that same bar's close.
* **Gap-aware fills.** A bar opening through the stop fills at the open, not at
  the stop — which is what would actually have happened.
* **Exits:** stop, target, trend break (close below 50DMA), reversion
  (RSI > 55), or a time stop (40 bars trend / 15 bars mean-reversion).
* **Intraday strategies are labelled.** S1-S4 need intraday bars; on daily bars
  they are an approximation and every result says so.
* The nine strategies collapse to two daily-bar rule sets, so one result is
  reported per **archetype** rather than printing the same two results nine
  times.

Every backtest reports **excess over buy-and-hold**, and the report states
plainly when a rule set underperformed it. A generated signal is not evidence
that trading it pays; that is what the backtest is for, and a backtest is still
the weakest evidence a strategy can offer.

### Risk model and the paper book

```
shares = floor((equity × risk_per_trade%) / (entry − stop))
```

Capped by available equity (no leverage) and by the library's maximum
concurrent positions. The risk manager is the **last** gate: a setup that clears
every score and every strategy gate is still refused here if sizing breaches a
cap, and every refusal carries a reason rather than a silent zero.

The paper book is SQLite-backed and lives in the same database as the analysis.
It is **paper only** — there is no broker adapter and no code path that could
place a real order. A position with no current mark is held at cost and flagged
`marked_at_cost`, never dropped from equity or marked to zero.
