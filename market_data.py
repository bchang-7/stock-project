"""Step 5 -- market table + an independent FCF series for cross-checking.

    .venv/bin/python market_data.py

Quotes are DELAYED (yfinance/Yahoo, typically 15 min) and the as-of UTC time is
recorded per row. One bad ticker is logged and skipped, never fatal.

The FCF pull exists because EDGAR publishes no free-cash-flow element, so the
derived free_cash_flow column has nothing in the filing to validate against.
yfinance only carries ~4 years of annual cash-flow history, so this covers a
minority of FY2014-2025.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone

import yfinance as yf

import edgar_extract as ex
from build_db import DB, SCHEMA, load_tickers, now

BASE = os.path.dirname(os.path.abspath(__file__))
FAILURES = os.path.join(BASE, "failures.log")
SLEEP = 0.4

# SEC writes class shares with a hyphen and so does Yahoo, but our ticker file
# uses the dotted form.
YF_SYMBOL = {"BF.B": "BF-B"}

CF_ROWS = {"free_cash_flow": "Free Cash Flow",
           "operating_cash_flow": "Operating Cash Flow",
           "capex": "Capital Expenditure"}


def log_failure(ticker, stage, exc):
    with open(FAILURES, "a") as fh:
        fh.write(f"{now()}\t{ticker}\t{stage}\t{type(exc).__name__}: {exc}\n")


def market_row(ticker):
    sym = YF_SYMBOL.get(ticker, ticker)
    t = yf.Ticker(sym)

    price = mcap = beta = sector = None
    try:
        fi = t.fast_info
        price = fi.get("lastPrice")
        mcap = fi.get("marketCap")
    except Exception:                                  # noqa: BLE001
        pass

    info = {}
    try:
        info = t.info or {}
    except Exception:                                  # noqa: BLE001
        info = {}

    if price is None:
        price = info.get("currentPrice") or info.get("regularMarketPrice")
    if mcap is None:
        mcap = info.get("marketCap")
    beta = info.get("beta")
    sector = info.get("sector")

    return {"ticker": ticker, "price": _f(price), "market_cap": _f(mcap),
            "beta": _f(beta), "sector": sector}, t


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def fcf_rows(ticker, t):
    """Annual FCF/CFO/capex from yfinance, keyed to OUR fiscal_year rule so it
    lines up with the fundamentals table."""
    try:
        cf = t.cashflow
    except Exception:                                  # noqa: BLE001
        return []
    if cf is None or cf.empty:
        return []

    stamp = now()
    out = []
    for col in cf.columns:
        end = col.date().isoformat() if hasattr(col, "date") else str(col)[:10]
        vals = {}
        for key, label in CF_ROWS.items():
            vals[key] = _f(cf.loc[label, col]) if label in cf.index else None
        if all(v is None for v in vals.values()):
            continue
        out.append((ticker, ex.fiscal_year_of(end), end,
                    vals["free_cash_flow"], vals["operating_cash_flow"],
                    vals["capex"], stamp))
    return out


def main():
    tickers = load_tickers()
    conn = sqlite3.connect(DB)
    with open(SCHEMA) as fh:
        conn.executescript(fh.read())

    ok, failed, fcf_total = [], [], 0
    print(f"  fetching market data for {len(tickers)} tickers "
          f"(delayed quotes, as-of {now()})\n")

    for i, tk in enumerate(tickers, 1):
        try:
            row, t = market_row(tk)
            conn.execute(
                "INSERT OR REPLACE INTO market (ticker, price, market_cap, beta, sector) "
                "VALUES (?,?,?,?,?)",
                (row["ticker"], row["price"], row["market_cap"], row["beta"],
                 row["sector"]),
            )
            rows = fcf_rows(tk, t)
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO market_fcf_crosscheck "
                    "(ticker, fiscal_year, period_end, yf_free_cash_flow, "
                    "yf_operating_cash_flow, yf_capex, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?)", rows)
                fcf_total += len(rows)
            conn.commit()
            ok.append(row)
            miss = [k for k in ("price", "market_cap", "beta", "sector")
                    if row[k] is None]
            print(f"  [{i:>2}/{len(tickers)}] {tk:<6} "
                  f"px={_s(row['price']):>9} mcap={_bn(row['market_cap']):>9} "
                  f"beta={_s(row['beta']):>6} {str(row['sector'] or '-'):<20}"
                  f"fcf_yrs={len(rows)}"
                  + (f"  MISSING:{','.join(miss)}" if miss else ""))
        except Exception as exc:                       # noqa: BLE001
            log_failure(tk, "market", exc)
            failed.append((tk, f"{type(exc).__name__}: {exc}"))
            print(f"  [{i:>2}/{len(tickers)}] {tk:<6} FAILED -> failures.log")
        time.sleep(SLEEP)

    print("\n  --- market table summary ---")
    for col in ("price", "market_cap", "beta", "sector"):
        have = sum(1 for r in ok if r[col] is not None)
        print(f"    {col:<14}{have:>3}/{len(tickers)}")
    sectors = {}
    for r in ok:
        sectors[r["sector"]] = sectors.get(r["sector"], 0) + 1
    print(f"    sectors: {sectors}")
    print(f"    fcf cross-check rows: {fcf_total}")
    if failed:
        print(f"\n  --- FAILURES ({len(failed)}) ---")
        for tk, m in failed:
            print(f"    {tk:<8}{m}")
    else:
        print("\n  --- no failures ---")
    conn.close()


def _s(v):
    return "-" if v is None else f"{v:,.2f}"


def _bn(v):
    return "-" if v is None else f"{v/1e9:,.1f}B"


if __name__ == "__main__":
    main()
