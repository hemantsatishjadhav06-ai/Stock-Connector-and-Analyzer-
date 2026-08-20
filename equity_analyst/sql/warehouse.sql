-- =====================================================================
-- WAREHOUSE -- the multi-company, persistent layer
-- ---------------------------------------------------------------------
-- schema.sql models ONE company for ONE run and is rebuilt each time.
-- This file adds the tables that must SURVIVE runs: the company index,
-- name->ticker resolutions, cached reports and the refresh log.
--
-- Both files are applied to the same SQLite database when the engine runs
-- in warehouse mode, so a cached report can be joined back to the staged
-- statements that produced it.
-- =====================================================================

-- Every company the warehouse knows about, with its freshness state.
CREATE TABLE IF NOT EXISTS company_index (
    ticker              TEXT PRIMARY KEY,
    name                TEXT,
    normalized_name     TEXT,           -- lowercased, punctuation stripped
    market              TEXT,
    exchange            TEXT,
    currency            TEXT,
    sector              TEXT,
    industry            TEXT,
    first_seen          TEXT,
    -- Freshness is tracked per domain: prices go stale daily, statements
    -- only when a company files. One timestamp for both would force a full
    -- re-scrape every day just to refresh a quote.
    last_refreshed      TEXT,
    last_price_refresh  TEXT,
    last_statement_refresh TEXT,
    refresh_status      TEXT,           -- ok | partial | failed | pending
    refresh_error       TEXT,
    refresh_count       INTEGER DEFAULT 0,
    -- Denormalised headline results so the index page renders from one
    -- query instead of parsing every cached report.
    price               REAL,
    verification_score  REAL,
    valuation_zone      TEXT,
    forensic_flag       TEXT,
    technical_bias      TEXT,
    intrinsic_value     REAL,
    buy_below           REAL
);

-- Resolutions from a typed query to a ticker. Caching these is what makes
-- the second search for "reliance" instant, and it records WHICH source
-- resolved it so a bad match can be traced.
CREATE TABLE IF NOT EXISTS company_alias (
    query           TEXT NOT NULL,      -- normalized user input
    ticker          TEXT NOT NULL,
    display_name    TEXT,
    market          TEXT,
    exchange        TEXT,
    source          TEXT,               -- sec | screener.in | yahoo | manual
    score           REAL,               -- 0-1 match confidence
    as_of           TEXT,
    PRIMARY KEY (query, ticker)
);

-- The full US ticker universe from SEC, so name search works offline and
-- without hitting SEC on every keystroke.
CREATE TABLE IF NOT EXISTS ticker_directory (
    ticker          TEXT NOT NULL,
    name            TEXT,
    normalized_name TEXT,
    market          TEXT NOT NULL,
    exchange        TEXT,
    cik             TEXT,
    source          TEXT,
    as_of           TEXT,
    PRIMARY KEY (market, ticker)
);

-- Rendered reports, so a page load does not re-run the whole pipeline.
CREATE TABLE IF NOT EXISTS report_cache (
    ticker          TEXT PRIMARY KEY,
    generated_at    TEXT,
    engine_version  TEXT,
    html            TEXT,
    summary_json    TEXT,               -- verdict card fields for the index
    duration_ms     INTEGER
);

-- One row per refresh attempt: the audit trail for "when was this last
-- updated and did it work".
CREATE TABLE IF NOT EXISTS refresh_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT,               -- ok | partial | failed
    verification    REAL,
    gaps            TEXT,
    error           TEXT,
    trigger         TEXT                -- web | scheduler | cli
);

CREATE INDEX IF NOT EXISTS idx_index_name    ON company_index(normalized_name);
CREATE INDEX IF NOT EXISTS idx_index_stale   ON company_index(last_refreshed);
CREATE INDEX IF NOT EXISTS idx_dir_name      ON ticker_directory(normalized_name);
CREATE INDEX IF NOT EXISTS idx_alias_query   ON company_alias(query);
CREATE INDEX IF NOT EXISTS idx_refresh_ticker ON refresh_log(ticker, started_at);
