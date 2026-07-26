# Consumer-staples fundamentals: SEC EDGAR + yfinance → SQLite

34 consumer-staples tickers, fiscal years 2014–2025. Fundamentals come from
SEC EDGAR's XBRL `companyfacts` API; price/market data from yfinance.

## Run order

```bash
python3 -m venv .venv && .venv/bin/pip install requests pandas yfinance

.venv/bin/python step0_resolve_ciks.py     # ticker -> CIK map (cik_map.json)
.venv/bin/python -m unittest test_extract  # 50 tests, no network
.venv/bin/python build_db.py [earliest|latest]   # Step 4: 34-ticker loop
.venv/bin/python market_data.py            # Step 5: market table + FCF cross-check
```

Everything is `INSERT OR REPLACE`, so re-running is idempotent. Raw
`companyfacts` JSON is cached under `raw_samples/` (reused if <24 h old) and
also stored in the `raw_responses` table, so a re-parse never re-hits SEC.

SEC requires a descriptive `User-Agent` on every request or returns 403, and
caps clients at 10 req/s; both are handled in `edgar_fetch.py`.

## Tables

| table | contents |
|---|---|
| `fundamentals` | 23 fields × 34 tickers × 12 years (408 rows), raw dollars |
| `market` | price, market_cap, beta, sector (delayed quotes) |
| `fundamentals_provenance` | per (ticker, year, field): winning tag, filing date, form, filing lag, source CIK, native FY label, selector |
| `raw_responses` | full companyfacts JSON per (ticker, CIK) |
| `market_fcf_crosscheck` | yfinance FCF/CFO/capex for validating the derived `free_cash_flow` |

`fundamentals` and `market` match the agreed column lists verbatim.

## Key conventions

**`fiscal_year` is the economic year**, derived from the period end date:

```
fiscal_year = end.year if end.month >= 6 else end.year - 1
```

This matches SEC's own `frame` convention and makes the key comparable across
all 34 fiscal calendars (19 of the 34 have non-December year-ends). It is *not*
always the company's own "FY" label — for **WMT, TGT, STZ, BF.B, CASY, SJM,
GIS** the key is one lower. The native label is in
`fundamentals_provenance.native_fy_label`.

The `fy` field in companyfacts is the fiscal year of the *filing*, not the
fact, and is never used — keying on it silently drops a year for Kroger.

**Values are as-originally-reported** (earliest 10-K whose period matches),
selected chain-wide across the tag fallback list so an early value under a
deprecated tag beats a later comparative under a newer tag. Run
`build_db.py latest` for as-restated and diff the two. About 9% of
fiscal-year/field cells differ between the two.

**Derived columns**: `ebitda` = OperatingIncomeLoss + D&A;
`total_debt` = long-term debt + current debt using widest-available aggregate
first (EDGAR's debt tags are nested, not sibling — summing them double-counts);
`free_cash_flow` = CFO − capex. EDGAR publishes no FCF element, so
`free_cash_flow` cannot be validated against the filing and is cross-checked
against yfinance instead.

## Known limitations

- **Per-share continuity across stock splits is deferred scope.** Under the
  as-originally-reported policy, `eps` and `shares_outstanding` are *not*
  split-adjusted, so per-share series break at split years. The loader flags
  every affected year (`shares_outstanding` vs `net_income/eps` beyond 25%).
  Raw fundamentals — revenue, margins, cash flow, balance sheet — are
  split-agnostic and unaffected. Add a split-adjustment step before computing
  per-share time series.
- `companyfacts` contains only `us-gaap`/`dei`/`srt` elements. Company
  extension tags are stripped, which is why `sga` is NULL for several retailers
  (Kroger et al. tag their opex line with a custom element). Never plug-derived.
- `other_operating_expenses` resolves for only 10 of 34 tickers; treat as
  low-confidence.
- `interest_expense` is NET of interest income for 11 tickers in later years
  (filers migrating to `InterestIncomeExpenseNonoperatingNet`). Sign-normalised
  to expense-positive and flagged in coverage.
- `cogs` changes definition at the ASC 606 boundary for 25 of 34 tickers; for
  Kroger and Casey's the new tag *excludes* D&A, so those COGS series have a
  real discontinuity no tag mapping can fix.
- KHC FY2014–15, KDP FY2014–18 and KVUE pre-2021 are nulled: those are
  predecessor-entity figures (Heinz standalone / Dr Pepper Snapple standalone)
  that would fabricate merger-driven growth spikes.
- BG spans two CIKs (Bunge Limited `0001144519` → Bunge Global SA
  `0001996862`); rows are merged, successor preferred on overlap, source
  recorded in provenance.
- yfinance carries only ~4–5 years of annual cash-flow history, so the FCF
  cross-check covers a minority of the 12-year window.
