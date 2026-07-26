"""
score.py

Reads database.db (fundamentals + market tables) and produces a composite
quality/valuation score for every company in the set, comparing them directly
against each other (one sector, no sector grouping).

Four pillars, two raw metrics each:
  growth:        revenue CAGR, EPS growth
  profitability: ROE, net margin
  health:        debt-to-equity (lower better), current ratio
  valuation:     P/E (lower better), EV/EBITDA (lower better)

Pipeline, in order:
  1. Compute the 8 raw metrics per company from the first/latest fiscal-year
     rows in `fundamentals` plus `market`.
  2. Apply negative-value guards: negative equity floors ROE and D/E to
     worst-in-set; negative earnings floors P/E; negative EBITDA floors
     EV/EBITDA. A turnaround company (negative first-year EPS) falls back to
     a simple percent-change formula for EPS growth instead of the power
     formula (which breaks on negative bases).
  3. Winsorize each metric across companies at the 1st/99th percentile.
  4. Z-score each metric across companies.
  5. Invert sign on lower-is-better metrics so higher z always means better.
  6. Average the two z-scores within each pillar -> 4 pillar z-scores.
  7. Min-max rescale each pillar z-score to 0-100 across companies.
  8. Composite = weighted average of the four 0-100 pillar scores.
  Any metric still missing/NaN after all of the above is imputed with the
  cross-company median and the company is flagged data-incomplete.
"""

import sqlite3
import numpy as np
import pandas as pd

DB_PATH = "database.db"

# ---------------------------------------------------------------------------
# Config: pillar weights + metric -> pillar/direction mapping. Edit these to
# re-weight or re-scope the model without touching the computation logic.
# ---------------------------------------------------------------------------
PILLAR_WEIGHTS = {
    "growth": 0.25,
    "profitability": 0.25,
    "health": 0.25,
    "valuation": 0.25,
}

# direction: "higher" = higher raw value is better, "lower" = lower is better
METRICS = {
    "revenue_cagr":   {"pillar": "growth",        "direction": "higher"},
    "eps_growth":     {"pillar": "growth",        "direction": "higher"},
    "roe":            {"pillar": "profitability",  "direction": "higher"},
    "net_margin":     {"pillar": "profitability",  "direction": "higher"},
    "debt_to_equity": {"pillar": "health",         "direction": "lower"},
    "current_ratio":  {"pillar": "health",         "direction": "higher"},
    "pe_ratio":       {"pillar": "valuation",      "direction": "lower"},
    "ev_ebitda":      {"pillar": "valuation",      "direction": "lower"},
}

PILLAR_ORDER = ["growth", "profitability", "health", "valuation"]
METRIC_ORDER = list(METRICS.keys())

