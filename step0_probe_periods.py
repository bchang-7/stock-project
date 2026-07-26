"""Step 0e: examine the fy / fp / form / start / end / filed / frame structure
to design the annual-value selection + dedup rule."""

import json
import os
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw_samples")
PROBES = {
    "KO": "KO_CIK0000021344_companyfacts.json",
    "PG": "PG_CIK0000080424_companyfacts.json",
    "KR": "KR_CIK0000056873_companyfacts.json",
}


def load(t):
    with open(os.path.join(RAW, PROBES[t])) as fh:
        return json.load(fh)


def d(s):
    y, m, dd = (int(x) for x in s.split("-"))
    return date(y, m, dd)


def dump(data, t, taxo, tag, unit=None):
    node = data["facts"][taxo][tag]
    print(f"\n{'='*104}\n{t}  {taxo}:{tag}\n{'='*104}")
    for u, entries in node["units"].items():
        if unit and u != unit:
            continue
        rows = []
        for e in entries:
            if "start" in e:
                dur = (d(e["end"]) - d(e["start"])).days
                if not (300 <= dur <= 400):
                    continue
            else:
                dur = None
            if not str(e.get("form", "")).startswith("10-K"):
                continue
            rows.append(e)
        rows.sort(key=lambda e: (e.get("end", ""), e.get("filed", "")))
        print(f"  unit={u}   ({len(rows)} annual 10-K rows)")
        print(f"    {'start':<12}{'end':<12}{'dur':>5} {'fy':>6} {'fp':>4} {'form':<8}"
              f"{'filed':<12}{'frame':<14}{'val':>18}")
        for e in rows:
            if e.get("end", "9999") < "2019-01-01":
                continue
            dur = (d(e["end"]) - d(e["start"])).days if "start" in e else ""
            print(f"    {e.get('start',''):<12}{e['end']:<12}{str(dur):>5} "
                  f"{str(e.get('fy')):>6} {str(e.get('fp')):>4} {e.get('form',''):<8}"
                  f"{e.get('filed',''):<12}{str(e.get('frame','')):<14}{e['val']:>18,}")


data = {t: load(t) for t in PROBES}

# flow item, Dec FYE vs Jan/Feb FYE vs Jun FYE
dump(data["KO"], "KO", "us-gaap", "NetIncomeLoss", unit="USD")
dump(data["KR"], "KR", "us-gaap", "NetIncomeLoss", unit="USD")
dump(data["PG"], "PG", "us-gaap", "NetIncomeLoss", unit="USD")
# instant item
dump(data["KR"], "KR", "us-gaap", "AssetsCurrent", unit="USD")
# dei shares
dump(data["KR"], "KR", "dei", "EntityCommonStockSharesOutstanding")
