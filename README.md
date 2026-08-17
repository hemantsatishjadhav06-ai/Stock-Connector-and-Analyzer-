# Stock Connector & Analyzer

An autonomous equity research engine. Point it at a ticker and it pulls the
company's fundamental history from the right screener for that market,
normalises everything into a SQL layer, runs fundamental, forensic, technical
and commodity-linkage analysis, values the business four independent ways,
applies a margin-of-safety rule, and emits a chart-first HTML report that
carries an explicit **verification percentage**.

It also generates **trade signals** from a nine-strategy library, detects the
market regime, and backtests the rules against costs — features merged in from
[`indian-stock-signal-ai`](https://github.com/hemantsatishjadhav06-ai/indian-stock-signal-ai)
so that data collection, analysis and reporting run as one pipeline.

It implements the analyst brief in [`MASTER_PROMPT.md`](MASTER_PROMPT.md)
end to end. The methodology, every formula and every threshold, is documented
in [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md); the module map is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

> **Research and education for individual investors — not personalised
> investment advice.** No output here is a recommendation to buy or sell.

---

## Why it is built this way

Three rules shape the whole codebase:

**Verify, don't assert.** Every headline claim carries a confidence score and a
traceable source. When a figure cannot be sourced, the engine says so and the
verification score falls — it is never filled with a plausible-looking number.
A forensic check that could not run is reported as *unknown*, never as a pass.

**Reproducible.** Nothing is computed from a raw scrape. Everything lands in
SQLite first, the analysis reads from SQL views, and the report prints back the
schema and every query it ran. Re-run the same SQL against the same database
and you get the same verdict.

**Margin of safety over optimism.** Where the models disagree, the engine takes
the median rather than the flattering read, halves it to set the buy-below
line, and explains the divergence.

---

## Install and run

No required dependencies — Python 3.9+ standard library only.

```bash
git clone <this repo> && cd Stock-Connector-and-Analyzer-
python3 -m equity_analyst.cli AAPL --market US --out reports/aapl.html
```

India routes through Screener.in automatically:

```bash
python3 -m equity_analyst.cli RELIANCE.NS --market India --out reports/reliance.html
```

Add trade signals and a cost-aware backtest:

```bash
python3 -m equity_analyst.cli RELIANCE.NS --market India --signals --backtest \
        --out reports/reliance.html
```

Try it with no network at all, against the bundled synthetic company:

```bash
python3 -m equity_analyst.cli DEMO.SYNTH --offline data/samples/demo_industrials.json \
        --no-network --commodity "CL=F" --signals --backtest --out reports/demo.html
```

Optionally install it as a command (`pip install -e .` → `equity-analyst AAPL`),
and `pip install -e '.[openbb]'` to add OpenBB as a data source.

### Useful flags

| Flag | Purpose |
|---|---|
| `--db run.sqlite` | keep the SQL layer on disk to query yourself |
| `--save-bundle x.json` | snapshot the raw pull so a run can be replayed exactly |
| `--offline x.json` / `--no-network` | replay a snapshot; air-gapped operation |
| `--commodity CL=F` | force a commodity linkage instead of inferring one |
| `--fcf-growth 0.08` | override a valuation assumption (see below) |
| `--signals` | score the 9-strategy library and emit trade setups |
| `--backtest` | run the cost-aware backtester over the price history |
| `--cost-bps 45` | round-trip trading cost in basis points (default 30) |
| `--benchmark ^NSEI` | regime benchmark (defaults per market) |

The process exits `2` when the forensic screen fails, so it composes with CI.

---

## ⚠️ The one assumption you should almost always override

The reference workbook's defaults are **20% FCF growth for ten years** against a
**7% discount rate**. Compounding 20% for a decade is a rare-company assumption;
paired with a 7% discount rate it will make most businesses look cheap.

The engine does not quietly fix this — it uses the documented defaults, then
flags them: the DCF reports what share of its value sits in the terminal value
and warns when growth exceeds 15%. For a mature business, override it:

```bash
python3 -m equity_analyst.cli AAPL --fcf-growth 0.06 --discount-rate 0.09
```

Every override is recorded in the `assumption` table and printed in the report
appendix marked `overridden`, so a reader can see exactly which levers moved.

---

## What comes out

A single self-contained HTML file — no CDN, no JavaScript required, readable in
light and dark mode:

1. **Verdict card** — buy-zone / fair / expensive, quality and forensic flags,
   technical bias, and the headline verification %.
2. **Valuation** — football field plus the model table, caveats and the
   buy-below line.
3. **Fundamentals** — revenue and margins, returns against the 15% quality gate,
   free cash flow and reinvestment, growth windows, capital allocation.
4. **Technicals** — price with SMA 50/200, RSI, support/resistance, a 12-month
   bias and the level that would invalidate it.
5. **Trade signals** *(`--signals`)* — market regime, all nine strategies scored
   against the trade threshold, and for each one either an entry/stop/target or
   the named gate that blocked it.
6. **Backtest** *(`--backtest`)* — equity curve against buy-and-hold, win rate,
   expectancy, drawdown, Sharpe and exposure, all after costs.
7. **News & commodity linkage** — measured sensitivity where the data supports
   it, explicitly labelled qualitative where it does not.
8. **Forensics** — every check with pass / watch / fail / skipped.
9. **Verification** — the formula, each sub-score, and the evidence behind it.
10. **Appendix** — assumptions, source coverage, and every SQL query executed.

The terminal prints the same verdict card for scripting.

### Two lenses, deliberately kept apart

The valuation models ask *what is this business worth* over ten years. The
strategy engine asks *is this a tradeable setup right now* over days to weeks.
They share the SQL layer and nothing else, and the report keeps them in separate
sections. A stock can be a buy on one and a no-trade on the other — collapsing
them into a single opinion would hide exactly the disagreement worth seeing.

---

## The four valuation models

| Model | Output | Reads as |
|---|---|---|
| **Warren Buffett Way** | 10-year FCF DCF + Gordon terminal, per share | central |
| **Benjamin Graham Way** | √(22.5 × EPS × BVPS), cross-checked against EPS × (8.5 + 2g) | the floor |
| **Bharat Shah Way** | earnings power compounded at ROIIC × reinvestment rate | the quality-adjusted ceiling |
| **Buffetology** | expected annual return, two growth legs | a return, not a value |

A model that cannot be computed reports **why** instead of disappearing —
because a missing model widens dispersion, which is itself information and is
scored in the verification total.

---

## Data sources and graceful degradation

| Market | Primary | Fallback |
|---|---|---|
| India | Screener.in (audited-filing aggregator) | Yahoo `.NS`, TwelveData, Indian-Stock-Market-API |
| US | OpenBB (when installed) | Yahoo, TwelveData, Alpaca |
| Everywhere else | OpenBB, when installed | Yahoo, TwelveData |
| Any | an offline JSON bundle | — |

Keyed providers join the chain automatically when their credentials are present
in the environment — no key, no provider, and the absence shows up as a coverage
gap rather than an error:

```bash
export TWELVEDATA_API_KEY=...      # free tier; works from datacenter IPs
export ALPACA_KEY_ID=... ALPACA_SECRET_KEY=...
export INDIAN_API_BASE_URL=https://your-indian-stock-market-api
```

Providers are merged first-supplier-wins per domain, so the higher-tier source
always sets the number and a lower-tier one only fills gaps. Every fetch can
fail; failure is recorded as a coverage gap and flows into the verification
score rather than raising.

This matters in practice: public finance endpoints rate-limit shared IPs
aggressively. A run that loses its price feed still produces a valuation from
the screener's fundamentals — it simply reports a lower confidence, publishes no
technical bias, and lists the gap.

**If Yahoo returns 429 for you**, you are sharing an egress IP with other
traffic (common on CI runners, containers and VPNs). The client backs off
exponentially, honours `Retry-After`, and — if `curl_cffi` is installed —
retries once with a Chrome TLS fingerprint, since Yahoo throttles by
fingerprint as well as by address:

```bash
pip install curl_cffi     # optional; silently skipped if unavailable
```

That trick does not survive a TLS-inspecting proxy. The durable fix is a
**TwelveData** key, which is exactly why that provider is here: it serves
datacenter IPs where Yahoo will not. Failing both, capture one good pull with
`--save-bundle` and iterate offline against it.

---

## Development

```bash
python3 -m pytest tests/ -q          # 145 tests, no network required
python3 tools/make_sample_bundle.py  # regenerate the synthetic fixture
```

The sample bundle under `data/samples/` is **synthetic** — generated by a seeded
script, describing an invented company, and labelled as such everywhere it
appears. It exists so the engine can be exercised and demonstrated offline.

```
equity_analyst/
  sql/schema.sql     tables + derived views (the arithmetic lives here)
  providers/         Screener.in · Yahoo · OpenBB · TwelveData · Alpaca · offline
  analysis/          §3 fundamentals · §4 forensics · §5 technicals · linkage · regime
  valuation/         §6 the four models + margin of safety
  strategies/        the 9-strategy library + score fusion
  backtest.py        cost-aware, gap-aware daily-bar backtester
  trading/           paper book + risk model
  verification.py    §8 the confidence score
  report/            §7 SVG charts + the HTML dashboard
```
