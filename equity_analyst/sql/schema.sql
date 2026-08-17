-- =====================================================================
-- §2  NORMALISED SQL MODEL
-- ---------------------------------------------------------------------
-- Every scraped/pulled figure lands here before any analysis runs.
-- Analysis reads from these tables and views only -- never from raw
-- provider payloads. That is what makes a report reproducible: re-run
-- the same SQL against the same database and you get the same verdict.
--
-- Conventions
--   * period_label  : the fiscal label EXACTLY as reported (Mar-24, Dec-23).
--                     Never normalised away -- fiscal years differ by market.
--   * period_type   : 'annual' | 'quarter' | 'ttm'
--   * currency      : ISO code of the REPORTING currency, stored per row so
--                     multi-market runs cannot silently mix units.
--   * source/as_of  : provenance for §8 verification. Every figure is
--                     traceable to (provider call, retrieval timestamp).
--   * amounts are stored in the reporting currency's natural major unit
--     (e.g. INR crore for Screener.in, USD absolute for Yahoo/OpenBB) and the
--     `unit_scale` on `company` records the multiplier back to base units.
--   * INVARIANT: company.shares_outstanding is stored in the SAME scale as the
--     monetary amounts (so "crore of shares" alongside "crore of rupees").
--     That makes every per-share view a plain division -- net_worth /
--     shares_outstanding is BVPS in currency units, with no scale factor to
--     forget. Providers are responsible for honouring this on load.
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Run bookkeeping
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analysis_run (
    run_id          TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    market          TEXT NOT NULL,
    horizon_years   INTEGER NOT NULL DEFAULT 10,
    created_at      TEXT    NOT NULL,          -- ISO-8601 UTC
    engine_version  TEXT    NOT NULL
);

-- Every assumption that fed a model, so a subscriber can audit the inputs.
CREATE TABLE IF NOT EXISTS assumption (
    run_id      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       REAL,
    unit        TEXT,
    rationale   TEXT,
    overridden  INTEGER NOT NULL DEFAULT 0,    -- 1 = deviates from the default
    PRIMARY KEY (run_id, key),
    FOREIGN KEY (run_id) REFERENCES analysis_run(run_id) ON DELETE CASCADE
);

-- Field-level provenance ledger. Drives §8 completeness + source + recency.
CREATE TABLE IF NOT EXISTS data_coverage (
    run_id      TEXT NOT NULL,
    domain      TEXT NOT NULL,   -- 'income'|'balance'|'cashflow'|'price'|...
    field       TEXT NOT NULL,
    present     INTEGER NOT NULL,-- 1 present, 0 missing
    source      TEXT,            -- provider call that supplied it
    source_tier TEXT,            -- 'filing'|'screener'|'vendor'|'estimated'|'interpolated'
    as_of       TEXT,            -- ISO-8601 retrieval timestamp
    note        TEXT,
    PRIMARY KEY (run_id, domain, field)
);

