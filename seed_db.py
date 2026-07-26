"""
seed_db.py

Generates database.db: a fully synthetic, internally-consistent three-statement
dataset for 6 fake Technology companies over 6 fiscal years each.

Nothing here is real market data. Each company is defined by a small set of
high-level drivers (starting revenue, growth trajectory, margin trajectory,
leverage trajectory, working-capital days, tax rate, capex intensity...) and
every line item in `fundamentals` is *derived* from those drivers so the
statements tie out exactly:

  - net_income = revenue - cogs - sga - advertising - rnd
                 - other_operating_expenses - depreciation_amortization
                 - interest_expense - income_tax_expense
  - income_tax_expense = pretax_income * tax_rate   (tax_rate ~21-25%)
  - ebitda = revenue - cogs - sga - advertising - rnd - other_operating_expenses
             (== operating income + depreciation_amortization)
  - eps = net_income / shares_outstanding
  - accounts_receivable / inventory / accounts_payable are built from DSO/DIO/DPO
  - free_cash_flow = (net_income + D&A - delta_AR - delta_inventory + delta_AP) - capex
  - cash and total_debt roll forward year over year so the balance sheet stays
    plausible relative to revenue and prior-year balances

Growth/margin/leverage/share-count drivers are linearly interpolated between a
"start" and "end" value across the 6 fiscal years, so each company can tell a
clean story (expanding margins, deteriorating margins, rising leverage, etc.)
without any random noise.
"""

import sqlite3

DB_PATH = "database.db"

FISCAL_YEARS = [2019, 2020, 2021, 2022, 2023, 2024]

# Small "plug" balance-sheet items not worth promoting to per-company drivers.
OTHER_CURRENT_ASSETS_PCT = 0.02       # prepaid expenses, other current assets
OTHER_CURRENT_LIABILITIES_PCT = 0.05  # accrued comp, deferred revenue, etc.

# A company can never report negative cash. If organic free cash flow plus
# the target debt trajectory would drive cash below this floor, the company
# draws additional debt (like a revolver) to cover the shortfall -- which is
# exactly what distressed companies do in reality, and only makes a "high
# leverage" story more pronounced rather than producing an impossible balance
# sheet.
MIN_CASH_PCT = 0.02
MIN_CASH_FLOOR = 10_000_000

