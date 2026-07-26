"""Step 0 revision — 2014-2025 window.

CHANGE 2: which revenue / cogs tag actually carries the number in an EARLY year
          (2015) vs a RECENT year (2024), per probe company, across the ASC 606
          boundary (effective for FYs beginning after 2017-12-15).
CHANGE 3: KVUE / KHC / KDP early-history coverage + whether the deprecated
          pre-606 tags exist in those files at all.
CHANGE 4: measure how often as-originally-reported differs from as-restated.
"""

import json
import os
import time
from datetime import date

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw_samples")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "staples-research/1.0 mohamed.elhaddad80@gmail.com",
    "Accept-Encoding": "gzip, deflate",
})
YEARS = list(range(2014, 2026))

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",   # ASC 606 era
    "RevenueFromContractWithCustomerIncludingAssessedTax",   # ASC 606 era
    "Revenues",                                              # both eras
    "SalesRevenueNet",                                       # pre-606
    "SalesRevenueGoodsNet",                                  # pre-606
    "SalesRevenueServicesNet",                               # pre-606
]
COGS_TAGS = [
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",                                       # pre-606 common
    "CostOfRevenue",
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
]
PRE606_ONLY = ["SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet",
               "CostOfGoodsSold"]


def d(s):
    y, m, dd = (int(x) for x in s.split("-"))
    return date(y, m, dd)


def fiscal_year(end):
    e = d(end)
    return e.year if e.month >= 6 else e.year - 1


def annual(facts, tag, taxo="us-gaap", instant=False):
    """All qualifying annual 10-K facts, grouped fiscal_year -> [entries]."""
    node = facts.get("facts", {}).get(taxo, {}).get(tag)
    if not node:
        return None
    out = {}
    for unit, entries in node["units"].items():
        if unit not in ("USD", "shares", "USD/shares"):
            continue
        for e in entries:
            if not str(e.get("form", "")).startswith("10-K"):
                continue
            if ("start" in e) == instant:
                continue
            if not instant and not (300 <= (d(e["end"]) - d(e["start"])).days <= 400):
                continue
            out.setdefault(fiscal_year(e["end"]), []).append(e)
    return out


def value(facts, tag, fy, policy="latest", **kw):
    """policy: 'latest' = as-restated; 'earliest' = as-originally-reported."""
    a = annual(facts, tag, **kw)
    if not a or fy not in a:
        return None
    rows = sorted(a[fy], key=lambda e: (e.get("filed", ""), e.get("form", "")))
    return (rows[0] if policy == "earliest" else rows[-1])["val"]


def load(fn):
    with open(os.path.join(RAW, fn)) as fh:
        return json.load(fh)


def fetch(tk, cik):
    path = os.path.join(RAW, f"{tk}_CIK{cik}_companyfacts.json")
    if not os.path.exists(path):
        r = SESSION.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                        timeout=60)
        r.raise_for_status()
        with open(path, "w") as fh:
            json.dump(r.json(), fh)
        time.sleep(0.15)
    return load(os.path.basename(path))


CIKS = json.load(open(os.path.join(BASE, "cik_map.json")))
PROBES = {t: fetch(t, CIKS[t]["cik"]) for t in ["KO", "PG", "KR"]}
YOUNG = {t: fetch(t, CIKS[t]["cik"]) for t in ["KVUE", "KHC", "KDP"]}


def era_table(title, tags, universe):
    print("\n" + "=" * 108)
    print(title)
    print("=" * 108)
    for tk, facts in universe.items():
        print(f"\n  --- {tk} ---")
        hdr = "  " + f"{'tag':<62}" + "".join(f"{y%100:>4}" for y in YEARS)
        print(hdr)
        print("  " + "-" * (62 + 4 * len(YEARS)))
        for tag in tags:
            a = annual(facts, tag)
            if a is None:
                continue
            cells = "".join("  ✓ " if y in a else "  . " for y in YEARS)
            if "✓" not in cells:
                continue
            print(f"  {tag:<62}{cells}")


