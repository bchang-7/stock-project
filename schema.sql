-- SEC EDGAR + yfinance -> SQLite, consumer-staples fundamentals, FY2014-2025.
--
-- UNITS
--   All money columns are RAW DOLLARS as reported to EDGAR (not millions,
--   not thousands). e.g. Coca-Cola FY2024 revenue = 47061000000.0
--   eps                is dollars per share (XBRL unit USD/shares).
--   shares_outstanding is a share COUNT (XBRL unit shares), not dollars.
--   market.price / market_cap are raw dollars from yfinance; beta is a ratio.
--
-- FISCAL YEAR KEY
--   fiscal_year is the ECONOMIC year, derived from the period END date:
--       fiscal_year = end.year if end.month >= 6 else end.year - 1
--   This matches SEC's own `frame` convention and makes fiscal_year comparable
--   across all 34 tickers regardless of fiscal calendar. It is NOT always the
--   company's own "FY" label -- for WMT, TGT, STZ, BF.B, CASY, SJM and GIS the
--   key is one lower than the label the company uses. The native label is
--   recorded in fundamentals_provenance.native_fy_label.
--
-- RESTATEMENT POLICY
--   Values are AS-ORIGINALLY-REPORTED by default (earliest 10-K filing whose
--   period matches), selected chain-wide across the tag fallback list, so an
--   early value under a deprecated tag beats a later comparative under a newer
--   tag. Set the extractor policy flag to 'latest' for as-restated instead.
--
-- DERIVED COLUMNS (not reported to EDGAR under any single tag)
--   ebitda           = OperatingIncomeLoss + DepreciationDepletionAndAmortization
--   total_debt       = long-term debt (noncurrent) + current debt, where the
--                      current component avoids EDGAR's nested debt aggregates
--   free_cash_flow   = NetCashProvidedByUsedInOperatingActivities - capex
--                      EDGAR reports no FCF element, so this cannot be
--                      validated against the filing; it is cross-checked
--                      against yfinance in the verification step instead.

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker TEXT,
    fiscal_year INTEGER,
    revenue REAL,
    cogs REAL,
    sga REAL,
    advertising REAL,
    rnd REAL,
    other_operating_expenses REAL,
    depreciation_amortization REAL,
    interest_expense REAL,
    income_tax_expense REAL,
    net_income REAL,
    ebitda REAL,
    eps REAL,
    accounts_receivable REAL,
    inventory REAL,
    accounts_payable REAL,
    total_equity REAL,
    total_debt REAL,
    current_assets REAL,
    current_liabilities REAL,
    cash REAL,
    shares_outstanding REAL,
    capex REAL,
    free_cash_flow REAL,
    PRIMARY KEY (ticker, fiscal_year)
);

CREATE TABLE IF NOT EXISTS market (
    ticker TEXT PRIMARY KEY,
    price REAL,
    market_cap REAL,
    beta REAL,
    sector TEXT
);

-- One row per (ticker, fiscal_year, field) explaining exactly where the number
-- in `fundamentals` came from. Never joined into the two tables above.
CREATE TABLE IF NOT EXISTS fundamentals_provenance (
    ticker TEXT,
    fiscal_year INTEGER,
    field TEXT,              -- column name in `fundamentals`
    tag TEXT,                -- winning us-gaap/dei tag, or the derivation formula
    filed TEXT,              -- filing date (ISO) the value was taken from
    form TEXT,               -- 10-K or 10-K/A
    period_end TEXT,         -- fiscal period end (ISO)
    filing_lag_days INTEGER, -- filed - period_end; >200 means this is a later
                             -- comparative, NOT an original filing
    source_cik TEXT,         -- which CIK supplied it (BG spans two entities)
    native_fy_label INTEGER, -- the company's own "FY" label for this period
    selector TEXT,           -- e.g. "max:SalesRevenueGoodsNet", "first:InventoryNet",
                             -- "sum:LongTermDebtNoncurrent+DebtCurrent", "derived:..."
    PRIMARY KEY (ticker, fiscal_year, field)
);

-- Raw companyfacts JSON, so a re-parse never needs to re-hit SEC.
CREATE TABLE IF NOT EXISTS raw_responses (
    ticker TEXT,
    cik TEXT,
    fetched_at TEXT,         -- ISO-8601 UTC
    json TEXT,               -- full companyfacts response body
    PRIMARY KEY (ticker, cik)
);

-- Independent FCF figures from yfinance, for cross-checking the DERIVED
-- free_cash_flow column (EDGAR reports no FCF element, so there is nothing in
-- the filing to validate against). yfinance only carries ~4 years of annual
-- cash-flow history, so this overlaps a minority of FY2014-2025.
CREATE TABLE IF NOT EXISTS market_fcf_crosscheck (
    ticker TEXT,
    fiscal_year INTEGER,
    period_end TEXT,
    yf_free_cash_flow REAL,
    yf_operating_cash_flow REAL,
    yf_capex REAL,          -- yfinance signs capex negative
    fetched_at TEXT,
    PRIMARY KEY (ticker, fiscal_year)
);

CREATE INDEX IF NOT EXISTS idx_prov_field ON fundamentals_provenance (field);
CREATE INDEX IF NOT EXISTS idx_prov_lag ON fundamentals_provenance (filing_lag_days);