# ---------------------------------------------------------------------------
# Company driver configs — tweak these to change the story of each company.
# All dollar figures are in raw dollars (not millions). Percentages are
# fractions (0.20 == 20%). *_start/*_end values are linearly interpolated
# across the 6 fiscal years; scalar values are held constant.
# ---------------------------------------------------------------------------
COMPANY_CONFIGS = [
    {
        # Clearly STRONG: high growth decelerating gracefully, expanding
        # margins, low and falling leverage, healthy FCF.
        "ticker": "ZNTA",
        "revenue_start": 900_000_000,       # prior-year (unreported) revenue base
        "growth_start": 0.20, "growth_end": 0.13,
        "gross_margin_start": 0.68, "gross_margin_end": 0.74,
        "sga_pct_start": 0.24, "sga_pct_end": 0.19,
        "advertising_pct": 0.02,
        "rnd_pct": 0.14,
        "other_opex_pct": 0.015,
        "da_pct": 0.035,
        "capex_pct": 0.045,
        "tax_rate": 0.22,
        "dso": 42, "dio": 20, "dpo": 45,
        "debt_to_revenue_start": 0.15, "debt_to_revenue_end": 0.08,
        "interest_rate": 0.045,
        "equity_start": 1_200_000_000,
        "cash_start": 400_000_000,
        "shares_start": 220_000_000, "shares_end": 200_000_000,  # buybacks
        "price": 145.00, "beta": 1.05,
    },
    {
        # Clearly WEAK: flat-to-declining revenue, thin & compressing margins,
        # rising leverage, deteriorating into net losses.
        "ticker": "QBIX",
        "revenue_start": 700_000_000,
        "growth_start": 0.01, "growth_end": -0.03,
        "gross_margin_start": 0.34, "gross_margin_end": 0.29,
        "sga_pct_start": 0.22, "sga_pct_end": 0.25,
        "advertising_pct": 0.02,
        "rnd_pct": 0.06,
        "other_opex_pct": 0.02,
        "da_pct": 0.05,
        "capex_pct": 0.04,
        "tax_rate": 0.23,
        "dso": 65, "dio": 70, "dpo": 35,
        "debt_to_revenue_start": 0.55, "debt_to_revenue_end": 0.75,
        "interest_rate": 0.085,
        "equity_start": 300_000_000,
        "cash_start": 60_000_000,
        "shares_start": 150_000_000, "shares_end": 155_000_000,  # dilution
        "price": 8.00, "beta": 1.60,
    },
    {
        # Moderate-strong: steady double-digit growth, solid margins,
        # moderate and improving leverage.
        "ticker": "VRTK",
        "revenue_start": 600_000_000,
        "growth_start": 0.13, "growth_end": 0.10,
        "gross_margin_start": 0.60, "gross_margin_end": 0.63,
        "sga_pct_start": 0.22, "sga_pct_end": 0.20,
        "advertising_pct": 0.025,
        "rnd_pct": 0.12,
        "other_opex_pct": 0.02,
        "da_pct": 0.04,
        "capex_pct": 0.05,
        "tax_rate": 0.23,
        "dso": 48, "dio": 35, "dpo": 42,
        "debt_to_revenue_start": 0.25, "debt_to_revenue_end": 0.20,
        "interest_rate": 0.05,
        "equity_start": 700_000_000,
        "cash_start": 180_000_000,
        "shares_start": 130_000_000, "shares_end": 130_000_000,
        "price": 62.00, "beta": 1.10,
    },
    {
        # Middle of the pack: unremarkable, flat operating leverage,
        # steady (not improving) leverage.
        "ticker": "HLIO",
        "revenue_start": 450_000_000,
        "growth_start": 0.08, "growth_end": 0.07,
        "gross_margin_start": 0.52, "gross_margin_end": 0.53,
        "sga_pct_start": 0.23, "sga_pct_end": 0.23,
        "advertising_pct": 0.03,
        "rnd_pct": 0.10,
        "other_opex_pct": 0.02,
        "da_pct": 0.045,
        "capex_pct": 0.05,
        "tax_rate": 0.24,
        "dso": 52, "dio": 40, "dpo": 38,
        "debt_to_revenue_start": 0.35, "debt_to_revenue_end": 0.35,
        "interest_rate": 0.06,
        "equity_start": 400_000_000,
        "cash_start": 90_000_000,
        "shares_start": 110_000_000, "shares_end": 110_000_000,
        "price": 34.00, "beta": 1.20,
    },
    {
        # Moderate-weak: decelerating growth with real margin compression,
        # rising leverage funding the weakness — decent-to-poor trajectory.
        "ticker": "PXLM",
        "revenue_start": 380_000_000,
        "growth_start": 0.09, "growth_end": 0.02,
        "gross_margin_start": 0.55, "gross_margin_end": 0.45,
        "sga_pct_start": 0.22, "sga_pct_end": 0.27,
        "advertising_pct": 0.035,
        "rnd_pct": 0.09,
        "other_opex_pct": 0.025,
        "da_pct": 0.05,
        "capex_pct": 0.045,
        "tax_rate": 0.24,
        "dso": 58, "dio": 50, "dpo": 36,
        "debt_to_revenue_start": 0.30, "debt_to_revenue_end": 0.50,
        "interest_rate": 0.07,
        "equity_start": 320_000_000,
        "cash_start": 70_000_000,
        "shares_start": 95_000_000, "shares_end": 95_000_000,
        "price": 15.00, "beta": 1.40,
    },
    {
        # High-growth but leveraged "growth trap": very fast revenue growth
        # and improving unit economics, but heavy debt + capex keep it loss
        # making early on, turning profitable only by the final years.
        "ticker": "DYNM",
        "revenue_start": 250_000_000,
        "growth_start": 0.28, "growth_end": 0.20,
        "gross_margin_start": 0.62, "gross_margin_end": 0.66,
        "sga_pct_start": 0.30, "sga_pct_end": 0.24,
        "advertising_pct": 0.05,
        "rnd_pct": 0.16,
        "other_opex_pct": 0.02,
        "da_pct": 0.06,
        "capex_pct": 0.08,
        "tax_rate": 0.22,
        "dso": 40, "dio": 15, "dpo": 50,
        "debt_to_revenue_start": 0.60, "debt_to_revenue_end": 0.55,
        "interest_rate": 0.075,
        "equity_start": 180_000_000,
        "cash_start": 120_000_000,
        "shares_start": 160_000_000, "shares_end": 160_000_000,
        "price": 28.00, "beta": 1.50,
    },
]