-- ---------------------------------------------------------------------
-- §1 Company master + staged statements
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company (
    ticker              TEXT PRIMARY KEY,
    name                TEXT,
    market              TEXT,
    exchange            TEXT,
    currency            TEXT,
    unit_scale          REAL DEFAULT 1.0,  -- multiply stored amounts -> base currency units
    unit_label          TEXT,              -- e.g. 'INR crore', 'USD'
    sector              TEXT,
    industry            TEXT,
    face_value          REAL,
    shares_outstanding  REAL,
    source              TEXT,
    as_of               TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshot (
    ticker      TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    price       REAL,
    market_cap  REAL,
    currency    TEXT,
    source      TEXT,
    PRIMARY KEY (ticker, as_of),
    FOREIGN KEY (ticker) REFERENCES company(ticker) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS income_statement (
    ticker              TEXT NOT NULL,
    period_label        TEXT NOT NULL,
    period_end          TEXT,
    period_type         TEXT NOT NULL,
    sales               REAL,
    other_income        REAL,
    expenses            REAL,
    cogs                REAL,
    ebitda              REAL,
    depreciation        REAL,
    ebit                REAL,
    interest            REAL,
    pbt                 REAL,
    tax                 REAL,
    tax_rate            REAL,
    pat                 REAL,
    eps                 REAL,
    dividend_per_share  REAL,
    dividend_payout     REAL,
    source              TEXT,
    as_of               TEXT,
    PRIMARY KEY (ticker, period_type, period_label),
    FOREIGN KEY (ticker) REFERENCES company(ticker) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS balance_sheet (
    ticker                  TEXT NOT NULL,
    period_label            TEXT NOT NULL,
    period_end              TEXT,
    period_type             TEXT NOT NULL,
    equity_capital          REAL,
    reserves                REAL,
    net_worth               REAL,
    borrowings              REAL,
    current_liabilities     REAL,
    other_liabilities       REAL,
    total_liabilities       REAL,
    gross_block             REAL,
    accumulated_dep         REAL,
    net_block               REAL,
    cwip                    REAL,
    investments             REAL,
    intangibles             REAL,
    goodwill                REAL,
    receivables             REAL,
    inventory               REAL,
    cash                    REAL,
    other_assets            REAL,
    total_assets            REAL,
    contingent_liabilities  REAL,
    receivable_provisions   REAL,
    source                  TEXT,
    as_of                   TEXT,
    PRIMARY KEY (ticker, period_type, period_label),
    FOREIGN KEY (ticker) REFERENCES company(ticker) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cash_flow (
    ticker          TEXT NOT NULL,
    period_label    TEXT NOT NULL,
    period_end      TEXT,
    period_type     TEXT NOT NULL,
    cfo             REAL,
    cfi             REAL,
    cff             REAL,
    capex           REAL,
    dividends_paid  REAL,
    net_cash_flow   REAL,
    source          TEXT,
    as_of           TEXT,
    PRIMARY KEY (ticker, period_type, period_label),
    FOREIGN KEY (ticker) REFERENCES company(ticker) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS price_daily (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,     -- ISO date
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    adj_close   REAL,
    volume      REAL,
    source      TEXT,
    as_of       TEXT,
    PRIMARY KEY (ticker, date),
    FOREIGN KEY (ticker) REFERENCES company(ticker) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS shareholding (
    ticker              TEXT NOT NULL,
    period_label        TEXT NOT NULL,
    promoter_pct        REAL,
    promoter_pledge_pct REAL,
    fii_pct             REAL,
    dii_pct             REAL,
    government_pct      REAL,
    public_pct          REAL,
    source              TEXT,
    as_of               TEXT,
    PRIMARY KEY (ticker, period_label),
    FOREIGN KEY (ticker) REFERENCES company(ticker) ON DELETE CASCADE
);

-- Ratios as *reported by the screener*, kept separate from ratios we derive,
-- so a subscriber can see where a number came from and cross-check ours.
CREATE TABLE IF NOT EXISTS ratios_reported (
    ticker          TEXT NOT NULL,
    period_label    TEXT NOT NULL,
    metric          TEXT NOT NULL,
    value           REAL,
    source          TEXT,
    as_of           TEXT,
    PRIMARY KEY (ticker, period_label, metric)
);

-- §5 news + commodity linkage inputs
CREATE TABLE IF NOT EXISTS news_item (
    ticker          TEXT NOT NULL,
    published_at    TEXT,
    headline        TEXT,
    url             TEXT,
    publisher       TEXT,
    sentiment       TEXT,      -- 'positive'|'neutral'|'negative'
    sentiment_score REAL,      -- -1..+1
    est_impact_pct  REAL,      -- estimated price impact, NULL when unquantifiable
    impact_basis    TEXT,      -- 'measured'|'qualitative'
    source          TEXT,
    as_of           TEXT
);

CREATE TABLE IF NOT EXISTS commodity_link (
    ticker      TEXT NOT NULL,
    symbol      TEXT NOT NULL,   -- e.g. 'CL=F'
    name        TEXT,
    role        TEXT,            -- 'input' | 'output'
    rationale   TEXT,
    PRIMARY KEY (ticker, symbol)
);

CREATE TABLE IF NOT EXISTS commodity_price (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    close       REAL,
    unit        TEXT,
    currency    TEXT,
    source      TEXT,
    as_of       TEXT,
    PRIMARY KEY (symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_price_ticker_date  ON price_daily(ticker, date);
CREATE INDEX IF NOT EXISTS idx_income_ticker_type ON income_statement(ticker, period_type);
CREATE INDEX IF NOT EXISTS idx_news_ticker_date   ON news_item(ticker, published_at);

-- =====================================================================
-- DERIVED VIEWS -- §2/§3 arithmetic lives in SQL, not in Python
-- =====================================================================

-- Free cash flow and reinvestment. capex is stored as a POSITIVE outflow.
DROP VIEW IF EXISTS v_cashflow_derived;
CREATE VIEW v_cashflow_derived AS
SELECT
    cf.ticker,
    cf.period_type,
    cf.period_label,
    cf.period_end,
    cf.cfo,
    cf.capex,
    cf.cfo - cf.capex                                       AS fcf,
    CASE WHEN cf.cfo > 0 THEN cf.capex / cf.cfo END         AS reinvestment_rate,
    cf.dividends_paid
FROM cash_flow cf;

-- Ordered annual spine: one row per fiscal year with a stable index so
-- window functions and CAGR windows are deterministic.
DROP VIEW IF EXISTS v_annual;
CREATE VIEW v_annual AS
SELECT
    i.ticker,
    i.period_label,
    COALESCE(i.period_end, i.period_label)                   AS period_end,
    ROW_NUMBER() OVER (PARTITION BY i.ticker
                       ORDER BY COALESCE(i.period_end, i.period_label)) AS yr_idx,
    i.sales, i.other_income, i.expenses, i.cogs, i.ebitda, i.depreciation,
    i.ebit, i.interest, i.pbt, i.tax, i.tax_rate, i.pat, i.eps,
    i.dividend_per_share, i.dividend_payout,
    b.net_worth, b.borrowings, b.total_assets, b.cash, b.investments,
    b.receivables, b.inventory, b.gross_block, b.net_block, b.cwip,
    b.intangibles, b.goodwill, b.contingent_liabilities,
    b.receivable_provisions, b.current_liabilities, b.equity_capital,
    b.reserves,
    c.cfo, c.capex, c.fcf, c.reinvestment_rate, c.dividends_paid
FROM income_statement i
LEFT JOIN balance_sheet b
       ON b.ticker = i.ticker AND b.period_type = 'annual'
      AND b.period_label = i.period_label
LEFT JOIN v_cashflow_derived c
       ON c.ticker = i.ticker AND c.period_type = 'annual'
      AND c.period_label = i.period_label
WHERE i.period_type = 'annual';

-- §3 Margins -- pricing power shows up as stable/expanding margins.
DROP VIEW IF EXISTS v_margins;
CREATE VIEW v_margins AS
SELECT
    ticker, period_label, period_end, yr_idx, sales,
    CASE WHEN sales > 0 AND cogs IS NOT NULL
         THEN (sales - cogs) / sales * 100.0 END      AS gp_margin,
    CASE WHEN sales > 0 THEN ebitda  / sales * 100.0 END AS ebitda_margin,
    CASE WHEN sales > 0 THEN ebit    / sales * 100.0 END AS opm,
    CASE WHEN sales > 0 THEN pat     / sales * 100.0 END AS npm
FROM v_annual;

-- §3 Returns -- the 15% quality gate is applied against these three.
-- ROCE  = EBIT / (net worth + borrowings)
-- ROIC  = NOPAT / invested capital, invested capital net of surplus cash
DROP VIEW IF EXISTS v_returns;
CREATE VIEW v_returns AS
SELECT
    ticker, period_label, period_end, yr_idx,
    CASE WHEN net_worth > 0 THEN pat / net_worth * 100.0 END AS roe,
    CASE WHEN COALESCE(net_worth,0) + COALESCE(borrowings,0) > 0
         THEN ebit / (COALESCE(net_worth,0) + COALESCE(borrowings,0)) * 100.0
    END AS roce,
    CASE WHEN COALESCE(net_worth,0) + COALESCE(borrowings,0) - COALESCE(cash,0) > 0
         THEN (ebit * (1.0 - COALESCE(tax_rate, 0.25)))
              / (COALESCE(net_worth,0) + COALESCE(borrowings,0) - COALESCE(cash,0))
              * 100.0
    END AS roic,
    ebit * (1.0 - COALESCE(tax_rate, 0.25))                       AS nopat,
    COALESCE(net_worth,0) + COALESCE(borrowings,0)                AS capital_employed,
    COALESCE(net_worth,0) + COALESCE(borrowings,0) - COALESCE(cash,0)
                                                                  AS invested_capital
FROM v_annual;

-- §3 DuPont decomposition
--   ROE = NPM x Asset turnover x Financial leverage
--   ROA = NPM x Asset turnover
DROP VIEW IF EXISTS v_dupont;
CREATE VIEW v_dupont AS
SELECT
    ticker, period_label, period_end, yr_idx,
    CASE WHEN sales > 0        THEN pat / sales           END AS npm,
    CASE WHEN total_assets > 0 THEN sales / total_assets  END AS asset_turnover,
    CASE WHEN net_worth > 0    THEN total_assets / net_worth END AS leverage,
    CASE WHEN sales > 0 AND total_assets > 0 AND net_worth > 0
         THEN (pat / sales) * (sales / total_assets) * (total_assets / net_worth) * 100.0
    END AS roe_dupont,
    CASE WHEN sales > 0 AND total_assets > 0
         THEN (pat / sales) * (sales / total_assets) * 100.0
    END AS roa_dupont
FROM v_annual;

-- §3 Leverage & solvency
DROP VIEW IF EXISTS v_leverage;
CREATE VIEW v_leverage AS
SELECT
    ticker, period_label, period_end, yr_idx,
    CASE WHEN net_worth > 0 THEN borrowings / net_worth END        AS debt_to_equity,
    CASE WHEN ebitda   > 0 THEN borrowings / ebitda     END        AS debt_to_ebitda,
    CASE WHEN interest > 0 THEN ebit / interest         END        AS interest_coverage,
    borrowings - COALESCE(cash,0) - COALESCE(investments,0)        AS net_debt
FROM v_annual;

-- §3 Capital-allocation intensity
DROP VIEW IF EXISTS v_capital_allocation;
CREATE VIEW v_capital_allocation AS
SELECT
    ticker, period_label, period_end, yr_idx,
    capex, cfo, fcf, reinvestment_rate,
    CASE WHEN pat > 0          THEN capex / pat          END AS capex_to_pat,
    CASE WHEN depreciation > 0 THEN capex / depreciation END AS capex_to_depreciation,
    pat - COALESCE(dividends_paid, 0)                        AS retained_earnings
FROM v_annual;

-- §4 Quality of earnings: does accrual profit arrive as cash?
DROP VIEW IF EXISTS v_earnings_quality;
CREATE VIEW v_earnings_quality AS
SELECT
    ticker, period_label, period_end, yr_idx,
    pat, cfo, ebitda,
    CASE WHEN ebitda > 0 THEN cfo / ebitda END AS cfo_to_ebitda,
    CASE WHEN pat    > 0 THEN cfo / pat    END AS cfo_to_pat
FROM v_annual;

-- §4 Working-capital creep: receivables/inventory outrunning sales.
DROP VIEW IF EXISTS v_working_capital_trend;
CREATE VIEW v_working_capital_trend AS
SELECT
    a.ticker, a.period_label, a.period_end, a.yr_idx,
    a.sales, a.receivables, a.inventory,
    CASE WHEN a.sales > 0 THEN a.receivables / a.sales * 100.0 END AS receivables_pct_sales,
    CASE WHEN a.sales > 0 THEN a.inventory   / a.sales * 100.0 END AS inventory_pct_sales,
    CASE WHEN p.sales > 0       THEN (a.sales - p.sales) / p.sales * 100.0 END       AS sales_growth,
    CASE WHEN p.receivables > 0 THEN (a.receivables - p.receivables) / p.receivables * 100.0 END AS receivables_growth,
    CASE WHEN p.inventory > 0   THEN (a.inventory - p.inventory) / p.inventory * 100.0 END       AS inventory_growth
FROM v_annual a
LEFT JOIN v_annual p ON p.ticker = a.ticker AND p.yr_idx = a.yr_idx - 1;

-- §4 Balance-sheet red flags, expressed as % of net worth.
DROP VIEW IF EXISTS v_balance_sheet_flags;
CREATE VIEW v_balance_sheet_flags AS
SELECT
    ticker, period_label, period_end, yr_idx, net_worth,
    CASE WHEN net_worth > 0
         THEN COALESCE(contingent_liabilities,0) / net_worth * 100.0 END AS contingent_pct_networth,
    CASE WHEN net_worth > 0
         THEN (COALESCE(intangibles,0) + COALESCE(goodwill,0)) / net_worth * 100.0 END AS intangibles_pct_networth,
    CASE WHEN receivables > 0
         THEN COALESCE(receivable_provisions,0) / receivables * 100.0 END AS provisions_pct_receivables,
    CASE WHEN gross_block > 0
         THEN depreciation / gross_block * 100.0 END AS depreciation_rate
FROM v_annual;

-- §3 Valuation ratios per fiscal year, marked to the price on/just before
-- the fiscal close. Gives the 10-yr max P/E and max EV/EBITDA context bands.
DROP VIEW IF EXISTS v_historic_valuation;
CREATE VIEW v_historic_valuation AS
SELECT
    a.ticker, a.period_label, a.period_end, a.yr_idx,
    px.close                                                     AS price_at_close,
    co.shares_outstanding,
    CASE WHEN a.eps > 0 THEN px.close / a.eps END                AS pe,
    CASE WHEN a.net_worth > 0 AND co.shares_outstanding > 0
         THEN px.close / (a.net_worth / co.shares_outstanding) END AS pb,
    CASE WHEN a.sales > 0 AND co.shares_outstanding > 0
         THEN px.close / (a.sales / co.shares_outstanding) END   AS ps,
    CASE WHEN a.cfo > 0 AND co.shares_outstanding > 0
         THEN px.close / (a.cfo / co.shares_outstanding) END     AS pcf,
    CASE WHEN a.ebitda > 0 AND co.shares_outstanding > 0
         THEN (px.close * co.shares_outstanding
               + COALESCE(a.borrowings,0) - COALESCE(a.cash,0)) / a.ebitda END AS ev_ebitda
FROM v_annual a
JOIN company co ON co.ticker = a.ticker
LEFT JOIN price_daily px
       ON px.ticker = a.ticker
      AND px.date = (SELECT MAX(p2.date) FROM price_daily p2
                      WHERE p2.ticker = a.ticker AND p2.date <= a.period_end);
