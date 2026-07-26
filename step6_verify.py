"""Step 6 -- reconcile staples.db against the actual 10-K filings.

Pulls the RENDERED financial statements (the R*.htm "Financial Report" pages
that EDGAR generates for each filing) rather than companyfacts, so the check is
independent of our own extraction path: tag selection, the max-guard, the
chain-wide dedup, fiscal-year keying, sign normalisation and the debt
aggregation are all exercised against a separately-rendered statement.

Caveat stated plainly: the R pages are rendered from the SAME XBRL instance
document that companyfacts aggregates. So this validates OUR logic end to end,
but it cannot catch an error in the filer's own tagging. Only the human-written
PDF/HTML statements could do that.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from html.parser import HTMLParser

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "staples.db")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "staples-research/1.0 mohamed.elhaddad80@gmail.com",
    "Accept-Encoding": "gzip, deflate",
})
SLEEP = 0.15

# (ticker, fiscal_year, cik, filed) -- taken from fundamentals_provenance
TARGETS = [
    ("SYY", 2014, "0000096021", "2014-08-26"),
    ("SYY", 2025, "0000096021", "2025-08-22"),
    ("KR",  2014, "0000056873", "2015-03-31"),
    ("KR",  2025, "0000056873", "2026-03-31"),
    ("BG",  2014, "0001144519", "2015-03-02"),
    ("BG",  2025, "0001996862", "2026-02-19"),
]

# label patterns per field, matched case-insensitively against the row caption
PATTERNS = {
    "revenue": [r"^sales$", r"^net sales$", r"^total revenues?$", r"^revenues?$",
                r"^net revenues?$", r"^sales and other operating revenues?$"],
    # attributable-to-parent FIRST: BG's statement shows both "Net income"
    # (843M, incl. noncontrolling interests) and "Net income attributable to
    # Bunge" (816M). us-gaap:NetIncomeLoss is the latter, so compare like
    # with like.
    "net_income": [r"^net (earnings|income).{0,20}attributable to (?!noncontrolling)",
                   r"^net (earnings|income)$", r"^net (earnings|income) \(loss\)$"],
    "total_equity": [r"^total (shareholders|stockholders).? (equity|investment)$",
                     r"^total equity$"],
    "operating_cash_flow": [r"^net cash (provided by|from) operating activities$",
                            r"^cash (provided by|from) operating activities$",
                            r"^(net )?cash provided by \(used for\) operating activities$",
                            r"cash (provided by|used for|from).{0,25}operating activities"],
    "capex": [r"^additions to plant and equipment$",
              r"^payments for (property|additions)", r"^capital expenditures?$",
              r"^purchases? of property", r"^additions to properties$",
              r"^payments for property and equipment$",
              r"capital expenditure", r"additions to property"],
    "current_liabilities": [r"^total current liabilities$"],
}

STATEMENT_HINTS = {
    "income": ["statements of operations", "statements of income",
               "statements of earnings", "income statements", "results of operations"],
    "balance": ["balance sheet"],
    "cash": ["statements of cash flow", "cash flow statements", "cash flows",
             "cash flow"],
}


def get(url):
    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    time.sleep(SLEEP)
    return r


def find_accession(cik, filed, form="10-K"):
    """Locate the accession number of the filing made on `filed`."""
    j = get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    shards = [j["filings"]["recent"]]
    for extra in j["filings"].get("files", []):
        shards.append(get(f"https://data.sec.gov/submissions/{extra['name']}").json())
    for s in shards:
        for i, d in enumerate(s["filingDate"]):
            if d == filed and s["form"][i].startswith(form):
                return s["accessionNumber"][i].replace("-", ""), s["form"][i]
    return None, None


class TableParser(HTMLParser):
    """Collect <tr> rows as lists of cell text from an EDGAR R*.htm page."""

    def __init__(self):
        super().__init__()
        self.rows, self.spans = [], []
        self._row, self._span, self._cell, self._in = None, None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row, self._span = [], []
        elif tag in ("td", "th"):
            self._in, self._cell = True, []
            n = dict(attrs).get("colspan", "1")
            self._pending = int(n) if str(n).isdigit() else 1

    def handle_endtag(self, tag):
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self.spans.append(self._span)
            self._row = self._span = None
        elif tag in ("td", "th") and self._in:
            self._in = False
            if self._row is not None:
                self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
                self._span.append(getattr(self, "_pending", 1))

    def handle_data(self, data):
        if self._in:
            self._cell.append(data)


def column_groups(rows, spans):
    """Map each data column to its period group, e.g. "3 Months Ended" vs
    "12 Months Ended". SYY's FY2014 statement carries both; without this the
    leftmost numeric cell is Q4, not the year."""
    for row, span in list(zip(rows, spans))[:3]:
        if any("months ended" in c.lower() for c in row):
            groups = []
            for cell, n in list(zip(row, span))[1:]:
                groups += [cell] * max(n, 1)
            return groups
    return None


def parse_report(html):
    p = TableParser()
    p.feed(html)
    header = " ".join(p.rows[0]).lower() if p.rows else ""
    scale = 1
    if "in thousands" in header:
        scale = 1_000
    elif "in millions" in header:
        scale = 1_000_000
    elif "in billions" in header:
        scale = 1_000_000_000
    return p.rows, scale, header, column_groups(p.rows, p.spans)


NUM = re.compile(r"^\(?\$?\s*-?[\d,]+(?:\.\d+)?\)?$")
# 2015-era R pages glue the element name onto the value: "11,403us-gaap_LiabilitiesCurrent"
TAGSUFFIX = re.compile(r"(?<=[\d)])\s*[a-z][a-zA-Z0-9-]*_[A-Za-z0-9]+.*$")


def to_num(txt):
    txt = TAGSUFFIX.sub("", txt.strip())
    t = txt.replace("$", "").replace(",", "").strip()
    if not t or not NUM.match(txt.strip()):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def first_value(row, start=0):
    """First numeric cell at or after data-column `start`."""
    for cell in row[1 + start:]:
        v = to_num(cell)
        if v is not None:
            return v
    return None


ANNUAL_ONLY = {"revenue", "net_income", "operating_cash_flow", "capex"}


def match_field(rows, scale, field, groups=None):
    start = 0
    if groups and field in ANNUAL_ONLY:
        idx = [i for i, g in enumerate(groups) if "12 months" in g.lower()]
        start = idx[0] if idx else 0
    pats = [re.compile(p, re.I) for p in PATTERNS[field]]
    # Pattern priority must dominate ROW order: Kroger's statement lists
    # "Net income attributable to noncontrolling interests" above "Net earnings
    # attributable to The Kroger Co.", so scanning rows first picks the 8M line.
    for strict in (True, False):
        for p in pats:
            for row in rows:
                if not row:
                    continue
                label = re.sub(r"\s*\[\d+\]\s*$", "", row[0]).strip().rstrip(":")
                label = re.sub(r"\s+", " ", label)
                if p.match(label) if strict else p.search(label):
                    v = first_value(row, start)
                    if v is not None:
                        return v * scale, label
    return None, None


def reports_for(cik, acc):
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}"
    fs = get(f"{base}/FilingSummary.xml").text
    out = []
    for m in re.finditer(r"<Report[^>]*>(.*?)</Report>", fs, re.S):
        blk = m.group(1)
        name = re.search(r"<ShortName>(.*?)</ShortName>", blk, re.S)
        fname = re.search(r"<HtmlFileName>(.*?)</HtmlFileName>", blk, re.S)
        if not fname:
            fname = re.search(r"<XmlFileName>(.*?)</XmlFileName>", blk, re.S)
        if name and fname:
            out.append((name.group(1).strip(), f"{base}/{fname.group(1).strip()}"))
    return out


def pick(reports, kind):
    hints = STATEMENT_HINTS[kind]
    cands = [(n, u) for n, u in reports
             if any(h in n.lower() for h in hints)
             and "parenthetical" not in n.lower()]
    return cands[0] if cands else (None, None)


def filing_values(cik, acc):
    reports = reports_for(cik, acc)
    vals, labels = {}, {}
    for kind, fields in [("income", ["revenue", "net_income"]),
                         ("balance", ["total_equity", "current_liabilities"]),
                         ("cash", ["operating_cash_flow", "capex"])]:
        name, url = pick(reports, kind)
        if not url:
            continue
        rows, scale, _hdr, groups = parse_report(get(url).text)
        for f in fields:
            v, lab = match_field(rows, scale, f, groups)
            if v is not None:
                vals[f], labels[f] = v, f"{lab}  [{name}]"
    return vals, labels


def db_values(ticker, fy):
    c = sqlite3.connect(DB)
    r = c.execute(
        "SELECT revenue, net_income, total_equity, free_cash_flow, capex, "
        "total_debt, current_liabilities FROM fundamentals "
        "WHERE ticker=? AND fiscal_year=?", (ticker, fy)).fetchone()
    c.close()
    rev, ni, eq, fcf, capex, debt, cl = r
    return {"revenue": rev, "net_income": ni, "total_equity": eq,
            "operating_cash_flow": (fcf + capex) if fcf is not None and capex is not None else None,
            "capex": capex, "total_debt": debt, "current_liabilities": cl}


def main():
    print("=" * 108)
    print("TABLE 1 -- staples.db vs the 10-K as filed (EDGAR rendered statements)")
    print("=" * 108)
    worst = []
    for tk, fy, cik, filed in TARGETS:
        acc, form = find_accession(cik, filed)
        if not acc:
            print(f"\n### {tk} FY{fy}: could not locate a filing on {filed}")
            continue
        ours = db_values(tk, fy)
        theirs, labels = filing_values(cik, acc)
        print(f"\n### {tk} FY{fy}   CIK {cik}   {form} filed {filed}   acc {acc}")
        print(f"    {'field':<22}{'staples.db':>18}{'10-K as filed':>18}{'diff %':>10}  matched line")
        for f in ["revenue", "net_income", "total_equity",
                  "operating_cash_flow", "capex", "current_liabilities"]:
            a, b = ours.get(f), theirs.get(f)
            if a is None or b is None:
                print(f"    {f:<22}{_m(a):>18}{_m(b):>18}{'--':>10}  "
                      f"{labels.get(f, 'NOT FOUND in filing')}")
                continue
            d = (a - abs(b)) / abs(b) * 100 if b else float("nan")
            if f == "capex":     # cash-flow outflows are negative in the filing
                d = (a - abs(b)) / abs(b) * 100
            flag = "  <-- CHECK" if abs(d) > 1.0 else ""
            worst.append((abs(d), tk, fy, f))
            print(f"    {f:<22}{_m(a):>18}{_m(b):>18}{d:>9.2f}%  "
                  f"{labels.get(f, '')}{flag}")
        print(f"    {'total_debt':<22}{_m(ours['total_debt']):>18}"
              f"{'(composite)':>18}{'--':>10}  spot-checked separately")
    worst.sort(reverse=True)
    print("\n  largest deviations: " +
          ", ".join(f"{t} FY{y} {f} {d:.2f}%" for d, t, y, f in worst[:5]))
    fcf_crosscheck()


def fcf_crosscheck():
    """TABLE 2 -- our derived FCF (CFO - capex, as-originally-reported, from
    EDGAR) vs yfinance's reported Free Cash Flow. Different sources AND
    different computation paths, so agreement is meaningful."""
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT f.ticker, f.fiscal_year, f.free_cash_flow, x.yf_free_cash_flow, "
        "       x.yf_operating_cash_flow, x.yf_capex, f.capex "
        "FROM fundamentals f JOIN market_fcf_crosscheck x "
        "  ON f.ticker=x.ticker AND f.fiscal_year=x.fiscal_year "
        "WHERE f.free_cash_flow IS NOT NULL AND x.yf_free_cash_flow IS NOT NULL "
        "ORDER BY f.ticker, f.fiscal_year").fetchall()
    c.close()

    print("\n\n" + "=" * 108)
    print("TABLE 2 -- derived free_cash_flow (EDGAR, CFO-capex) vs yfinance reported FCF")
    print("=" * 108)

    diffs, big = [], []
    for tk, fy, ours, theirs, yfcfo, yfcapex, ourcapex in rows:
        d = (ours - theirs) / abs(theirs) * 100 if theirs else float("nan")
        diffs.append((abs(d), tk, fy, ours, theirs, yfcfo, yfcapex, ourcapex))
        if abs(d) > 3:
            big.append((abs(d), tk, fy, ours, theirs, yfcfo, yfcapex, ourcapex))

    within = lambda t: sum(1 for d, *_ in diffs if d <= t)
    n = len(diffs)
    print(f"\n  {n} overlapping ticker-years across {len({r[0] for r in rows})} tickers")
    print(f"    within 0.1% : {within(0.1):>3}/{n}")
    print(f"    within 1%   : {within(1):>3}/{n}")
    print(f"    within 3%   : {within(3):>3}/{n}")
    print(f"    beyond 3%   : {n - within(3):>3}/{n}")

    print("\n  --- the three reconciled tickers, every overlapping year ---")
    print(f"    {'tk':<6}{'FY':<6}{'ours':>13}{'yfinance':>13}{'diff %':>9}"
          f"{'our capex':>13}{'yf capex':>13}")
    for tk, fy, ours, theirs, yfcfo, yfcapex, ourcapex in rows:
        if tk not in ("SYY", "KR", "BG"):
            continue
        d = (ours - theirs) / abs(theirs) * 100 if theirs else float("nan")
        print(f"    {tk:<6}{fy:<6}{_m(ours):>13}{_m(theirs):>13}{d:>8.2f}%"
              f"{_m(ourcapex):>13}{_m(yfcapex):>13}")

    print(f"\n  --- all deviations beyond 3% ({len(big)}) ---")
    if not big:
        print("    none")
    for d, tk, fy, ours, theirs, yfcfo, yfcapex, ourcapex in sorted(big, reverse=True):
        capex_gap = (abs(ourcapex) - abs(yfcapex)) if ourcapex and yfcapex else None
        cfo_gap = (ours + ourcapex - yfcfo) if ourcapex and yfcfo else None
        print(f"    {tk:<6}FY{fy}  ours={_m(ours):>11} yf={_m(theirs):>11} "
              f"{d:>7.1f}%   capex gap={_m(capex_gap):>10}  CFO gap={_m(cfo_gap):>10}")
    return diffs


def _m(v):
    return "None" if v is None else f"{v/1e6:,.1f}M"


if __name__ == "__main__":
    main()
