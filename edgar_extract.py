"""Extraction helper for SEC EDGAR companyfacts -> one annual value per field.

The whole module is pure: it takes an already-parsed companyfacts dict and
never touches the network.

Selection rules implemented here (all decided during Step 0 probing):

  filter      form startswith "10-K" and fp == "FY";
              flows  -> duration 300..400 days (52/53-week years drift 363-370)
              instants -> no `start`
  fiscal_year keyed off the period END date, never the `fy` field:
                  fy = end.year if end.month >= 6 else end.year - 1
              (`fy` is the fiscal year of the FILING; e.g. Kroger's FY2023 10-K
              is stamped fy=2024, so keying on `fy` silently loses a year.)
  dedup       chain-wide across the whole tag fallback list, by filing date:
              policy "earliest" -> as-originally-reported (default)
              policy "latest"   -> as-restated
  selector    "max"   revenue/cogs: take the largest candidate in the chosen
                      filing, because EDGAR mixes consolidated totals with
                      segment/disaggregation lines under sibling tags
                      (GIS tags a $2.0B segment line as `Revenues` alongside
                      an $18.1B total; PG FY2014 does the same). Fixed priority
                      cannot work in both directions.
              "first" everything else: first tag in chain order present.
"""

from __future__ import annotations

from datetime import date

# --- tunables ---------------------------------------------------------------
ANNUAL_MIN_DAYS = 300
ANNUAL_MAX_DAYS = 400
# filed - period_end beyond this means the "earliest" fact we hold is itself a
# later comparative, not the original filing (KHC FY2014-15, KVUE FY2021-23).
COMPARATIVE_LAG_DAYS = 200

POLICIES = ("earliest", "latest")

FLOW, INSTANT = "flow", "instant"
USD, SHARES, USD_PER_SHARE = "USD", "shares", "USD/shares"


def _d(s: str) -> date:
    y, m, dd = (int(x) for x in s.split("-"))
    return date(y, m, dd)


def fiscal_year_of(end: str) -> int:
    """Economic fiscal year for a period ending on `end`.

    Matches SEC's own `frame` assignment, verified across every fiscal-calendar
    in this universe: Jan/Feb/Apr/May ends -> prior year, Jun/Aug/Oct/Nov/Dec
    ends -> same year.
    """
    e = _d(end)
    return e.year if e.month >= 6 else e.year - 1


class FieldSpec:
    """How to pull one column out of companyfacts."""

    __slots__ = ("name", "kind", "unit", "chain", "selector")

    def __init__(self, name, kind, chain, selector="first", unit=USD):
        self.name = name
        self.kind = kind              # FLOW | INSTANT
        self.unit = unit              # USD | shares | USD/shares
        self.chain = chain            # [(taxonomy, tag), ...] in priority order
        self.selector = selector      # "first" | "max"


# ---------------------------------------------------------------------------
# fact iteration
# ---------------------------------------------------------------------------
def _candidates(cf: dict, spec: FieldSpec, fiscal_year: int) -> list[dict]:
    """Every qualifying annual 10-K fact for this field and fiscal year,
    across the entire tag chain."""
    out = []
    facts = cf.get("facts", {})
    for rank, (taxo, tag) in enumerate(spec.chain):
        node = facts.get(taxo, {}).get(tag)
        if not node:
            continue
        for unit, entries in node.get("units", {}).items():
            if unit != spec.unit:
                continue
            for e in entries:
                form = str(e.get("form", ""))
                if not form.startswith("10-K"):
                    continue
                if e.get("fp") != "FY":
                    continue
                has_start = "start" in e
                if spec.kind == FLOW:
                    if not has_start:
                        continue
                    dur = (_d(e["end"]) - _d(e["start"])).days
                    if not (ANNUAL_MIN_DAYS <= dur <= ANNUAL_MAX_DAYS):
                        continue
                elif has_start:
                    continue
                if fiscal_year_of(e["end"]) != fiscal_year:
                    continue
                out.append({
                    "taxo": taxo, "tag": tag, "rank": rank,
                    "val": e["val"], "end": e["end"],
                    "filed": e.get("filed", ""), "form": form,
                })
    return out