def interp(start, end, i, n):
    """Linearly interpolate between start and end across n steps (index i)."""
    if n <= 1:
        return start
    return start + (end - start) * i / (n - 1)


def build_company(cfg):
    """Derive all 6 years of fundamentals rows + the market row for one company."""
    n = len(FISCAL_YEARS)

    # Antecedent (prior, unreported) year balances, used only to compute
    # year-1 deltas (working capital change, debt change) consistently.
    rev_prev = cfg["revenue_start"]
    cogs_prev = rev_prev * (1 - cfg["gross_margin_start"])
    ar_prev = rev_prev * cfg["dso"] / 365
    inv_prev = cogs_prev * cfg["dio"] / 365
    ap_prev = cogs_prev * cfg["dpo"] / 365
    debt_prev = rev_prev * cfg["debt_to_revenue_start"]

    equity = cfg["equity_start"]
    cash = cfg["cash_start"]
    shares = cfg["shares_start"]

    rows = []
    for i, year in enumerate(FISCAL_YEARS):
        growth = interp(cfg["growth_start"], cfg["growth_end"], i, n)
        gross_margin = interp(cfg["gross_margin_start"], cfg["gross_margin_end"], i, n)
        sga_pct = interp(cfg["sga_pct_start"], cfg["sga_pct_end"], i, n)
        debt_to_revenue = interp(cfg["debt_to_revenue_start"], cfg["debt_to_revenue_end"], i, n)
        shares = interp(cfg["shares_start"], cfg["shares_end"], i, n)

        revenue = rev_prev * (1 + growth)
        cogs = revenue * (1 - gross_margin)
        sga = revenue * sga_pct
        advertising = revenue * cfg["advertising_pct"]
        rnd = revenue * cfg["rnd_pct"]
        other_opex = revenue * cfg["other_opex_pct"]
        da = revenue * cfg["da_pct"]

        # EBITDA = operating income before D&A (ties to net income below).
        ebitda = revenue - cogs - sga - advertising - rnd - other_opex
        ebit = ebitda - da

        total_debt = revenue * debt_to_revenue
        interest_expense = total_debt * cfg["interest_rate"]

        pretax_income = ebit - interest_expense
        income_tax_expense = pretax_income * cfg["tax_rate"]
        net_income = pretax_income - income_tax_expense
        eps = net_income / shares

        accounts_receivable = revenue * cfg["dso"] / 365
        inventory = cogs * cfg["dio"] / 365
        accounts_payable = cogs * cfg["dpo"] / 365

        capex = revenue * cfg["capex_pct"]

        delta_ar = accounts_receivable - ar_prev
        delta_inv = inventory - inv_prev
        delta_ap = accounts_payable - ap_prev

        operating_cash_flow = net_income + da - delta_ar - delta_inv + delta_ap
        free_cash_flow = operating_cash_flow - capex

        # Roll forward balance sheet: retain all earnings, use FCF plus net
        # borrowing/repayment (against the target leverage trajectory) to
        # move cash. If that would push cash below the minimum floor, draw
        # extra debt (a revolver-style plug) to keep cash non-negative and
        # plausible.
        debt_change = total_debt - debt_prev
        cash_before_plug = cash + free_cash_flow + debt_change
        min_cash = max(revenue * MIN_CASH_PCT, MIN_CASH_FLOOR)
        if cash_before_plug < min_cash:
            debt_plug = min_cash - cash_before_plug
            total_debt += debt_plug
            cash = min_cash
        else:
            cash = cash_before_plug
        equity = equity + net_income

        other_ca = revenue * OTHER_CURRENT_ASSETS_PCT
        other_cl = revenue * OTHER_CURRENT_LIABILITIES_PCT
        current_assets = cash + accounts_receivable + inventory + other_ca
        current_liabilities = accounts_payable + other_cl

        rows.append((
            cfg["ticker"], year,
            round(revenue, 2), round(cogs, 2), round(sga, 2), round(advertising, 2), round(rnd, 2),
            round(other_opex, 2), round(da, 2),
            round(interest_expense, 2), round(income_tax_expense, 2), round(net_income, 2),
            round(ebitda, 2), round(eps, 4),
            round(accounts_receivable, 2), round(inventory, 2), round(accounts_payable, 2),
            round(equity, 2), round(total_debt, 2), round(current_assets, 2),
            round(current_liabilities, 2), round(cash, 2), round(shares, 0),
            round(capex, 2), round(free_cash_flow, 2),
        ))

        rev_prev = revenue
        ar_prev, inv_prev, ap_prev = accounts_receivable, inventory, accounts_payable
        debt_prev = total_debt

    market_row = (cfg["ticker"], cfg["price"], round(cfg["price"] * shares, 2), cfg["beta"], "Technology")
    return rows, market_row


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript("""
        DROP TABLE IF EXISTS fundamentals;
        DROP TABLE IF EXISTS market;

        CREATE TABLE fundamentals (
            ticker TEXT, fiscal_year INTEGER,
            revenue REAL, cogs REAL, sga REAL, advertising REAL, rnd REAL,
            other_operating_expenses REAL, depreciation_amortization REAL,
            interest_expense REAL, income_tax_expense REAL, net_income REAL,
            ebitda REAL, eps REAL,
            accounts_receivable REAL, inventory REAL, accounts_payable REAL,
            total_equity REAL, total_debt REAL, current_assets REAL,
            current_liabilities REAL, cash REAL, shares_outstanding REAL,
            capex REAL, free_cash_flow REAL,
            PRIMARY KEY (ticker, fiscal_year)
        );
        CREATE TABLE market (
            ticker TEXT PRIMARY KEY, price REAL, market_cap REAL, beta REAL, sector TEXT
        );
    """)

    fundamentals_insert = """
        INSERT INTO fundamentals (
            ticker, fiscal_year, revenue, cogs, sga, advertising, rnd,
            other_operating_expenses, depreciation_amortization,
            interest_expense, income_tax_expense, net_income,
            ebitda, eps,
            accounts_receivable, inventory, accounts_payable,
            total_equity, total_debt, current_assets,
            current_liabilities, cash, shares_outstanding,
            capex, free_cash_flow
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    market_insert = """
        INSERT INTO market (ticker, price, market_cap, beta, sector) VALUES (?,?,?,?,?)
    """

    for cfg in COMPANY_CONFIGS:
        rows, market_row = build_company(cfg)
        cur.executemany(fundamentals_insert, rows)
        cur.execute(market_insert, market_row)

    conn.commit()
    conn.close()
    print(f"Seeded {DB_PATH} with {len(COMPANY_CONFIGS)} companies x {len(FISCAL_YEARS)} years.")


if __name__ == "__main__":
    main()
