"""Step 0c/0d/0e: probe which XBRL tags actually exist in the KO / PG / KR
companyfacts files, and test the annual-value selection rule.
"""

import json
import os
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw_samples")

PROBES = {
    "KO": "KO_CIK0000021344_companyfacts.json",
    "PG": "PG_CIK0000080424_companyfacts.json",
    "KR": "KR_CIK0000056873_companyfacts.json",
}

YEARS = range(2020, 2026)

# field -> ordered candidate tags (taxonomy, tag)
CANDIDATES = {
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "SalesRevenueGoodsNet"),
    ],
    "cogs": [
        ("us-gaap", "CostOfGoodsAndServicesSold"),
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsSold"),
        ("us-gaap", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"),
    ],
    "sga": [
        ("us-gaap", "SellingGeneralAndAdministrativeExpense"),
        ("us-gaap", "GeneralAndAdministrativeExpense"),
        ("us-gaap", "SellingAndMarketingExpense"),
        ("us-gaap", "OperatingExpenses"),
    ],
    "advertising": [
        ("us-gaap", "AdvertisingExpense"),
        ("us-gaap", "MarketingAndAdvertisingExpense"),
        ("us-gaap", "AdvertisingCosts"),
    ],
    "rnd": [
        ("us-gaap", "ResearchAndDevelopmentExpense"),
        ("us-gaap", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
    ],
    "other_operating_expenses": [
        ("us-gaap", "OtherCostAndExpenseOperating"),
        ("us-gaap", "OtherOperatingIncomeExpenseNet"),
        ("us-gaap", "OtherNonoperatingIncomeExpense"),
    ],
    "depreciation_amortization": [
        ("us-gaap", "DepreciationDepletionAndAmortization"),
        ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
        ("us-gaap", "DepreciationAndAmortization"),
        ("us-gaap", "Depreciation"),
    ],
    "interest_expense": [
        ("us-gaap", "InterestExpense"),
        ("us-gaap", "InterestExpenseNonoperating"),
        ("us-gaap", "InterestIncomeExpenseNet"),
        ("us-gaap", "InterestAndDebtExpense"),
        ("us-gaap", "InterestExpenseDebt"),
    ],
    "income_tax_expense": [
        ("us-gaap", "IncomeTaxExpenseBenefit"),
        ("us-gaap", "CurrentIncomeTaxExpenseBenefit"),
    ],
    "net_income": [
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
    ],
    "eps": [
        ("us-gaap", "EarningsPerShareDiluted"),
        ("us-gaap", "EarningsPerShareBasic"),
        ("us-gaap", "EarningsPerShareBasicAndDiluted"),
    ],
    "accounts_receivable": [
        ("us-gaap", "AccountsReceivableNetCurrent"),
        ("us-gaap", "ReceivablesNetCurrent"),
        ("us-gaap", "AccountsAndOtherReceivablesNetCurrent"),
        ("us-gaap", "AccountsReceivableGrossCurrent"),
    ],
    "inventory": [
        ("us-gaap", "InventoryNet"),
        ("us-gaap", "InventoryFinishedGoods"),
        ("us-gaap", "FIFOInventoryAmount"),
    ],
    "accounts_payable": [
        ("us-gaap", "AccountsPayableCurrent"),
        ("us-gaap", "AccountsPayableTradeCurrent"),
        ("us-gaap", "AccountsPayableAndAccruedLiabilitiesCurrent"),
    ],
    "total_equity": [
        ("us-gaap", "StockholdersEquity"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ],
    "total_debt__components": [
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("us-gaap", "LongTermDebt"),
        ("us-gaap", "LongTermDebtCurrent"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
        ("us-gaap", "ShortTermBorrowings"),
        ("us-gaap", "OtherShortTermBorrowings"),
        ("us-gaap", "CommercialPaper"),
        ("us-gaap", "DebtCurrent"),
        ("us-gaap", "NotesPayableCurrent"),
    ],
    "current_assets": [
        ("us-gaap", "AssetsCurrent"),
    ],
    "current_liabilities": [
        ("us-gaap", "LiabilitiesCurrent"),
    ],
    "cash": [
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
        ("us-gaap", "CashAndDueFromBanks"),
    ],
    "shares_outstanding": [
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesIssued"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
    ],
    "capex": [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("us-gaap", "PaymentsToAcquireMachineryAndEquipment"),
    ],
    "free_cash_flow__components": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    ],
    "ebitda__components": [
        ("us-gaap", "OperatingIncomeLoss"),
        ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),
    ],
}


def load(t):
    with open(os.path.join(RAW, PROBES[t])) as fh:
        return json.load(fh)


def annual_facts(facts, taxo, tag):
    """Return {fy: [entries]} for annual 10-K facts, using the rule described
    in the report: no dimensional members (companyfacts is already
    consolidated), form 10-K/10-K/A, fp==FY, and either a ~365d duration
    (flows) or an instant (balance-sheet)."""
    node = facts.get("facts", {}).get(taxo, {}).get(tag)
    if not node:
        return None
    out = defaultdict(list)
    for unit, entries in node["units"].items():
        for e in entries:
            if not str(e.get("form", "")).startswith("10-K"):
                continue
            if e.get("fp") != "FY":
                continue
            if "start" in e:
                dur = (
                    _days(e["end"]) - _days(e["start"])
                )
                if not (300 <= dur <= 400):
                    continue
            out[e.get("fy")].append({**e, "unit": unit})
    return dict(out)


def _days(datestr):
    from datetime import date
    y, m, d = (int(x) for x in datestr.split("-"))
    return date(y, m, d).toordinal()


def main():
    data = {t: load(t) for t in PROBES}

    # ---- inventory of every tag each filer actually uses -------------------
    print("=" * 100)
    print("TAG AVAILABILITY  (Y = tag present with annual 10-K FY values; years listed = fy values in 2020-25)")
    print("=" * 100)
    for field, cands in CANDIDATES.items():
        cands = [c for c in cands if c]
        print(f"\n### {field}")
        for taxo, tag in cands:
            row = []
            for t in PROBES:
                af = annual_facts(data[t], taxo, tag)
                if af is None:
                    row.append(f"{t}: -")
                else:
                    yrs = sorted(y for y in af if y in YEARS)
                    row.append(f"{t}: {yrs if yrs else 'present/no FY20-25'}")
            if all(r.endswith(": -") for r in row):
                continue
            print(f"    {taxo}:{tag}")
            for r in row:
                print(f"        {r}")


if __name__ == "__main__":
    main()