era_table("CHANGE 2a — REVENUE tag coverage by fiscal year (✓ = annual 10-K value present)",
          REVENUE_TAGS, PROBES)
era_table("CHANGE 2b — COGS tag coverage by fiscal year",
          COGS_TAGS, PROBES)

# ---- explicit early-vs-recent value comparison -----------------------------
print("\n" + "=" * 108)
print("CHANGE 2c — WHICH tag actually carries the number: FY2015 vs FY2024")
print("=" * 108)
for label, tags in [("REVENUE", REVENUE_TAGS), ("COGS", COGS_TAGS)]:
    print(f"\n  {label}")
    for tk, facts in PROBES.items():
        for fy in (2015, 2024):
            hits = [(t, value(facts, t, fy)) for t in tags]
            hits = [(t, v) for t, v in hits if v is not None]
            if not hits:
                print(f"    {tk} FY{fy}: NO TAG RESOLVES")
                continue
            win, wv = hits[0]
            extra = "".join(f"\n                  also: {t:<58}{v:>18,.0f}"
                            for t, v in hits[1:])
            print(f"    {tk} FY{fy}: {win:<58}{wv:>18,.0f}{extra}")

# ---- CHANGE 3 --------------------------------------------------------------
print("\n" + "=" * 108)
print("CHANGE 3 — young entities: earliest fiscal year with data, and whether")
print("           deprecated pre-606 tags exist AT ALL in the file")
print("=" * 108)
for tk, facts in YOUNG.items():
    print(f"\n  --- {tk}  (CIK{CIKS[tk]['cik']}, {facts.get('entityName')}) ---")
    for tag in REVENUE_TAGS + ["NetIncomeLoss", "AssetsCurrent"]:
        inst = tag == "AssetsCurrent"
        a = annual(facts, tag, instant=inst)
        if a is None:
            continue
        ys = sorted(y for y in a if 2010 <= y <= 2026)
        if not ys:
            continue
        print(f"      {tag:<58} FY{min(ys)}..FY{max(ys)}  "
              f"({len(ys)} yrs, in-window {sorted(y for y in ys if y in YEARS)})")
    present = [t for t in PRE606_ONLY
               if facts.get("facts", {}).get("us-gaap", {}).get(t)]
    print(f"      pre-606 tags present in file at all: {present or 'NONE'}")

# ---- CHANGE 4 --------------------------------------------------------------
print("\n" + "=" * 108)
print("CHANGE 4 — as-ORIGINALLY-REPORTED (earliest filed) vs as-RESTATED (latest filed)")
print("=" * 108)
CHECK = [("Revenues", False), ("RevenueFromContractWithCustomerExcludingAssessedTax", False),
         ("NetIncomeLoss", False), ("OperatingIncomeLoss", False),
         ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", True),
         ("AssetsCurrent", True), ("InventoryNet", True),
         ("NetCashProvidedByUsedInOperatingActivities", False)]
total = diff = 0
for tk, facts in PROBES.items():
    lines = []
    for tag, inst in CHECK:
        a = annual(facts, tag, instant=inst)
        if not a:
            continue
        for fy in YEARS:
            if fy not in a:
                continue
            rows = sorted(a[fy], key=lambda e: (e.get("filed", ""), e.get("form", "")))
            if len(rows) < 2:
                continue
            o, r = rows[0], rows[-1]
            total += 1
            if o["val"] != r["val"]:
                diff += 1
                pct = (r["val"] - o["val"]) / abs(o["val"]) * 100 if o["val"] else float("nan")
                lines.append(f"      FY{fy} {tag[:46]:<46} orig={o['val']:>16,.0f} "
                             f"({o['filed']})  restated={r['val']:>16,.0f} ({r['filed']}) "
                             f"{pct:+.1f}%")
    print(f"\n  --- {tk} ---")
    if lines:
        for l in lines:
            print(l)
    else:
        print("      no divergences")
print(f"\n  TOTAL: {diff} of {total} fiscal-year/tag cells differ between the "
      f"original filing and the latest restatement ({diff/total*100:.1f}%)")