WINSOR_LOWER_PCT = 1
WINSOR_UPPER_PCT = 99


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_data(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    fundamentals = pd.read_sql_query("SELECT * FROM fundamentals", conn)
    market = pd.read_sql_query("SELECT * FROM market", conn)
    conn.close()
    return fundamentals, market


def safe_div(a, b):
    return a / b if b not in (0, 0.0) else np.nan


# ---------------------------------------------------------------------------
# Step 1: raw metrics
# ---------------------------------------------------------------------------
def compute_raw_metrics(fundamentals, market):
    records = []
    flags = {}

    for ticker, g in fundamentals.groupby("ticker"):
        g = g.sort_values("fiscal_year")
        first, latest = g.iloc[0], g.iloc[-1]
        years = latest["fiscal_year"] - first["fiscal_year"]
        mkt = market.loc[market["ticker"] == ticker].iloc[0]
        company_flags = []

        # --- growth ---
        if years > 0 and first["revenue"] > 0 and latest["revenue"] > 0:
            revenue_cagr = (latest["revenue"] / first["revenue"]) ** (1 / years) - 1
        else:
            revenue_cagr = np.nan

        eps_first, eps_latest = first["eps"], latest["eps"]
        if years > 0 and eps_first > 0 and eps_latest > 0:
            eps_growth = (eps_latest / eps_first) ** (1 / years) - 1
        elif eps_first != 0:
            # turnaround / negative-base case: power formula breaks on a
            # negative or zero-crossing base, so fall back to simple % change.
            eps_growth = (eps_latest - eps_first) / abs(eps_first)
            company_flags.append("eps turnaround: growth via simple % change")
        else:
            eps_growth = np.nan

        # --- profitability / health (equity-dependent) ---
        equity_latest = latest["total_equity"]
        roe = safe_div(latest["net_income"], equity_latest)
        net_margin = safe_div(latest["net_income"], latest["revenue"])
        debt_to_equity = safe_div(latest["total_debt"], equity_latest)
        current_ratio = safe_div(latest["current_assets"], latest["current_liabilities"])

        # --- valuation ---
        eps_for_pe = eps_latest
        pe_ratio = safe_div(mkt["price"], eps_for_pe)
        ev = mkt["market_cap"] + latest["total_debt"] - latest["cash"]
        ev_ebitda = safe_div(ev, latest["ebitda"])

        neg_equity = equity_latest < 0
        neg_earnings = eps_latest <= 0
        neg_ebitda = latest["ebitda"] <= 0

        if neg_equity:
            company_flags.append("neg equity: ROE/D&E floored")
        if neg_earnings:
            company_flags.append("neg earnings: P/E floored")
        if neg_ebitda:
            company_flags.append("neg EBITDA: EV/EBITDA floored")

        flags[ticker] = company_flags
        records.append({
            "ticker": ticker,
            "revenue_cagr": revenue_cagr,
            "eps_growth": eps_growth,
            "roe": roe,
            "net_margin": net_margin,
            "debt_to_equity": debt_to_equity,
            "current_ratio": current_ratio,
            "pe_ratio": pe_ratio,
            "ev_ebitda": ev_ebitda,
            "ev": ev,
            "ebitda": latest["ebitda"],
            "neg_equity": neg_equity,
            "neg_earnings": neg_earnings,
            "neg_ebitda": neg_ebitda,
        })

    df = pd.DataFrame(records).set_index("ticker")
    return df, flags


# ---------------------------------------------------------------------------
# Step 2: negative-value guards -- floor to worst-in-set (strictly worse than
# every legitimate value, so a guarded company can never tie for last place).
# ---------------------------------------------------------------------------
def _floor_worst(df, metric, bad_mask, direction):
    valid = df.loc[~bad_mask, metric].dropna()
    if not bad_mask.any() or valid.empty:
        return
    spread = (valid.max() - valid.min()) if len(valid) > 1 else (abs(valid.iloc[0]) + 1)
    margin = spread * 0.5 + 1e-6
    if direction == "higher":
        floor_value = valid.min() - margin
    else:
        floor_value = valid.max() + margin
    df.loc[bad_mask, metric] = floor_value


def apply_guards(df):
    df = df.copy()
    _floor_worst(df, "roe", df["neg_equity"], "higher")
    _floor_worst(df, "debt_to_equity", df["neg_equity"], "lower")
    _floor_worst(df, "pe_ratio", df["neg_earnings"], "lower")
    _floor_worst(df, "ev_ebitda", df["neg_ebitda"], "lower")
    return df


# ---------------------------------------------------------------------------
# Missing-value safety net: anything still NaN gets the cross-company median.
# ---------------------------------------------------------------------------
def impute_missing(df, flags):
    df = df.copy()
    incomplete = pd.Series(False, index=df.index)
    for metric in METRIC_ORDER:
        missing_mask = df[metric].isna()
        if missing_mask.any():
            median = df[metric].median()
            df.loc[missing_mask, metric] = median
            incomplete |= missing_mask
            for t in df.index[missing_mask]:
                flags[t].append(f"missing {metric}: imputed cross-company median")
    df["data_complete"] = ~incomplete
    return df


# ---------------------------------------------------------------------------
# Steps 3-8: winsorize -> z-score -> sign-invert -> pillar avg -> 0-100 rescale
# ---------------------------------------------------------------------------
def winsorize(s):
    lo, hi = np.percentile(s, [WINSOR_LOWER_PCT, WINSOR_UPPER_PCT])
    return s.clip(lo, hi)


def zscore(s):
    std = s.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / std


def minmax_100(s):
    lo, hi = s.min(), s.max()
    if hi == lo:
        return pd.Series(50.0, index=s.index)
    return (s - lo) / (hi - lo) * 100


def compute_scores(raw_df):
    z_df = pd.DataFrame(index=raw_df.index)
    for metric, cfg in METRICS.items():
        z = zscore(winsorize(raw_df[metric]))
        if cfg["direction"] == "lower":
            z = -z
        z_df[metric] = z

    pillar_z = pd.DataFrame(index=raw_df.index)
    for pillar in PILLAR_ORDER:
        cols = [m for m, c in METRICS.items() if c["pillar"] == pillar]
        pillar_z[pillar] = z_df[cols].mean(axis=1)

    pillar_scores = pd.DataFrame(index=raw_df.index)
    for pillar in PILLAR_ORDER:
        pillar_scores[pillar] = minmax_100(pillar_z[pillar])

    weight_sum = sum(PILLAR_WEIGHTS.values())
    composite = sum(pillar_scores[p] * PILLAR_WEIGHTS[p] for p in PILLAR_ORDER) / weight_sum
    pillar_scores["composite"] = composite
    return z_df, pillar_scores


# ---------------------------------------------------------------------------
# Output tables
# ---------------------------------------------------------------------------
def build_scoreboard(pillar_scores, raw_df):
    df = pillar_scores.copy()
    df["data_complete"] = raw_df["data_complete"]
    df = df.sort_values("composite", ascending=False)
    df = df[["composite"] + PILLAR_ORDER + ["data_complete"]]
    return df.reset_index()


def build_detail_table(raw_df, flags):
    display = pd.DataFrame(index=raw_df.index)
    for metric in METRIC_ORDER:
        display[metric] = raw_df[metric].round(2)
    display["ev_musd"] = (raw_df["ev"] / 1e6).round(2)
    display["ebitda_musd"] = (raw_df["ebitda"] / 1e6).round(2)
    display["flags"] = [
        "; ".join(flags[t]) if flags[t] else "" for t in raw_df.index
    ]
    return display.reset_index()


def build_ranks_table(raw_df, pillar_scores):
    n = len(raw_df)
    ranks = pd.DataFrame(index=raw_df.index)
    for metric, cfg in METRICS.items():
        ascending = cfg["direction"] == "lower"
        r = raw_df[metric].rank(ascending=ascending, method="min").astype(int)
        ranks[metric] = r.apply(lambda x: f"{x}/{n}")
    for pillar in PILLAR_ORDER + ["composite"]:
        r = pillar_scores[pillar].rank(ascending=False, method="min").astype(int)
        ranks[pillar] = r.apply(lambda x: f"{x}/{n}")
    return ranks.reset_index()


# ---------------------------------------------------------------------------
# Orchestration + cache (so explain() can reuse an already-computed run)
# ---------------------------------------------------------------------------
_CACHE = {}


def compute_all(db_path=DB_PATH, force=False):
    if _CACHE and not force:
        return _CACHE

    fundamentals, market = load_data(db_path)
    raw_df, flags = compute_raw_metrics(fundamentals, market)
    raw_df = apply_guards(raw_df)
    raw_df = impute_missing(raw_df, flags)
    z_df, pillar_scores = compute_scores(raw_df)

    scoreboard = build_scoreboard(pillar_scores, raw_df)
    detail = build_detail_table(raw_df, flags)
    ranks = build_ranks_table(raw_df, pillar_scores)

    _CACHE.update({
        "raw_df": raw_df,
        "flags": flags,
        "z_df": z_df,
        "pillar_scores": pillar_scores,
        "scoreboard": scoreboard,
        "detail": detail,
        "ranks": ranks,
        "n": len(raw_df),
    })
    return _CACHE


def explain(ticker, db_path=DB_PATH):
    cache = compute_all(db_path)
    raw_df = cache["raw_df"]
    pillar_scores = cache["pillar_scores"]
    ranks = cache["ranks"].set_index("ticker")
    flags = cache["flags"]

    if ticker not in raw_df.index:
        print(f"Unknown ticker: {ticker}")
        return

    print(f"=== {ticker} ===")
    print("Raw metrics (value, cross-company rank):")
    for metric in METRIC_ORDER:
        val = raw_df.at[ticker, metric]
        rank = ranks.at[ticker, metric]
        print(f"  {metric:16s} {val:>12.4f}   rank {rank}")
    print(f"  {'ev_musd':16s} {raw_df.at[ticker, 'ev'] / 1e6:>12.2f}")
    print(f"  {'ebitda_musd':16s} {raw_df.at[ticker, 'ebitda'] / 1e6:>12.2f}")
    print(f"  flags: {'; '.join(flags[ticker]) if flags[ticker] else 'none'}")

    print("\nPillar scores (0-100, cross-company rank):")
    for pillar in PILLAR_ORDER:
        score = pillar_scores.at[ticker, pillar]
        rank = ranks.at[ticker, pillar]
        print(f"  {pillar:16s} {score:>12.2f}   rank {rank}")

    composite = pillar_scores.at[ticker, "composite"]
    rank = ranks.at[ticker, "composite"]
    print(f"\nComposite: {composite:.2f}   rank {rank}")
    print()


def main():
    cache = compute_all()
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    print("=== SCOREBOARD (sorted by composite, descending) ===")
    print(cache["scoreboard"].to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    print()

    print("=== RAW METRICS DETAIL ===")
    print(cache["detail"].to_string(index=False))
    print()

    print("=== RANKS ===")
    print(cache["ranks"].to_string(index=False))


if __name__ == "__main__":
    main()
