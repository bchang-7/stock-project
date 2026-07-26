"""Step 0 edge-case probe:
 1. fiscalYearEnd for all 34 (submissions API) -> how many non-December.
 2. Bunge predecessor CIK (Bunge Limited) for FY2020-2022.
 3. BF-B share-count tags (dei:EntityCommonStockSharesOutstanding is absent).
 4. What `frame` SEC assigns to an April-FYE annual period -> validates the
    fiscal_year cutoff.
"""

import json
import os
import time

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw_samples")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "staples-research/1.0 mohamed.elhaddad80@gmail.com",
    "Accept-Encoding": "gzip, deflate",
})
SLEEP = 0.15
MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
          "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

cik_map = json.load(open(os.path.join(BASE, "cik_map.json")))

print("=" * 76)
print("1. FISCAL YEAR END (dei fiscalYearEnd, MMDD) for all 34")
print("=" * 76)
rows = []
for t, info in cik_map.items():
    r = SESSION.get(f"https://data.sec.gov/submissions/CIK{info['cik']}.json", timeout=30)
    r.raise_for_status()
    j = r.json()
    fye = j.get("fiscalYearEnd") or "????"
    rows.append((t, fye, j.get("name")))
    time.sleep(SLEEP)

rows.sort(key=lambda r: (r[1][:2], r[0]))
for t, fye, name in rows:
    mm = fye[:2]
    lbl = f"{MONTHS.get(mm, '??')}-{fye[2:]}"
    flag = "" if mm == "12" else "   <-- non-December"
    print(f"  {t:<6} {lbl:<8}{flag}   {name}")
noned = [r for r in rows if r[1][:2] != "12"]
print(f"\n  {len(noned)}/34 have a non-December fiscal year end.")

print("\n" + "=" * 76)
print("2. BUNGE predecessor — does 'Bunge Limited' exist as a separate CIK?")
print("=" * 76)
r = SESSION.get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=bunge"
                "&type=10-K&dateb=&owner=include&count=40&output=atom", timeout=30)
print("  browse-edgar status:", r.status_code)
for cik in ["0001144519", "0001996862"]:
    rr = SESSION.get(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=30)
    if rr.status_code != 200:
        print(f"  CIK{cik}: HTTP {rr.status_code}")
        continue
    j = rr.json()
    forms = j["filings"]["recent"]
    tenks = [(forms["filingDate"][i], forms["form"][i])
             for i in range(len(forms["form"])) if forms["form"][i].startswith("10-K")]
    print(f"  CIK{cik}  name={j.get('name')}  tickers={j.get('tickers')}")
    print(f"      former names: {[fn['name'] for fn in j.get('formerNames', [])]}")
    print(f"      recent 10-Ks: {tenks[:8]}")
    time.sleep(SLEEP)

print("\n" + "=" * 76)
print("3. BF-B share-count tags (dei EntityCommonStockSharesOutstanding absent)")
print("=" * 76)
bf = json.load(open(os.path.join(RAW, "BF-B_CIK0000014693_companyfacts.json")))
print("  dei tags available:", sorted(bf["facts"].get("dei", {}).keys()))
for tag in ["CommonStockSharesOutstanding", "CommonStockSharesIssued",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingBasic"]:
    node = bf["facts"]["us-gaap"].get(tag)
    if not node:
        print(f"  us-gaap:{tag}: ABSENT")
        continue
    for unit, entries in node["units"].items():
        rows = [e for e in entries if str(e.get("form", "")).startswith("10-K")
                and e["end"] >= "2024-01-01"]
        rows.sort(key=lambda e: e["end"])
        print(f"  us-gaap:{tag} [{unit}] last rows:")
        for e in rows[-3:]:
            print(f"      end={e['end']} fy={e.get('fy')} filed={e.get('filed')} "
                  f"val={e['val']:>16,}")

print("\n" + "=" * 76)
print("4. `frame` SEC assigns to April-FYE (BF-B) and Jan-FYE annual periods")
print("=" * 76)
for name, facts, tag in [
    ("BF-B", bf, "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("KR", json.load(open(os.path.join(RAW, "KR_CIK0000056873_companyfacts.json"))),
     "RevenueFromContractWithCustomerExcludingAssessedTax"),
]:
    node = facts["facts"]["us-gaap"][tag]
    for unit, entries in node["units"].items():
        seen = set()
        for e in entries:
            if "start" not in e or not str(e.get("form", "")).startswith("10-K"):
                continue
            if e.get("frame") and e["end"] >= "2020-01-01":
                k = (e["start"], e["end"], e["frame"])
                if k in seen:
                    continue
                seen.add(k)
                print(f"  {name}: {e['start']} -> {e['end']}   frame={e['frame']}")
