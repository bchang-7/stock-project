"""Step 3 -- fetch and parse ONE company end to end.

fetch_company(ticker) -> (fundamentals_rows, provenance_rows, raw_by_cik)

One companyfacts call per company (two for BG, merged). Raw JSON is cached to
raw_samples/ and reused if less than CACHE_HOURS old.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

import edgar_extract as ex

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw_samples")

USER_AGENT = "staples-research/1.0 mohamed.elhaddad80@gmail.com"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SLEEP = 0.15          # SEC caps clients at 10 req/sec
CACHE_HOURS = 24
YEARS = range(2014, 2026)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})

# BG re-domesticated Bermuda -> Switzerland in Nov 2023 and took a new CIK.
# Successor first: it wins on any overlapping fiscal year.
EXTRA_CIKS = {"BG": ["0001144519"]}

# Predecessor-entity figures that would fabricate merger-driven growth spikes.
# KHC FY2014-15 is H.J. Heinz standalone; KDP FY2014-18 is Dr Pepper Snapple
# standalone; KVUE simply did not exist.
NULL_YEARS = {
    "KHC": set(range(2014, 2016)),
    "KDP": set(range(2014, 2019)),
    "KVUE": set(range(2014, 2021)),
}

# Companies whose own "FY" label differs from the economic fiscal_year key.
NATIVE_LABEL_OFFSET = {t: 1 for t in
                       ["WMT", "TGT", "STZ", "BF.B", "CASY", "SJM", "GIS"]}

# shares_outstanding vs net_income/eps: flag beyond this relative gap
SHARE_CONSISTENCY_TOL = 0.25


def _cik_map():
    with open(os.path.join(BASE, "cik_map.json")) as fh:
        return json.load(fh)


def _cache_path(ticker, cik):
    return os.path.join(RAW, f"{ticker}_CIK{cik}_companyfacts.json")


def fetch_companyfacts(ticker, cik, force=False):
    """Download companyfacts, or reuse a cache entry younger than CACHE_HOURS."""
    path = _cache_path(ticker, cik)
    if not force and os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < CACHE_HOURS * 3600:
            with open(path) as fh:
                return json.load(fh), path, True
    os.makedirs(RAW, exist_ok=True)
    resp = SESSION.get(FACTS_URL.format(cik=cik), timeout=60)
    resp.raise_for_status()
    data = resp.json()
    with open(path, "w") as fh:
        json.dump(data, fh)
    time.sleep(SLEEP)
    return data, path, False


def merge_entities(entities):
    """entities: [(cik, companyfacts)] in PREFERENCE order (successor first).

    Returns lookup(spec_or_callable, fiscal_year) -> (value, source_cik).
    The first entity that produces a non-None value wins, so the successor is
    preferred on overlapping years and the predecessor fills the early gap.
    """
    def lookup(spec_or_fn, fiscal_year, policy="earliest", with_prov=False):
        last_prov = None
        for cik, cf in entities:
            if callable(spec_or_fn):
                val, prov = spec_or_fn(cf, fiscal_year, policy)
            else:
                val, prov = ex.extract_field(cf, spec_or_fn, fiscal_year, policy)
            last_prov = prov
            if val is not None:
                return (val, cik, prov) if with_prov else (val, cik)
        return (None, None, last_prov) if with_prov else (None, None)
    return lookup


def fetch_company(ticker, policy="earliest", force=False):
    """Parse all 23 fields for FY2014-2025.

    Returns (fundamentals_rows, provenance_rows, raw_by_cik).
    """
    cmap = _cik_map()
    if ticker not in cmap:
        raise KeyError(f"{ticker} not in cik_map.json")

    ciks = [cmap[ticker]["cik"]] + EXTRA_CIKS.get(ticker, [])
    entities, raw_by_cik = [], {}
    for cik in ciks:
        cf, _, _cached = fetch_companyfacts(ticker, cik, force=force)
        entities.append((cik, cf))
        raw_by_cik[cik] = cf

    rows, provs = parse_entities(ticker, entities, policy=policy)
    return rows, provs, raw_by_cik


def parse_entities(ticker, entities, policy="earliest"):
    """Pure parsing half of fetch_company -- no network, no disk.

    entities: [(cik, companyfacts)] in preference order (successor first).
    """
    lookup = merge_entities(entities)
    fye_map = {}
    for _, cf in entities:
        for fy, end in ex.fiscal_period_ends(cf).items():
            fye_map.setdefault(fy, end)

    offset = NATIVE_LABEL_OFFSET.get(ticker, 0)
    nulled = NULL_YEARS.get(ticker, set())

    fundamentals_rows, provenance_rows = [], []
    for fy in YEARS:
        row = {"ticker": ticker, "fiscal_year": fy}
        provs = []

        if fy in nulled:
            for f in ex.FUNDAMENTAL_FIELDS:
                row[f] = None
            provs.append({"field": "*", "tag": None, "filed": None, "form": None,
                          "period_end": None, "filing_lag_days": None,
                          "selector": "suppressed:predecessor_entity"})
        else:
            for field in ex.FUNDAMENTAL_FIELDS:
                if field in ex.DERIVED:
                    val, cik, prov = lookup(ex.DERIVED[field], fy, policy, with_prov=True)
                else:
                    val, cik, prov = lookup(ex.SPECS[field], fy, policy, with_prov=True)

                # shares_outstanding fallback ladder. dei is cover-date
                # remapped (never matched on period end) and comes BEFORE
                # CommonStockSharesIssued, which is issued-incl-treasury and
                # wildly overstates filers with big buyback programmes.
                if field == "shares_outstanding" and val is None:
                    for c, cf in entities:
                        val, prov = ex.dei_shares_outstanding(cf, fy, fye_map, policy)
                        if val is not None:
                            cik = c
                            break
                if field == "shares_outstanding" and val is None:
                    val, cik, prov = lookup(ex.COMPONENTS["_shares_issued"], fy,
                                            policy, with_prov=True)
                    if val is not None:
                        prov = dict(prov)
                        prov["field"] = "shares_outstanding"
                        prov["selector"] = "last_resort:CommonStockSharesIssued"

                row[field] = val
                prov = dict(prov)
                prov["source_cik"] = cik
                prov["native_fy_label"] = fy + offset if val is not None else None
                provs.append(prov)

            # stored in provenance only -- lets you check eps x shares ~= net income
            wd, wcik, wprov = lookup(ex.COMPONENTS["_weighted_diluted_shares"],
                                     fy, policy, with_prov=True)
            wprov = dict(wprov)
            wprov["field"] = "_weighted_diluted_shares"
            wprov["source_cik"] = wcik
            wprov["native_fy_label"] = fy + offset if wd is not None else None
            wprov["selector"] = (f"crosscheck:{wd:.0f}" if wd is not None else "missing")
            provs.append(wprov)

        fundamentals_rows.append(row)
        for p in provs:
            p = dict(p)
            p["ticker"] = ticker
            p["fiscal_year"] = fy
            p.setdefault("source_cik", None)
            p.setdefault("native_fy_label", None)
            provenance_rows.append(p)

    return fundamentals_rows, provenance_rows


def coverage_notes(ticker, rows, provs):
    """Flag the things a caller must not silently treat as clean data."""
    notes = []
    nulled = sorted(NULL_YEARS.get(ticker, set()))
    if nulled:
        notes.append(f"FY{nulled[0]}-{nulled[-1]} suppressed (predecessor entity)")

    for f in ex.FUNDAMENTAL_FIELDS:
        missing = [r["fiscal_year"] for r in rows
                   if r[f] is None and r["fiscal_year"] not in set(nulled)]
        if missing:
            notes.append(f"{f}: NULL for {_compact(missing)}")

    late = sorted({p["fiscal_year"] for p in provs
                   if (p.get("filing_lag_days") or 0) > ex.COMPARATIVE_LAG_DAYS})
    if late:
        notes.append(f"non-contemporaneous (filing lag >{ex.COMPARATIVE_LAG_DAYS}d, "
                     f"value is a later comparative not an original): {_compact(late)}")

    net_int = sorted(p["fiscal_year"] for p in provs
                     if p["field"] == "interest_expense"
                     and p["tag"] in ex.NET_INTEREST_TAGS)
    if net_int:
        notes.append(f"interest_expense is NET of interest income (tag "
                     f"InterestIncomeExpenseNonoperatingNet) for {_compact(net_int)}")

    no_lease = sorted(p["fiscal_year"] for p in provs
                      if p["field"] == "total_debt" and p["tag"]
                      and "CapitalLeaseObligations" not in p["tag"]
                      and "FinanceLease" not in p["tag"])
    if no_lease:
        notes.append(f"total_debt carries NO finance-lease component for "
                     f"{_compact(no_lease)}: debt-only tags and no lease liability "
                     f"reported. May genuinely have none -- distinguishes 'no "
                     f"leases' from 'leases missing'")

    ni_common = sorted(p["fiscal_year"] for p in provs
                       if p["field"] == "net_income"
                       and p["tag"] == ex.NET_INCOME_TO_COMMON_TAG)
    if ni_common:
        notes.append(f"net_income is income AVAILABLE TO COMMON (net of preferred "
                     f"dividends) for {_compact(ni_common)}")

    last_resort = sorted(p["fiscal_year"] for p in provs
                         if p["field"] == "shares_outstanding"
                         and str(p.get("selector", "")).startswith("last_resort"))
    if last_resort:
        notes.append(f"shares_outstanding falls back to CommonStockSharesIssued "
                     f"(issued incl. treasury) for {_compact(last_resort)}")

    # As-originally-reported values are NOT split-adjusted. The dei count is
    # taken at the 10-K COVER date, which can sit on the far side of a split
    # from the EPS in the same filing -- KR's FY2014 cover says 953.7M while
    # that filing's weighted-diluted is 497.0M. Cross-check against ni/eps and
    # surface any year the two disagree rather than emitting a silently
    # inconsistent pair.
    for r in rows:
        s, ni, eps = r["shares_outstanding"], r["net_income"], r["eps"]
        if not (s and ni and eps):
            continue
        implied = ni / eps
        if implied and abs(s - implied) / abs(implied) > SHARE_CONSISTENCY_TOL:
            notes.append(
                f"FY{r['fiscal_year']}: shares_outstanding {s/1e6:,.1f}M "
                f"disagrees with net_income/eps {implied/1e6:,.1f}M "
                f"(ratio {s/implied:.2f}x) -- cover-date count and EPS are on "
                f"different stock-split bases; do NOT compute per-share metrics "
                f"for this year without split-adjusting")

    cogs_tags = {p["fiscal_year"]: p["tag"] for p in provs
                 if p["field"] == "cogs" and p["tag"]}
    switches = [fy for fy in sorted(cogs_tags)
                if fy - 1 in cogs_tags and cogs_tags[fy] != cogs_tags[fy - 1]]
    for fy in switches:
        notes.append(f"cogs definition changes at FY{fy}: "
                     f"{cogs_tags[fy-1].split(':')[-1]} -> {cogs_tags[fy].split(':')[-1]}")
    return notes


def _compact(years):
    years = sorted(years)
    out, i = [], 0
    while i < len(years):
        j = i
        while j + 1 < len(years) and years[j + 1] == years[j] + 1:
            j += 1
        out.append(str(years[i]) if i == j else f"{years[i]}-{years[j]}")
        i = j + 1
    return ",".join(out)


def _fmt(field, v):
    if v is None:
        return "None"
    if field == "eps":
        return f"{v:,.2f}"
    if field == "shares_outstanding":
        return f"{v/1e6:,.1f}M sh"
    return f"{v/1e6:,.1f}"


def main(ticker="KR", policy="earliest"):
    rows, provs, raw = fetch_company(ticker, policy=policy)
    print(f"\n{'='*100}")
    print(f"{ticker}  policy={policy}  CIKs={list(raw)}  (values in $ MILLIONS "
          f"except eps and shares)")
    print("=" * 100)

    for field in ex.FUNDAMENTAL_FIELDS:
        cells = "".join(f"{_fmt(field, r[field]):>14}" for r in rows)
        print(f"  {field:<27}{cells}")
    hdr = "".join(f"{r['fiscal_year']:>14}" for r in rows)
    print(f"  {'FY':<27}{hdr}")

    for field in ("revenue", "cogs"):
        print(f"\n  --- provenance: {field} ---")
        print(f"    {'FY':<6}{'tag':<62}{'filed':<12}{'form':<8}{'end':<12}"
              f"{'lag':>5}  selector")
        for p in provs:
            if p["field"] != field:
                continue
            print(f"    {p['fiscal_year']:<6}{str(p['tag']):<62}"
                  f"{str(p['filed']):<12}{str(p['form']):<8}"
                  f"{str(p['period_end']):<12}{str(p['filing_lag_days']):>5}  "
                  f"{p['selector']}")

    print("\n  --- coverage notes ---")
    for n in coverage_notes(ticker, rows, provs) or ["(none)"]:
        print(f"    * {n}")
    return rows, provs


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "KR",
         sys.argv[2] if len(sys.argv) > 2 else "earliest")