def _choose_filing(cands: list[dict], policy: str) -> tuple[str, str]:
    """Pick the (filed, form) pair the value should come from.

    'earliest' prefers the plain 10-K over a same-day 10-K/A (the amendment is
    already a revision); 'latest' prefers the amendment.
    """
    if policy == "earliest":
        key = lambda c: (c["filed"], 1 if c["form"].endswith("/A") else 0)
        best = min(cands, key=key)
    else:
        key = lambda c: (c["filed"], 0 if c["form"].endswith("/A") else -1)
        best = max(cands, key=key)
    return best["filed"], best["form"]


# Reported income-positive; the column they feed is expense-positive.
SIGN_FLIPPED_TAGS = {"us-gaap:InterestIncomeExpenseNonoperatingNet"}

_EXCL = "RevenueFromContractWithCustomerExcludingAssessedTax"
_INCL = "RevenueFromContractWithCustomerIncludingAssessedTax"


def _apply_selector(cands: list[dict], spec: FieldSpec) -> dict:
    if spec.selector == "max":
        pool = cands
        # Both assessed-tax variants present -> Excluding is the comparable one;
        # Including bundles sales taxes and would win a naive max.
        tags = {c["tag"] for c in pool}
        if _EXCL in tags and _INCL in tags:
            pool = [c for c in pool if c["tag"] != _INCL]
        return max(pool, key=lambda c: (c["val"], -c["rank"]))
    return min(cands, key=lambda c: (c["rank"], -c["val"]))


def extract_field(cf: dict, spec: FieldSpec, fiscal_year: int,
                  policy: str = "earliest") -> tuple[float | None, dict]:
    """Return (value_or_None, provenance).

    Never fabricates: if nothing qualifies the value is None and provenance
    records the miss.
    """
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}, got {policy!r}")

    prov = {"field": spec.name, "tag": None, "filed": None, "form": None,
            "period_end": None, "filing_lag_days": None, "selector": None}

    cands = _candidates(cf, spec, fiscal_year)
    if not cands:
        prov["selector"] = "missing"
        return None, prov

    # A fiscal year should map to exactly one period end; if a 53-week drift or
    # a fiscal-calendar change produced two, keep the later one.
    target_end = max(c["end"] for c in cands)
    cands = [c for c in cands if c["end"] == target_end]

    filed, form = _choose_filing(cands, policy)
    in_filing = [c for c in cands if c["filed"] == filed and c["form"] == form]

    win = _apply_selector(in_filing, spec)
    tag = f"{win['taxo']}:{win['tag']}"
    val = float(win["val"])
    selector = f"{spec.selector}:{win['tag']}"

    # Sign normalisation. InterestIncomeExpenseNonoperatingNet is signed
    # income-positive, while InterestExpense is expense-positive; without this
    # the interest_expense column flips sign mid-series (KR: +441 in FY2023,
    # -450 in FY2024 for a rising expense).
    if tag in SIGN_FLIPPED_TAGS:
        val = -val
        selector += "|sign_flipped"

    prov.update({
        "tag": tag, "filed": win["filed"], "form": win["form"],
        "period_end": win["end"],
        "filing_lag_days": (_d(win["filed"]) - _d(win["end"])).days if win["filed"] else None,
        "selector": selector,
    })
    return val, prov


# ---------------------------------------------------------------------------
# field specs -- 20 directly-tagged columns
# ---------------------------------------------------------------------------
G = "us-gaap"
D = "dei"

