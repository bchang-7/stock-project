"""
valuation.py

Reads database.db (same schema as score.py) and computes a DCF intrinsic
value per share for each company via a bottom-up three-statement forecast.

We do NOT forecast free cash flow directly. Instead each income-statement,
balance-sheet, and working-capital line item is forecast independently by
the method appropriate to it (see CONFIG / METHODS below), the forecasted
statements are assembled, and FCFF is *derived* from them year by year.
All companies are treated as one sector (Technology, per market.sector).

Pipeline, in order:
  1. Load ~6 years of history per company from `fundamentals` + `market`.
  2. Forecast each line item FORECAST_YEARS ahead:
       - ETS (Holt / SES, statsmodels)   -> revenue, cogs, sga, advertising,
         rnd, other_operating_expenses, interest_expense, capex, total_debt
       - OLS linear trend on fiscal_year -> depreciation_amortization,
         shares_outstanding
       - Working-capital "activity day" models tied to forecasted drivers:
         AR via DSO, inventory via DIO, AP via DPO (day-metric = recent
         historical average).
       - Effective tax rate = historical average (last TAX_AVG_YEARS years)
         of income_tax_expense / pretax_income, applied to forecasted EBIT.
  3. Derive FCFF for each forecast year from the assembled statements:
       EBIT   = revenue - cogs - sga - advertising - rnd - other_opex - D&A
       NOPAT  = EBIT * (1 - effective_tax_rate)
       dNWC   = change in (AR + inventory - AP), year over year
       FCFF   = NOPAT + D&A - capex - dNWC
  4. WACC via CAPM (WACC ~= cost of equity, prototype simplification -- see
     note at compute_wacc). Discount the 5 FCFFs + Gordon-growth terminal
     value back to present.
  5. Equity value = EV - latest total_debt + latest cash. Intrinsic value
     per share = equity value / latest shares_outstanding. Upside vs.
     market price.
  6. Validation: derive FCFF for the latest *historical* year the same way
     and compare it to the `free_cash_flow` column reported in the DB.

Note on the validation gap: the reported `free_cash_flow` in the DB is a
levered figure (built from net_income, which is after interest expense and
actual cash taxes), while our derived FCFF is unlevered (built from EBIT,
before interest). The exact relationship is:
    derived_FCFF - reported_FCF == interest_expense * (1 - tax_rate)
i.e. the after-tax interest expense -- the standard FCFF-vs-FCFE tax-shield
gap. For low-leverage companies this is small (a good sign the derivation
logic is correct); for highly levered companies it will be more visible.
"""

import sqlite3
import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing, SimpleExpSmoothing

warnings.filterwarnings("ignore")

DB_PATH = "database.db"

# ---------------------------------------------------------------------------
# Config: every assumption lives here so the model can be re-scoped without
# touching the computation logic.
# ---------------------------------------------------------------------------
CONFIG = {
    "FORECAST_YEARS": 5,
    "RF": 0.043,             # risk-free rate
    "ERP": 0.05,             # equity risk premium
    "G_TERMINAL": 0.025,     # Gordon-growth terminal growth rate
    "TAX_AVG_YEARS": 4,      # years averaged for effective tax rate
    "WC_AVG_YEARS": 4,       # years averaged for DSO/DIO/DPO
    "MIN_HISTORY_FOR_TREND": 3,  # below this, fall back to naive/flat forecasts
}

# Line items forecast via ETS (exponential smoothing, trend-capable, annual)
ETS_ITEMS = [
    "revenue", "cogs", "sga", "advertising", "rnd",
    "other_operating_expenses", "interest_expense", "capex", "total_debt",
]

