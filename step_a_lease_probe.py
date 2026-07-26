"""Step A -- probe finance/capital-lease tags across all 34 cached filers before
touching the total_debt chain.

The question that decides the rule: for each ticker-year, is the long-term debt
tag we use lease-INCLUSIVE or debt-ONLY, and do separate finance-lease tags also
exist? Adding leases on top of a lease-inclusive debt tag double-counts.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict

import edgar_extract as ex
from edgar_fetch import EXTRA_CIKS, _cik_map

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw_samples")
YEARS = range(2014, 2026)
G = "us-gaap"

LEASE_NONCURRENT = ["FinanceLeaseLiabilityNoncurrent", "CapitalLeaseObligationsNoncurrent"]
LEASE_CURRENT = ["FinanceLeaseLiabilityCurrent", "CapitalLeaseObligationsCurrent"]
LEASE_COMBINED = ["FinanceLeaseLiability", "CapitalLeaseObligations"]
ALL_LEASE = LEASE_NONCURRENT + LEASE_CURRENT + LEASE_COMBINED

LEASE_INCLUSIVE_MARK = "CapitalLeaseObligations"   # substring of the debt tags


def load_entities(ticker):
    cmap = _cik_map()
    ciks = [cmap[ticker]["cik"]] + EXTRA_CIKS.get(ticker, [])
    out = []
    for cik in ciks:
        p = os.path.join(RAW, f"{ticker}_CIK{cik}_companyfacts.json")
        if os.path.exists(p):
            with open(p) as fh:
                out.append((cik, json.load(fh)))
    return out


def spec(name, tags):
    return ex.FieldSpec(name, ex.INSTANT, [(G, t) for t in tags])


def val_and_tag(entities, sp, fy):
    for cik, cf in entities:
        v, p = ex.extract_field(cf, sp, fy)
        if v is not None:
            return v, p["tag"].split(":")[-1], cik
    return None, None, None


def main():
    tickers = [ln.strip() for ln in open(os.path.join(BASE, "tickers.txt")) if ln.strip()]

    print("=" * 104)
    print("A1 -- which finance/capital-lease tags each filer uses (annual 10-K values, FY2014-2025)")
    print("=" * 104)
    presence = {}
    for tk in tickers:
        ents = load_entities(tk)
        rowmap = {}
        for tag in ALL_LEASE:
            yrs = set()
            for _cik, cf in ents:
                sp = spec("x", [tag])
                for fy in YEARS:
                    if ex.extract_field(cf, sp, fy)[0] is not None:
                        yrs.add(fy)
            if yrs:
                rowmap[tag] = sorted(yrs)
        presence[tk] = rowmap
        if rowmap:
            print(f"\n  {tk}")
            for tag, yrs in rowmap.items():
                print(f"      {tag:<38}{_compact(yrs)}")
        else:
            print(f"\n  {tk}   -- NO finance/capital-lease tags at all --")

    print("\n" + "=" * 104)
    print("A2 -- OVERLAP: debt tag kind vs availability of separate lease tags")
    print("=" * 104)
    print("  (the double-count test: 'lease-incl debt tag' + 'separate lease tags' in the")
    print("   same ticker-year means adding leases would count them twice)")

    buckets = defaultdict(int)
    risk = defaultdict(list)
    detail = {}
    for tk in tickers:
        ents = load_entities(tk)
        for fy in YEARS:
            nc, nc_tag, _ = val_and_tag(ents, ex.COMPONENTS["_lt_noncurrent"], fy)
            cu, cu_tag, _ = val_and_tag(ents, ex.COMPONENTS["_lt_current"], fy)
            if nc is None and cu is None:
                continue
            incl_nc = bool(nc_tag and LEASE_INCLUSIVE_MARK in nc_tag)
            incl_cu = bool(cu_tag and LEASE_INCLUSIVE_MARK in cu_tag)
            lnc, lnc_tag, _ = val_and_tag(ents, spec("lnc", LEASE_NONCURRENT), fy)
            lcu, lcu_tag, _ = val_and_tag(ents, spec("lcu", LEASE_CURRENT), fy)
            kind = ("lease-inclusive" if (incl_nc or incl_cu) else "debt-only")
            has = "has separate lease tags" if (lnc or lcu) else "no lease tags"
            buckets[(kind, has)] += 1
            if kind == "lease-inclusive" and (lnc or lcu):
                risk[tk].append(fy)
            detail[(tk, fy)] = dict(nc=nc, nc_tag=nc_tag, cu=cu, cu_tag=cu_tag,
                                    incl_nc=incl_nc, incl_cu=incl_cu,
                                    lnc=lnc, lnc_tag=lnc_tag, lcu=lcu, lcu_tag=lcu_tag)

    print(f"\n    {'debt tag kind':<20}{'lease tags':<28}{'ticker-years':>14}")
    for (kind, has), n in sorted(buckets.items()):
        print(f"    {kind:<20}{has:<28}{n:>14}")

    print(f"\n  DOUBLE-COUNT RISK -- lease-inclusive debt tag AND separate lease tags "
          f"present ({sum(len(v) for v in risk.values())} ticker-years, "
          f"{len(risk)} tickers):")
    if not risk:
        print("    none")
    for tk, yrs in sorted(risk.items()):
        print(f"    {tk:<7}{_compact(yrs)}")

    print("\n" + "=" * 104)
    print("A3 -- KR (debt-only, needs leases) and KO (lease-inclusive, must NOT change)")
    print("=" * 104)
    for tk in ("KR", "KO"):
        print(f"\n  --- {tk} ---")
        print(f"    {'FY':<6}{'lt_noncurrent tag':<44}{'value':>12}"
              f"{'lease_nc':>11}{'lease_cur':>11}{'current now':>13}{'corrected':>12}")
        ents = load_entities(tk)
        for fy in YEARS:
            d = detail.get((tk, fy))
            if not d:
                continue
            now, prov = ex.derive_total_debt(_first(ents), fy)
            add_nc = 0 if d["incl_nc"] else (d["lnc"] or 0)
            add_cu = 0 if d["incl_cu"] else (d["lcu"] or 0)
            corrected = (now or 0) + add_nc + add_cu
            mark = "" if (add_nc or add_cu) else "   (no change)"
            print(f"    {fy:<6}{str(d['nc_tag']):<44}{_m(d['nc']):>12}"
                  f"{_m(d['lnc']):>11}{_m(d['lcu']):>11}{_m(now):>13}"
                  f"{_m(corrected):>12}{mark}")


def _first(ents):
    return ents[0][1]


def _m(v):
    return "-" if v is None else f"{v/1e6:,.0f}"


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


if __name__ == "__main__":
    main()
