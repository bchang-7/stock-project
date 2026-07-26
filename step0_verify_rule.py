"""Step 0e verification: apply the proposed end-date-based fiscal_year rule to
the three probes, and check two risk cases (BG re-domestication CIK, BF-B
multi-class share counts)."""

import json
import os
import time
from collections import defaultdict
from datetime import date

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw_samples")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "staples-research/1.0 mohamed.elhaddad80@gmail.com",
    "Accept-Encoding": "gzip, deflate",
})


def d(s):
    y, m, dd = (int(x) for x in s.split("-"))
    return date(y, m, dd)


def fiscal_year(end):
    """Assign a period to the calendar year holding most of it.
    Matches SEC's own `frame` convention (KR FYE 2024-02-03 -> CY2023Q4I)."""
    e = d(end)
    return e.year if e.month >= 6 else e.year - 1


def pick(facts, taxo, tag, instant=False):
    """Select ONE annual 10-K value per fiscal year.
    filter: form 10-K*, ~365d duration (flows) or instant (balances)
    dedup:  keep the LATEST `filed` for each period end."""
    node = facts.get("facts", {}).get(taxo, {}).get(tag)
    if not node:
        return {}
    best = {}
    for unit, entries in node["units"].items():
        if unit.startswith("USD/"):  # per-share units handled separately
            pass
        for e in entries:
            if not str(e.get("form", "")).startswith("10-K"):
                continue
            if ("start" in e) == instant:
                continue
            if not instant:
                if not (300 <= (d(e["end"]) - d(e["start"])).days <= 400):
                    continue
            key = e["end"]
            prev = best.get(key)
            if prev is None or (e.get("filed", ""), e.get("form", "")) > (
                prev.get("filed", ""), prev.get("form", "")
            ):
                best[key] = e
    out = {}
    for end, e in best.items():
        fy = fiscal_year(end)
        # if two period-ends land on the same fiscal year (52/53-week drift),
        # keep the later end date
        if fy not in out or end > out[fy][0]:
            out[fy] = (end, e["val"], e.get("filed"))
    return out


def show(name, path_or_facts, tags):
    facts = path_or_facts
    print(f"\n{'='*88}\n{name}\n{'='*88}")
    for label, (taxo, tag, inst) in tags.items():
        res = pick(facts, taxo, tag, instant=inst)
        print(f"  {label:<24} {taxo}:{tag}")
        for fy in range(2019, 2027):
            if fy in res:
                end, val, filed = res[fy]
                print(f"      FY{fy}  end={end}  filed={filed}  val={val:>18,.0f}")
            elif 2020 <= fy <= 2025:
                print(f"      FY{fy}  -- MISSING --")


TAGS = {
    "revenue(Revenues)": ("us-gaap", "Revenues", False),
    "revenue(RevFromContract)": ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", False),
    "net_income": ("us-gaap", "NetIncomeLoss", False),
    "current_assets(instant)": ("us-gaap", "AssetsCurrent", True),
}

for t, fn in [("KO", "KO_CIK0000021344_companyfacts.json"),
              ("PG", "PG_CIK0000080424_companyfacts.json"),
              ("KR", "KR_CIK0000056873_companyfacts.json")]:
    with open(os.path.join(RAW, fn)) as fh:
        show(f"{t} — proposed rule applied", json.load(fh), TAGS)

# ---- risk case 1: BG (Bunge Global SA, CIK 1996862) re-domesticated in 2023.
# ---- risk case 2: BF-B multi-class share counts.
print("\n\n" + "#" * 88)
print("RISK CASES")
print("#" * 88)
for tk, cik in [("BG", "0001996862"), ("BF-B", "0000014693")]:
    r = SESSION.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", timeout=60)
    r.raise_for_status()
    f = r.json()
    with open(os.path.join(RAW, f"{tk}_CIK{cik}_companyfacts.json"), "w") as fh:
        json.dump(f, fh)
    show(f"{tk} (CIK{cik}) — {f.get('entityName')}", f, TAGS)
    node = f["facts"].get("dei", {}).get("EntityCommonStockSharesOutstanding")
    print(f"  dei:EntityCommonStockSharesOutstanding present: {node is not None}")
    if node:
        for unit, entries in node["units"].items():
            rows = [e for e in entries if str(e.get("form","")).startswith("10-K")]
            rows.sort(key=lambda e: e["end"])
            for e in rows[-4:]:
                print(f"      cover={e['end']} fy={e.get('fy')} filed={e.get('filed')} "
                      f"val={e['val']:>16,}")
    time.sleep(0.15)