SPECS: dict[str, FieldSpec] = {s.name: s for s in [
    FieldSpec("revenue", FLOW, [
        (G, _EXCL), (G, _INCL), (G, "Revenues"),
        (G, "SalesRevenueNet"), (G, "SalesRevenueGoodsNet"),
    ], selector="max"),

    FieldSpec("cogs", FLOW, [
        (G, "CostOfGoodsAndServicesSold"),
        (G, "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"),
        (G, "CostOfGoodsSold"), (G, "CostOfRevenue"),
    ], selector="max"),

    # No plug-derivation: absent (Kroger et al. use a custom extension element,
    # which companyfacts strips) means NULL, logged to coverage.
    FieldSpec("sga", FLOW, [(G, "SellingGeneralAndAdministrativeExpense")]),

    FieldSpec("advertising", FLOW, [
        (G, "AdvertisingExpense"), (G, "MarketingAndAdvertisingExpense"),
        (G, "AdvertisingCosts"),
    ]),
    FieldSpec("rnd", FLOW, [
        (G, "ResearchAndDevelopmentExpense"),
        (G, "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
    ]),
    FieldSpec("other_operating_expenses", FLOW, [
        (G, "OtherCostAndExpenseOperating"), (G, "OtherOperatingIncomeExpenseNet"),
    ]),
    FieldSpec("depreciation_amortization", FLOW, [
        (G, "DepreciationDepletionAndAmortization"),
        (G, "DepreciationAmortizationAndAccretionNet"),
        (G, "DepreciationAndAmortization"), (G, "Depreciation"),
    ]),
    # FY2024 migration: FASB deprecated InterestExpense in favour of
    # InterestExpenseNonoperating; neither tag spans 2014-2025 alone. Kroger
    # migrated to InterestIncomeExpenseNonoperatingNet instead -- that one is a
    # NET figure (interest income already offset), so it is last resort and is
    # flagged in coverage when used.
    FieldSpec("interest_expense", FLOW, [
        (G, "InterestExpense"), (G, "InterestExpenseNonoperating"),
        (G, "InterestAndDebtExpense"), (G, "InterestIncomeExpenseNonoperatingNet"),
    ]),
    FieldSpec("income_tax_expense", FLOW, [(G, "IncomeTaxExpenseBenefit")]),
    # SYY and TGT tag net income as ...AvailableToCommonStockholdersBasic for
    # FY2014-2019 and only migrate to NetIncomeLoss at FY2020; without the third
    # tag those years are silently NULL. It is net of preferred dividends, so it
    # is last resort and flagged in coverage when used.
    FieldSpec("net_income", FLOW, [
        (G, "NetIncomeLoss"), (G, "ProfitLoss"),
        (G, "NetIncomeLossAvailableToCommonStockholdersBasic"),
    ]),
    FieldSpec("eps", FLOW, [
        (G, "EarningsPerShareDiluted"), (G, "EarningsPerShareBasicAndDiluted"),
        (G, "EarningsPerShareBasic"),
    ], unit=USD_PER_SHARE),

    FieldSpec("accounts_receivable", INSTANT, [
        (G, "AccountsReceivableNetCurrent"), (G, "ReceivablesNetCurrent"),
        (G, "AccountsAndOtherReceivablesNetCurrent"),
    ]),
    FieldSpec("inventory", INSTANT, [(G, "InventoryNet"), (G, "FIFOInventoryAmount")]),
    FieldSpec("accounts_payable", INSTANT, [
        (G, "AccountsPayableCurrent"), (G, "AccountsPayableTradeCurrent"),
    ]),
    # Including-NCI first: it is the only variant PG reports in-window.
    FieldSpec("total_equity", INSTANT, [
        (G, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        (G, "StockholdersEquity"),
    ]),
    FieldSpec("current_assets", INSTANT, [(G, "AssetsCurrent")]),
    FieldSpec("current_liabilities", INSTANT, [(G, "LiabilitiesCurrent")]),
    FieldSpec("cash", INSTANT, [
        (G, "CashAndCashEquivalentsAtCarryingValue"),
        (G, "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ]),
    # NOTE deviation from the agreed chain (Outstanding -> Issued -> dei):
    # Issued must come AFTER dei. CommonStockSharesOutstanding is absent for
    # Kroger, and CommonStockSharesIssued is a constant 1,918.0M every year --
    # issued shares including a large treasury block, ~2.9x the true count
    # (dei 660.9M / weighted-diluted 720.0M for FY2024). Issued is only a
    # sane proxy for filers with no material treasury stock, so it is the
    # last resort. See SHARES_LAST_RESORT_TAG.
    FieldSpec("shares_outstanding", INSTANT, [
        (G, "CommonStockSharesOutstanding"),
    ], unit=SHARES),
    FieldSpec("capex", FLOW, [
        (G, "PaymentsToAcquirePropertyPlantAndEquipment"),
        (G, "PaymentsToAcquireProductiveAssets"),
    ]),
]}

# components used only to derive ebitda / total_debt / free_cash_flow
COMPONENTS: dict[str, FieldSpec] = {s.name: s for s in [
    FieldSpec("_operating_income", FLOW, [(G, "OperatingIncomeLoss")]),
    FieldSpec("_cfo", FLOW, [
        (G, "NetCashProvidedByUsedInOperatingActivities"),
        (G, "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    ]),
    FieldSpec("_weighted_diluted_shares", FLOW, [
        (G, "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ], unit=SHARES),
    # long-term, non-current portion (FY2024 migration to the capital-lease tag)
    FieldSpec("_lt_noncurrent", INSTANT, [
        (G, "LongTermDebtNoncurrent"), (G, "LongTermDebtAndCapitalLeaseObligations"),
    ]),
    # current portion of long-term debt
    FieldSpec("_lt_current", INSTANT, [
        (G, "LongTermDebtCurrent"),
        (G, "LongTermDebtAndCapitalLeaseObligationsCurrent"),
    ]),
    # EDGAR's debt tags are NESTED aggregates, not siblings. Verified:
    #   PG: DebtCurrent == LongTermDebtCurrent + CommercialPaper
    #                      + OtherShortTermBorrowings   (exact, every year)
    #   KO: ShortTermBorrowings == CommercialPaper + OtherShortTermBorrowings
    # so they must be tried widest-first and never summed together.
    FieldSpec("_debt_current_all", INSTANT, [(G, "DebtCurrent")]),
    FieldSpec("_short_term_agg", INSTANT, [(G, "ShortTermBorrowings")]),
    FieldSpec("_commercial_paper", INSTANT, [(G, "CommercialPaper")]),
    FieldSpec("_other_st", INSTANT, [(G, "OtherShortTermBorrowings")]),
    # Finance/capital leases. These are a SEPARATE balance-sheet line from the
    # debt tags above, so they are added only when the debt tag used is
    # debt-ONLY. Adding them on top of a ...CapitalLeaseObligations tag would
    # double-count -- 67 ticker-years across 11 filers carry both.
    FieldSpec("_lease_noncurrent", INSTANT, [
        (G, "FinanceLeaseLiabilityNoncurrent"),
        (G, "CapitalLeaseObligationsNoncurrent"),
    ]),
    FieldSpec("_lease_current", INSTANT, [
        (G, "FinanceLeaseLiabilityCurrent"),
        (G, "CapitalLeaseObligationsCurrent"),
    ]),
    # Pre-ASC-842 filers bundle capital leases INTO the lease-inclusive debt tag
    # and never tag them separately (KR FY2014-17, KDP FY2014). For those the
    # lease amount is the difference between the two debt tags.
    FieldSpec("_lt_noncurrent_leaseincl", INSTANT, [
        (G, "LongTermDebtAndCapitalLeaseObligations"),
    ]),
    FieldSpec("_lt_current_leaseincl", INSTANT, [
        (G, "LongTermDebtAndCapitalLeaseObligationsCurrent"),
    ]),
    # last-resort share count; see SPECS["shares_outstanding"]
    FieldSpec("_shares_issued", INSTANT, [(G, "CommonStockSharesIssued")], unit=SHARES),
]}

SHARES_LAST_RESORT_TAG = "us-gaap:CommonStockSharesIssued"

# A debt tag carrying this substring already includes finance/capital leases.
LEASE_INCLUSIVE_MARK = "CapitalLeaseObligations"

# Net income net of preferred dividends rather than the headline figure.
NET_INCOME_TO_COMMON_TAG = "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic"

# Interest tags that are NET of interest income rather than gross expense.
NET_INTEREST_TAGS = {"us-gaap:InterestIncomeExpenseNonoperatingNet"}


# ---------------------------------------------------------------------------
# derived columns
# ---------------------------------------------------------------------------
def _blank(field: str, note: str = "missing") -> dict:
    return {"field": field, "tag": None, "filed": None, "form": None,
            "period_end": None, "filing_lag_days": None, "selector": note}


def _merge_prov(field: str, parts: list[dict], formula: str) -> dict:
    """Provenance for a derived value: the period is shared, but components may
    come from different filings -- report the latest, i.e. the date by which
    every input was public."""
    live = [p for p in parts if p.get("filed")]
    if not live:
        return _blank(field)
    latest = max(live, key=lambda p: p["filed"])
    return {
        "field": field, "tag": formula, "filed": latest["filed"],
        "form": latest["form"], "period_end": latest["period_end"],
        "filing_lag_days": latest["filing_lag_days"],
        "selector": "derived:" + formula,
    }


def derive_ebitda(cf, fiscal_year, policy="earliest"):
    """Operating income + D&A.

    Chosen over net_income + interest + tax + D&A because the latter sweeps in
    non-operating items -- for KO, equity income from bottlers is large enough
    to visibly inflate it.
    """
    oi, p1 = extract_field(cf, COMPONENTS["_operating_income"], fiscal_year, policy)
    da, p2 = extract_field(cf, SPECS["depreciation_amortization"], fiscal_year, policy)
    if oi is None or da is None:
        return None, _blank("ebitda")
    tags = f"{p1['selector'].split(':', 1)[1]}+{p2['selector'].split(':', 1)[1]}"
    return oi + da, _merge_prov("ebitda", [p1, p2], tags)


def _tagname(prov):
    return (prov.get("tag") or ":").split(":")[-1]


def _is_lease_inclusive(prov):
    return LEASE_INCLUSIVE_MARK in (prov.get("tag") or "")


def _lease_component(cf, fiscal_year, policy, side_prov, side_value,
                     lease_spec, bundled_spec):
    """Finance-lease liability to add to one side of total_debt.

    Returns (amount, provenance, tag_label) -- (0, None, None) when the side's
    debt tag is already lease-inclusive, or when the filer reports no leases.
    """
    if side_value is None or _is_lease_inclusive(side_prov):
        return 0.0, None, None
    v, p = extract_field(cf, COMPONENTS[lease_spec], fiscal_year, policy)
    if v is not None:
        return v, p, _tagname(p)
    # no separate tag: recover leases bundled inside the lease-inclusive tag
    inc, pi = extract_field(cf, COMPONENTS[bundled_spec], fiscal_year, policy)
    if inc is not None and inc > side_value:
        return inc - side_value, pi, f"{_tagname(pi)}~bundled"
    return 0.0, None, None


def derive_total_debt(cf, fiscal_year, policy="earliest"):
    """Interest-bearing debt INCLUDING finance/capital leases.

    Two independent hazards, both verified against filed balance sheets:

    1. EDGAR's debt tags are NESTED aggregates, not siblings, so the current
       portion takes the widest available one and never sums siblings:
         DebtCurrent  >  ShortTermBorrowings (+ current LTD)
                      >  CommercialPaper + OtherShortTermBorrowings (+ current LTD)
    2. Finance leases are a separate line. They are added ONLY where the debt
       tag used is debt-only; where the filer uses a ...CapitalLeaseObligations
       tag the leases are already inside it.

    Operating leases are deliberately excluded -- they are not debt.
    """
    ltnc, p_nc = extract_field(cf, COMPONENTS["_lt_noncurrent"], fiscal_year, policy)
    ltc, p_c = extract_field(cf, COMPONENTS["_lt_current"], fiscal_year, policy)
    dc, p_dc = extract_field(cf, COMPONENTS["_debt_current_all"], fiscal_year, policy)
    sta, p_st = extract_field(cf, COMPONENTS["_short_term_agg"], fiscal_year, policy)
    cp, p_cp = extract_field(cf, COMPONENTS["_commercial_paper"], fiscal_year, policy)
    ost, p_ost = extract_field(cf, COMPONENTS["_other_st"], fiscal_year, policy)

    parts, tags = [], []
    if ltnc is not None:
        parts.append(p_nc)
        tags.append(_tagname(p_nc))

    if dc is not None:
        current, cur_parts, cur_prov = dc, [p_dc], p_dc
        cur_tags = [_tagname(p_dc)]
    elif sta is not None:
        current = sta + (ltc or 0)
        cur_parts = [p_st] + ([p_c] if ltc is not None else [])
        cur_tags = [_tagname(p_st)] + ([_tagname(p_c)] if ltc is not None else [])
        cur_prov = p_c if ltc is not None else p_st
    else:
        bits = [(cp, p_cp), (ost, p_ost), (ltc, p_c)]
        bits = [(v, p) for v, p in bits if v is not None]
        current = sum(v for v, _ in bits) if bits else None
        cur_parts = [p for _, p in bits]
        cur_tags = [_tagname(p) for _, p in bits]
        cur_prov = p_c if ltc is not None else (bits[0][1] if bits else {})

    if ltnc is None and not current:
        return None, _blank("total_debt")

    total = (ltnc or 0) + (current or 0)
    parts += cur_parts
    tags += cur_tags

    lease_nc, p_lnc, t_lnc = _lease_component(
        cf, fiscal_year, policy, p_nc, ltnc,
        "_lease_noncurrent", "_lt_noncurrent_leaseincl")
    lease_cu, p_lcu, t_lcu = _lease_component(
        cf, fiscal_year, policy, cur_prov, current,
        "_lease_current", "_lt_current_leaseincl")

    for amt, prov, label in ((lease_nc, p_lnc, t_lnc), (lease_cu, p_lcu, t_lcu)):
        if amt and prov:
            total += amt
            parts.append(prov)
            tags.append(label)

    return total, _merge_prov("total_debt", parts, "+".join(tags))


def derive_free_cash_flow(cf, fiscal_year, policy="earliest"):
    """CFO - capex. EDGAR reports no FCF element, so this is derived and cannot
    be validated against the filing (cross-checked against yfinance instead)."""
    cfo, p1 = extract_field(cf, COMPONENTS["_cfo"], fiscal_year, policy)
    capex, p2 = extract_field(cf, SPECS["capex"], fiscal_year, policy)
    if cfo is None or capex is None:
        return None, _blank("free_cash_flow")
    tags = f"{p1['selector'].split(':', 1)[1]}-{p2['selector'].split(':', 1)[1]}"
    return cfo - capex, _merge_prov("free_cash_flow", [p1, p2], tags)


# ---------------------------------------------------------------------------
# shares outstanding: dei fallback needs cover-date remapping
# ---------------------------------------------------------------------------
def fiscal_period_ends(cf: dict) -> dict[int, str]:
    """fiscal_year -> period end, learned from the filer's own balance sheet."""
    ends: dict[int, str] = {}
    node = cf.get("facts", {}).get(G, {}).get("AssetsCurrent") \
        or cf.get("facts", {}).get(G, {}).get("Assets")
    if not node:
        return ends
    for unit, entries in node.get("units", {}).items():
        if unit != USD:
            continue
        for e in entries:
            if "start" in e or not str(e.get("form", "")).startswith("10-K"):
                continue
            fy = fiscal_year_of(e["end"])
            if fy not in ends or e["end"] > ends[fy]:
                ends[fy] = e["end"]
    return ends


def dei_shares_outstanding(cf, fiscal_year, fye_map, policy="earliest"):
    """dei:EntityCommonStockSharesOutstanding is measured at the 10-K COVER
    date, ~2 months after fiscal year end, so it must never be matched on the
    period-end date. Assign each cover-date fact to the fiscal year whose end
    most recently PRECEDES it.

    Absent entirely for multi-class filers (BF-B has only EntityPublicFloat in
    dei) -- the us-gaap chain covers those.
    """
    node = cf.get("facts", {}).get(D, {}).get("EntityCommonStockSharesOutstanding")
    fye = fye_map.get(fiscal_year)
    if not node or not fye:
        return None, _blank("shares_outstanding")

    nxt = fye_map.get(fiscal_year + 1)
    best = []
    for unit, entries in node.get("units", {}).items():
        if unit != SHARES:
            continue
        for e in entries:
            if "start" in e or not str(e.get("form", "")).startswith("10-K"):
                continue
            if e["end"] < fye:
                continue
            if nxt and e["end"] > nxt:
                continue
            best.append(e)
    if not best:
        return None, _blank("shares_outstanding")

    best.sort(key=lambda e: (e["end"], e.get("filed", "")))
    win = best[0] if policy == "earliest" else best[-1]
    return float(win["val"]), {
        "field": "shares_outstanding", "tag": f"{D}:EntityCommonStockSharesOutstanding",
        "filed": win.get("filed"), "form": win.get("form"), "period_end": fye,
        "filing_lag_days": (_d(win["filed"]) - _d(fye)).days if win.get("filed") else None,
        "selector": f"cover_date:{win['end']}",
    }


DERIVED = {"ebitda": derive_ebitda, "total_debt": derive_total_debt,
           "free_cash_flow": derive_free_cash_flow}

FUNDAMENTAL_FIELDS = [
    "revenue", "cogs", "sga", "advertising", "rnd", "other_operating_expenses",
    "depreciation_amortization", "interest_expense", "income_tax_expense",
    "net_income", "ebitda", "eps", "accounts_receivable", "inventory",
    "accounts_payable", "total_equity", "total_debt", "current_assets",
    "current_liabilities", "cash", "shares_outstanding", "capex",
    "free_cash_flow",
]
assert len(FUNDAMENTAL_FIELDS) == 23