# Line items forecast via OLS linear trend on fiscal_year
LINEAR_ITEMS = ["depreciation_amortization", "shares_outstanding"]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_data(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    fundamentals = pd.read_sql_query("SELECT * FROM fundamentals", conn)
    market = pd.read_sql_query("SELECT * FROM market", conn)
    conn.close()
    return fundamentals, market


# ---------------------------------------------------------------------------
# Forecasting primitives
# ---------------------------------------------------------------------------
def forecast_ets(series, periods, damped=False):
    """
    Forecast `periods` steps ahead with Holt's exponential smoothing
    (trend-capable, non-seasonal, annual data). Falls back to simple
    exponential smoothing (no trend) if a trend fit isn't stable or there
    aren't enough points, and to a flat/naive carry-forward if there's
    really nothing to fit on.
    """
    series = pd.Series(series).astype(float).reset_index(drop=True)
    n = len(series)

    if n == 0:
        return np.zeros(periods)
    if n == 1 or series.nunique() == 1:
        # Degenerate: nothing to learn a trend from -- carry the last value.
        return np.full(periods, series.iloc[-1])

    if n < CONFIG["MIN_HISTORY_FOR_TREND"]:
        try:
            fit = SimpleExpSmoothing(series, initialization_method="estimated").fit()
            return fit.forecast(periods).to_numpy()
        except Exception:
            return np.full(periods, series.iloc[-1])

    try:
        model = ExponentialSmoothing(
            series, trend="add", damped_trend=damped, seasonal=None,
            initialization_method="estimated",
        )
        fit = model.fit()
        fc = fit.forecast(periods).to_numpy()
        if np.any(~np.isfinite(fc)):
            raise ValueError("non-finite forecast")
        return fc
    except Exception:
        try:
            fit = SimpleExpSmoothing(series, initialization_method="estimated").fit()
            return fit.forecast(periods).to_numpy()
        except Exception:
            return np.full(periods, series.iloc[-1])


def forecast_linear(fiscal_years, values, periods):
    """OLS linear trend of `values` on `fiscal_years`, extrapolated forward."""
    x = np.asarray(fiscal_years, dtype=float)
    y = np.asarray(values, dtype=float)
    n = len(y)

    if n == 0:
        return np.zeros(periods)
    if n == 1 or np.unique(x).size == 1:
        return np.full(periods, y[-1])

    slope, intercept = np.polyfit(x, y, 1)
    future_x = np.arange(x[-1] + 1, x[-1] + 1 + periods)
    return slope * future_x + intercept


# ---------------------------------------------------------------------------
# Per-company forecast assembly
# ---------------------------------------------------------------------------
def build_company_forecast(hist, cfg=CONFIG):
    """
    hist: fundamentals rows for one company, sorted by fiscal_year.
    Returns a dict with the forecasted line-item DataFrame (index 1..N years
    ahead), the historical DataFrame, the effective tax rate used, and the
    day-metrics used for working capital.
    """
    hist = hist.sort_values("fiscal_year").reset_index(drop=True)
    years = hist["fiscal_year"].to_numpy()
    n_years = cfg["FORECAST_YEARS"]

    forecast = pd.DataFrame(index=range(1, n_years + 1))
    forecast["fiscal_year"] = years[-1] + np.arange(1, n_years + 1)

    for item in ETS_ITEMS:
        forecast[item] = forecast_ets(hist[item], n_years)

    for item in LINEAR_ITEMS:
        forecast[item] = forecast_linear(years, hist[item], n_years)

    # --- effective tax rate: historical average over the last TAX_AVG_YEARS ---
    hist_pretax = (
        hist["revenue"] - hist["cogs"] - hist["sga"] - hist["advertising"]
        - hist["rnd"] - hist["other_operating_expenses"]
        - hist["depreciation_amortization"] - hist["interest_expense"]
    )
    hist_tax_rate = hist["income_tax_expense"] / hist_pretax
    hist_tax_rate = hist_tax_rate.replace([np.inf, -np.inf], np.nan)
    window = hist_tax_rate.tail(cfg["TAX_AVG_YEARS"]).dropna()
    tax_rate = window.mean() if len(window) else hist_tax_rate.dropna().mean()
    if not np.isfinite(tax_rate):
        tax_rate = 0.21  # last-resort fallback, shouldn't trigger on this dataset

    # --- working capital: DSO / DIO / DPO, recent-average day-metric ---
    dso_hist = hist["accounts_receivable"] / hist["revenue"] * 365
    dio_hist = hist["inventory"] / hist["cogs"] * 365
    dpo_hist = hist["accounts_payable"] / hist["cogs"] * 365

    dso = dso_hist.tail(cfg["WC_AVG_YEARS"]).mean()
    dio = dio_hist.tail(cfg["WC_AVG_YEARS"]).mean()
    dpo = dpo_hist.tail(cfg["WC_AVG_YEARS"]).mean()

    forecast["accounts_receivable"] = dso / 365 * forecast["revenue"]
    forecast["inventory"] = dio / 365 * forecast["cogs"]
    forecast["accounts_payable"] = dpo / 365 * forecast["cogs"]

    return {
        "hist": hist,
        "forecast": forecast,
        "tax_rate": tax_rate,
        "dso": dso, "dio": dio, "dpo": dpo,
    }


def derive_fcff(df, tax_rate, prior_ar, prior_inv, prior_ap):
    """
    Derive FCFF for each row of `df` (a forecast or a single historical
    year, as long as it has the needed columns). `prior_*` are the
    AR/inventory/AP balances from the year immediately before df's first
    row, used to compute the first period's delta-NWC.
    """
    ebit = (
        df["revenue"] - df["cogs"] - df["sga"] - df["advertising"] - df["rnd"]
        - df["other_operating_expenses"] - df["depreciation_amortization"]
    )
    nopat = ebit * (1 - tax_rate)

    nwc = df["accounts_receivable"] + df["inventory"] - df["accounts_payable"]
    prior_nwc = prior_ar + prior_inv - prior_ap
    d_nwc = nwc - pd.concat([pd.Series([prior_nwc]), nwc.iloc[:-1]], ignore_index=True).to_numpy()

    fcff = nopat + df["depreciation_amortization"] - df["capex"] - d_nwc
    return fcff, ebit, nopat, d_nwc


# ---------------------------------------------------------------------------
# WACC (CAPM cost of equity)
# ---------------------------------------------------------------------------
def compute_wacc(beta, cfg=CONFIG):
    # Prototype simplification: WACC ~= cost of equity (CAPM), i.e. we treat
    # the whole capital structure as equity-financed for discounting
    # purposes. A proper debt-weighted WACC (blending after-tax cost of
    # debt by D/(D+E)) is the natural next refinement but is optional here.
    return cfg["RF"] + beta * cfg["ERP"]


# ---------------------------------------------------------------------------
# Reliability flag: negative/erratic historical fundamentals make the DCF
# untrustworthy even though a number can still be computed.
# ---------------------------------------------------------------------------
def assess_reliability(hist):
    reasons = []
    net_income = hist["net_income"].to_numpy()
    fcf = hist["free_cash_flow"].to_numpy()

    if (net_income[-2:] < 0).any():
        reasons.append("negative net income in recent history")
    if np.mean(fcf) < 0 or (fcf < 0).sum() >= len(fcf) / 2:
        reasons.append("negative/erratic historical free cash flow")

    return reasons


# ---------------------------------------------------------------------------
# Per-company valuation
# ---------------------------------------------------------------------------
def value_company(ticker, fundamentals, market, cfg=CONFIG):
    hist_all = fundamentals.loc[fundamentals["ticker"] == ticker]
    built = build_company_forecast(hist_all, cfg)
    hist, forecast, tax_rate = built["hist"], built["forecast"], built["tax_rate"]

    latest = hist.iloc[-1]
    beta = market.loc[market["ticker"] == ticker, "beta"].iloc[0]
    price = market.loc[market["ticker"] == ticker, "price"].iloc[0]
    wacc = compute_wacc(beta, cfg)

    fcff, ebit, nopat, d_nwc = derive_fcff(
        forecast, tax_rate,
        latest["accounts_receivable"], latest["inventory"], latest["accounts_payable"],
    )
    forecast = forecast.copy()
    forecast["ebit"] = ebit
    forecast["nopat"] = nopat
    forecast["d_nwc"] = d_nwc
    forecast["fcff"] = fcff

    # --- discounting ---
    n = cfg["FORECAST_YEARS"]
    discount_factors = np.array([(1 + wacc) ** -t for t in range(1, n + 1)])
    pv_fcff = fcff.to_numpy() * discount_factors
    ev_from_fcff = pv_fcff.sum()

    g = cfg["G_TERMINAL"]
    tv_ok = wacc > g
    if tv_ok:
        tv = fcff.iloc[-1] * (1 + g) / (wacc - g)
        pv_tv = tv * discount_factors[-1]
    else:
        tv, pv_tv = np.nan, 0.0

    ev = ev_from_fcff + pv_tv
    equity_value = ev - latest["total_debt"] + latest["cash"]
    shares = latest["shares_outstanding"]
    intrinsic_value = equity_value / shares if shares else np.nan
    upside = (intrinsic_value - price) / price if price else np.nan

    reasons = assess_reliability(hist)
    if not tv_ok:
        reasons.append("WACC <= terminal growth: terminal value skipped")
    flag = "DCF unreliable - negative fundamentals" if any(
        "negative" in r for r in reasons
    ) else ("; ".join(reasons) if reasons else "")

    # --- validation: derive FCFF for the latest historical year ---
    prior = hist.iloc[-2] if len(hist) >= 2 else hist.iloc[-1]
    hist_latest_row = hist.iloc[[-1]]
    fcff_hist, _, _, _ = derive_fcff(
        hist_latest_row, tax_rate,
        prior["accounts_receivable"], prior["inventory"], prior["accounts_payable"],
    )
    derived_latest_fcff = fcff_hist.iloc[0]
    reported_fcf = latest["free_cash_flow"]
    pct_diff = (
        (derived_latest_fcff - reported_fcf) / abs(reported_fcf)
        if reported_fcf not in (0, 0.0) else np.nan
    )

    return {
        "ticker": ticker,
        "intrinsic_value_per_share": intrinsic_value,
        "current_price": price,
        "upside_pct": upside * 100 if np.isfinite(upside) else np.nan,
        "wacc": wacc,
        "flag": flag,
        "ev": ev,
        "equity_value": equity_value,
        "tv": tv,
        "forecast": forecast,
        "derived_latest_fcff": derived_latest_fcff,
        "reported_fcf": reported_fcf,
        "fcf_pct_diff": pct_diff * 100 if np.isfinite(pct_diff) else np.nan,
    }


# ---------------------------------------------------------------------------
# Orchestration + output tables
# ---------------------------------------------------------------------------
_CACHE = {}


def compute_all(db_path=DB_PATH, force=False):
    if _CACHE and not force:
        return _CACHE

    fundamentals, market = load_data(db_path)
    results = {
        ticker: value_company(ticker, fundamentals, market)
        for ticker in fundamentals["ticker"].unique()
    }

    valuation_table = pd.DataFrame([
        {
            "ticker": r["ticker"],
            "intrinsic_value_per_share": r["intrinsic_value_per_share"],
            "current_price": r["current_price"],
            "upside_pct": r["upside_pct"],
            "wacc": r["wacc"],
            "flag": r["flag"],
        }
        for r in results.values()
    ]).sort_values("upside_pct", ascending=False).reset_index(drop=True)

    validation_table = pd.DataFrame([
        {
            "ticker": r["ticker"],
            "derived_latest_fcff": r["derived_latest_fcff"],
            "reported_fcf": r["reported_fcf"],
            "pct_diff": r["fcf_pct_diff"],
        }
        for r in results.values()
    ]).sort_values("ticker").reset_index(drop=True)

    _CACHE.update({
        "fundamentals": fundamentals,
        "market": market,
        "results": results,
        "valuation_table": valuation_table,
        "validation_table": validation_table,
    })
    return _CACHE


def show_forecast(ticker, db_path=DB_PATH):
    cache = compute_all(db_path)
    results = cache["results"]
    if ticker not in results:
        print(f"Unknown ticker: {ticker}")
        return

    r = results[ticker]
    fc = r["forecast"]

    print(f"=== {ticker}: 5-year forecast ===")
    cols = [
        "fiscal_year", "revenue", "cogs", "sga", "advertising", "rnd",
        "other_operating_expenses", "depreciation_amortization",
        "interest_expense", "capex", "total_debt",
        "accounts_receivable", "inventory", "accounts_payable",
    ]
    print(fc[cols].round(1).to_string(index=False))
    print()
    print("Derived FCFF path:")
    print(fc[["fiscal_year", "ebit", "nopat", "d_nwc", "fcff"]].round(1).to_string(index=False))
    print()
    tax_rate = build_company_forecast(
        cache["fundamentals"].loc[cache["fundamentals"]["ticker"] == ticker]
    )["tax_rate"]
    print(f"Effective tax rate used: {tax_rate:.2%}")
    print(f"WACC used: {r['wacc']:.2%}")
    print(f"Terminal value: {r['tv']:,.0f}" if np.isfinite(r["tv"]) else "Terminal value: skipped (WACC <= g)")
    print(f"Enterprise value: {r['ev']:,.0f}")
    print(f"Equity value: {r['equity_value']:,.0f}")
    print(f"Intrinsic value / share: {r['intrinsic_value_per_share']:.2f}  "
          f"(price: {r['current_price']:.2f}, upside: {r['upside_pct']:.1f}%)")
    print()


def main():
    cache = compute_all()
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    print("=== VALUATION TABLE (sorted by upside, descending) ===")
    vt = cache["valuation_table"].copy()
    vt["intrinsic_value_per_share"] = vt["intrinsic_value_per_share"].round(2)
    vt["current_price"] = vt["current_price"].round(2)
    vt["upside_pct"] = vt["upside_pct"].round(1)
    vt["wacc"] = (vt["wacc"] * 100).round(2)
    vt = vt.rename(columns={"wacc": "wacc_pct"})
    print(vt.to_string(index=False))
    print()

    print("=== FCF VALIDATION (bottom-up FCFF vs. reported free_cash_flow, latest year) ===")
    val = cache["validation_table"].copy()
    val["derived_latest_fcff"] = val["derived_latest_fcff"].round(0)
    val["reported_fcf"] = val["reported_fcf"].round(0)
    val["pct_diff"] = val["pct_diff"].round(1)
    print(val.to_string(index=False))
    print()

    print("=== show_forecast(\"ZNTA\") ===")
    show_forecast("ZNTA")


if __name__ == "__main__":
    main()
