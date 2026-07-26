"""Step 0a/0b: resolve ticker -> CIK from SEC's company_tickers.json and pull
companyfacts for three probe companies (KO, PG, KR) into raw_samples/.

SEC requires a descriptive User-Agent on every request or it returns 403,
and caps clients at 10 requests/second.
"""

import json
import os
import time

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw_samples")

USER_AGENT = "staples-research/1.0 mohamed.elhaddad80@gmail.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

SLEEP = 0.15  # < 10 req/sec

PROBES = ["KO", "PG", "KR"]


def load_tickers():
    with open(os.path.join(BASE, "tickers.txt")) as fh:
        return [line.strip() for line in fh if line.strip()]


def fetch_ticker_index():
    resp = SESSION.get(COMPANY_TICKERS_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    return {row["ticker"].upper(): row for row in data.values()}


def candidates(ticker):
    """SEC writes class shares with a hyphen (BF-B), sometimes with no
    separator at all. Try the literal string first, then the variants."""
    t = ticker.upper()
    out = [t]
    if "." in t:
        out.append(t.replace(".", "-"))
        out.append(t.replace(".", ""))
    else:
        out.append(t.replace("-", "."))
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def resolve(tickers, index):
    resolved, unresolved = {}, []
    for t in tickers:
        hit = None
        for cand in candidates(t):
            if cand in index:
                hit = (cand, index[cand])
                break
        if hit is None:
            unresolved.append(t)
            continue
        sec_ticker, row = hit
        resolved[t] = {
            "sec_ticker": sec_ticker,
            "cik": str(row["cik_str"]).zfill(10),
            "title": row["title"],
        }
    return resolved, unresolved


def fetch_facts(cik, out_path):
    resp = SESSION.get(FACTS_URL.format(cik=cik), timeout=60)
    resp.raise_for_status()
    with open(out_path, "w") as fh:
        json.dump(resp.json(), fh)
    return os.path.getsize(out_path)


def main():
    os.makedirs(RAW, exist_ok=True)
    tickers = load_tickers()
    index = fetch_ticker_index()
    time.sleep(SLEEP)
    print(f"company_tickers.json entries: {len(index)}\n")

    resolved, unresolved = resolve(tickers, index)

    print(f"{'TICKER':<8} {'SEC':<8} {'CIK':<12} TITLE")
    print("-" * 78)
    for t in tickers:
        if t in resolved:
            r = resolved[t]
            note = "  <-- remapped" if r["sec_ticker"] != t else ""
            print(f"{t:<8} {r['sec_ticker']:<8} {r['cik']:<12} {r['title']}{note}")
        else:
            print(f"{t:<8} {'--':<8} {'UNRESOLVED':<12} !!! could not resolve")
    print("-" * 78)
    print(f"resolved {len(resolved)}/{len(tickers)}; unresolved: {unresolved or 'none'}\n")

    with open(os.path.join(BASE, "cik_map.json"), "w") as fh:
        json.dump(resolved, fh, indent=2)

    for t in PROBES:
        if t not in resolved:
            print(f"probe {t}: skipped, unresolved")
            continue
        cik = resolved[t]["cik"]
        path = os.path.join(RAW, f"{t}_CIK{cik}_companyfacts.json")
        size = fetch_facts(cik, path)
        print(f"probe {t}: CIK{cik} -> {path} ({size/1e6:.1f} MB)")
        time.sleep(SLEEP)


if __name__ == "__main__":
    main()
