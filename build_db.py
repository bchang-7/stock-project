"""Step 4 -- run the 34-ticker loop and load SQLite.

    .venv/bin/python build_db.py            # as-originally-reported (default)
    .venv/bin/python build_db.py latest     # as-restated

Idempotent: every write is INSERT OR REPLACE, so re-running overwrites in
place. One bad ticker never kills the run -- it is logged to failures.log and
the loop continues.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone

import edgar_extract as ex
from edgar_fetch import NULL_YEARS, coverage_notes, fetch_company

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "staples.db")
FAILURES = os.path.join(BASE, "failures.log")
SCHEMA = os.path.join(BASE, "schema.sql")

PROV_COLS = ["ticker", "fiscal_year", "field", "tag", "filed", "form",
             "period_end", "filing_lag_days", "source_cik", "native_fy_label",
             "selector"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_tickers():
    with open(os.path.join(BASE, "tickers.txt")) as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def connect():
    conn = sqlite3.connect(DB)
    with open(SCHEMA) as fh:
        conn.executescript(fh.read())
    return conn


def write_ticker(conn, ticker, rows, provs, raw_by_cik):
    cols = ["ticker", "fiscal_year"] + ex.FUNDAMENTAL_FIELDS
    conn.executemany(
        f"INSERT OR REPLACE INTO fundamentals ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [tuple(r.get(c) for c in cols) for r in rows],
    )
    conn.executemany(
        f"INSERT OR REPLACE INTO fundamentals_provenance ({','.join(PROV_COLS)}) "
        f"VALUES ({','.join('?' * len(PROV_COLS))})",
        [tuple(p.get(c) for c in PROV_COLS) for p in provs],
    )
    stamp = now()
    conn.executemany(
        "INSERT OR REPLACE INTO raw_responses (ticker, cik, fetched_at, json) "
        "VALUES (?,?,?,?)",
        [(ticker, cik, stamp, json.dumps(cf)) for cik, cf in raw_by_cik.items()],
    )
    conn.commit()


def log_failure(ticker, exc):
    with open(FAILURES, "a") as fh:
        fh.write(f"{now()}\t{ticker}\t{type(exc).__name__}: {exc}\n")
        fh.write(textwrap_indent(traceback.format_exc()))


def textwrap_indent(s):
    return "".join("\t\t" + ln + "\n" for ln in s.strip().splitlines()) + "\n"


def summarise(ticker, rows, provs):
    """Per-ticker coverage facts for the universe-wide summary."""
    populated = [r for r in rows
                 if any(r[f] is not None for f in ex.FUNDAMENTAL_FIELDS)]
    years = sorted(r["fiscal_year"] for r in populated)
    all_null = [f for f in ex.FUNDAMENTAL_FIELDS
                if all(r[f] is None for r in rows)]

    notes = coverage_notes(ticker, rows, provs)
    flags = {"split": [], "net_interest": [], "non_contemporaneous": [],
             "cogs_break": [], "shares_last_resort": []}
    for n in notes:
        if "split-adjust" in n:
            flags["split"].append(n)
        elif "NET of interest income" in n:
            flags["net_interest"].append(n)
        elif "non-contemporaneous" in n:
            flags["non_contemporaneous"].append(n)
        elif "cogs definition changes" in n:
            flags["cogs_break"].append(n)
        elif "CommonStockSharesIssued" in n:
            flags["shares_last_resort"].append(n)

    filled = sum(1 for r in rows for f in ex.FUNDAMENTAL_FIELDS
                 if r[f] is not None)
    total = len(rows) * len(ex.FUNDAMENTAL_FIELDS)
    return {"ticker": ticker, "years": years, "n_years": len(years),
            "all_null": all_null, "flags": flags, "notes": notes,
            "fill_pct": 100.0 * filled / total if total else 0.0}


def print_summary(summaries, failures):
    print("\n" + "=" * 104)
    print("COVERAGE SUMMARY -- all 34 tickers, FY2014-2025")
    print("=" * 104)
    print(f"  {'ticker':<8}{'yrs':>4}{'span':>12}{'fill%':>7}  all-NULL fields")
    print("  " + "-" * 100)
    for s in summaries:
        span = f"{s['years'][0]}-{s['years'][-1]}" if s["years"] else "-"
        nulls = ",".join(s["all_null"]) or "-"
        if len(nulls) > 62:
            nulls = nulls[:59] + "..."
        print(f"  {s['ticker']:<8}{s['n_years']:>4}{span:>12}{s['fill_pct']:>7.1f}  {nulls}")

    print("\n  --- field availability across the universe ---")
    n = len(summaries)
    for f in ex.FUNDAMENTAL_FIELDS:
        have = sum(1 for s in summaries if f not in s["all_null"])
        bar = "#" * int(round(30 * have / n)) if n else ""
        print(f"    {f:<28}{have:>3}/{n}  {bar}")

    print("\n  --- per-year flags ---")
    labels = {"split": "stock-split basis mismatch (per-share metrics unsafe)",
              "net_interest": "interest_expense is NET of interest income",
              "non_contemporaneous": "value is a later comparative, not an original",
              "cogs_break": "cogs definition/tag break",
              "shares_last_resort": "shares fell back to Issued incl. treasury"}
    for key, label in labels.items():
        hits = [s for s in summaries if s["flags"][key]]
        print(f"\n    {label}: {len(hits)} ticker(s)")
        for s in hits:
            for n in s["flags"][key]:
                print(f"      {s['ticker']:<7}{n}")

    print("\n  --- suppressed (predecessor-entity) ranges ---")
    for tk, yrs in sorted(NULL_YEARS.items()):
        y = sorted(yrs)
        print(f"    {tk:<7}FY{y[0]}-{y[-1]} nulled")

    if failures:
        print(f"\n  --- FAILURES ({len(failures)}) -- see failures.log ---")
        for tk, msg in failures:
            print(f"    {tk:<8}{msg}")
    else:
        print("\n  --- no failures ---")


def main(policy="earliest"):
    tickers = load_tickers()
    conn = connect()
    if os.path.exists(FAILURES):
        os.remove(FAILURES)

    summaries, failures = [], []
    for i, tk in enumerate(tickers, 1):
        try:
            rows, provs, raw = fetch_company(tk, policy=policy)
            write_ticker(conn, tk, rows, provs, raw)
            s = summarise(tk, rows, provs)
            summaries.append(s)
            print(f"  [{i:>2}/{len(tickers)}] {tk:<6} "
                  f"{s['n_years']:>2} yrs  fill {s['fill_pct']:>5.1f}%  "
                  f"cik={','.join(raw)}")
        except Exception as exc:                      # noqa: BLE001
            log_failure(tk, exc)
            failures.append((tk, f"{type(exc).__name__}: {exc}"))
            print(f"  [{i:>2}/{len(tickers)}] {tk:<6} FAILED -> failures.log")

    print_summary(summaries, failures)

    n = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
    p = conn.execute("SELECT COUNT(*) FROM fundamentals_provenance").fetchone()[0]
    r = conn.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]
    print(f"\n  wrote {n} fundamentals rows, {p} provenance rows, "
          f"{r} raw responses -> {os.path.basename(DB)} "
          f"({os.path.getsize(DB)/1e6:.0f} MB), policy={policy}")
    conn.close()
    return summaries


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "earliest")
