"""Unit tests for edgar_extract -- NO NETWORK. Runs against the companyfacts
files saved under raw_samples/ during Step 0 probing.

    .venv/bin/python -m unittest test_extract -v
"""

import json
import os
import re
import unittest

import edgar_extract as ex

RAW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_samples")
M = 1_000_000


# Pin every fixture to an explicit CIK. The 34-ticker loop caches BG under
# BOTH of its CIKs, so a prefix-only match would silently load Bunge Limited
# (predecessor) where the successor is meant.
FIXTURES = {
    "KO": "0000021344", "PG": "0000080424", "KR": "0000056873",
    "BG": "0001996862",        # Bunge Global SA -- successor
    "BGPRED": "0001144519",    # Bunge Limited   -- predecessor
    "BF-B": "0000014693", "GIS": "0000040704", "COST": "0000909832",
    "KVUE": "0001944048", "KHC": "0001637459", "KDP": "0001418135",
    "SYY": "0000096021", "PM": "0001413329",
}


def load(prefix, cik=None):
    cik = cik or FIXTURES[prefix]
    path = os.path.join(RAW, f"{prefix}_CIK{cik}_companyfacts.json")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path) as fh:
        return json.load(fh)


CF = {t: load(t) for t in FIXTURES}


def rev(tk, fy, policy="earliest"):
    return ex.extract_field(CF[tk], ex.SPECS["revenue"], fy, policy)


class FiscalYearRule(unittest.TestCase):
    """fiscal_year keys off the period end, matching SEC's own frame."""

    def test_matches_sec_frames(self):
        cases = [
            ("2020-12-31", 2020),   # KO   Dec  -> CY2020
            ("2021-01-30", 2020),   # KR   Jan  -> CY2020
            ("2024-02-03", 2023),   # KR   Feb  -> CY2023  (53-week year)
            ("2020-04-30", 2019),   # BF-B Apr  -> CY2019
            ("2020-05-31", 2019),   # GIS May  -> CY2019
            ("2020-06-30", 2020),   # PG   Jun  -> CY2020
            ("2020-08-30", 2020),   # COST Aug  -> CY2020
            ("2021-01-03", 2020),   # KVUE 53-week year crossing new year
        ]
        for end, want in cases:
            self.assertEqual(ex.fiscal_year_of(end), want, end)


class MaxGuard(unittest.TestCase):
    """A fixed tag priority breaks in BOTH directions; max-across-chain is the
    only selector that survives all three shapes."""

    def test_gis_segment_line_does_not_win(self):
        # GIS tags a ~$2.0B segment line as `Revenues` in the same filing as the
        # $18.1B consolidated total. Priority-by-`Revenues` would take the 2.0B.
        v, p = rev("GIS", 2020)
        self.assertAlmostEqual(v, 18_127.0 * M, delta=M)
        self.assertIn("RevenueFromContractWithCustomerExcludingAssessedTax", p["tag"])

    def test_bg_reverse_direction(self):
        # BG is the mirror image: `Revenues` (70.3B) is the true total and
        # RevenueFromContract (16.9B) is the disaggregation.
        v, p = rev("BG", 2025)
        self.assertAlmostEqual(v, 70_329.0 * M, delta=M)
        self.assertIn("Revenues", p["tag"])

    def test_pg_2014_partial_revenues_tag(self):
        # PG's ORIGINAL FY2014 10-K carries both SalesRevenueNet 83,062M and a
        # `Revenues` 29,400M partial. The real top line must win.
        v, p = rev("PG", 2014)
        self.assertAlmostEqual(v, 83_062.0 * M, delta=M)
        self.assertIn("SalesRevenueNet", p["tag"])

    def test_cogs_max_guard_records_tag(self):
        # As originally reported in the FY2015 10-K (filed 2015-08-07):
        # net sales 76,279M, cost of products sold 38,876M. The restated
        # figures (37,056M on 70,749M, post-Beauty-divestiture) belong to
        # policy="latest" -- see RestatementPolicy below.
        v, p = ex.extract_field(CF["PG"], ex.SPECS["cogs"], 2015)
        self.assertAlmostEqual(v, 38_876.0 * M, delta=M)
        self.assertTrue(p["selector"].startswith("max:"))

    def test_revenue_and_cogs_come_from_the_same_filing(self):
        # Margins are only meaningful if numerator and denominator are on the
        # same basis; a per-field policy drift would silently mix restated
        # revenue with original COGS.
        for tk, fy in [("PG", 2015), ("KO", 2018), ("KR", 2017)]:
            _, pr = ex.extract_field(CF[tk], ex.SPECS["revenue"], fy)
            _, pc = ex.extract_field(CF[tk], ex.SPECS["cogs"], fy)
            self.assertEqual(pr["filed"], pc["filed"], f"{tk} FY{fy}")


class DeprecatedTagEra(unittest.TestCase):
    """FY2014-2017 resolves only under pre-ASC-606 tags."""

    def test_ko_2015_uses_sales_revenue_goods_net(self):
        v, p = rev("KO", 2015)
        self.assertAlmostEqual(v, 44_294.0 * M, delta=M)
        self.assertEqual(p["tag"], "us-gaap:SalesRevenueGoodsNet")

    def test_kr_2015_early_era(self):
        v, p = rev("KR", 2015)
        self.assertAlmostEqual(v, 109_830.0 * M, delta=M)
        self.assertEqual(p["tag"], "us-gaap:SalesRevenueGoodsNet")

    def test_cogs_era_switch(self):
        early, pe = ex.extract_field(CF["KO"], ex.SPECS["cogs"], 2015)
        late, pl = ex.extract_field(CF["KO"], ex.SPECS["cogs"], 2024)
        self.assertEqual(pe["tag"], "us-gaap:CostOfGoodsSold")
        self.assertEqual(pl["tag"], "us-gaap:CostOfGoodsAndServicesSold")
        self.assertAlmostEqual(early, 17_482.0 * M, delta=M)
        self.assertAlmostEqual(late, 18_324.0 * M, delta=M)


class FyFieldTrap(unittest.TestCase):
    """Kroger's FY2023 10-K is stamped fy=2024, so keying on `fy` loses the
    year entirely. Keying on period end must recover it."""

    def test_kr_2023_resolves(self):
        v, p = rev("KR", 2023)
        self.assertIsNotNone(v, "KR FY2023 vanished -- the `fy` trap is back")
        self.assertEqual(p["period_end"], "2024-02-03")
        self.assertAlmostEqual(v, 150_039.0 * M, delta=M)

    def test_kr_all_years_present(self):
        missing = [fy for fy in range(2014, 2026) if rev("KR", fy)[0] is None]
        self.assertEqual(missing, [])


class RestatementPolicy(unittest.TestCase):
    """earliest == as-originally-reported, latest == as-restated."""

    def test_pg_2014_original_vs_restated(self):
        orig, po = rev("PG", 2014, "earliest")
        rest, pr = rev("PG", 2014, "latest")
        self.assertAlmostEqual(orig, 83_062.0 * M, delta=M)
        self.assertAlmostEqual(rest, 74_401.0 * M, delta=M)
        self.assertLess(po["filed"], pr["filed"])

    def test_chain_wide_not_per_tag(self):
        # KO FY2017's original value lives under SalesRevenueGoodsNet (filed
        # 2018-02-23); the earliest `Revenues` fact is a 2019 comparative.
        # Per-tag "earliest" would silently return the comparative.
        v, p = rev("KO", 2017, "earliest")
        self.assertEqual(p["filed"], "2018-02-23")
        self.assertEqual(p["tag"], "us-gaap:SalesRevenueGoodsNet")

    def test_ko_2018_current_assets_restatement(self):
        s = ex.SPECS["current_assets"]
        orig, _ = ex.extract_field(CF["KO"], s, 2018, "earliest")
        rest, _ = ex.extract_field(CF["KO"], s, 2018, "latest")
        self.assertAlmostEqual(orig, 30_634.0 * M, delta=M)
        self.assertAlmostEqual(rest, 24_930.0 * M, delta=M)

    def test_bad_policy_rejected(self):
        with self.assertRaises(ValueError):
            rev("KO", 2020, "whatever")


class ComparativeLagFlag(unittest.TestCase):
    """Where even the earliest fact is a later comparative, say so."""

    def test_kvue_carveouts_flagged(self):
        _, p = rev("KVUE", 2021)
        self.assertGreater(p["filing_lag_days"], ex.COMPARATIVE_LAG_DAYS)

    def test_normal_year_not_flagged(self):
        _, p = rev("KO", 2020)
        self.assertLess(p["filing_lag_days"], ex.COMPARATIVE_LAG_DAYS)


class TotalDebt(unittest.TestCase):
    """EDGAR's debt tags are nested aggregates; summing siblings double-counts."""

    def test_pg_uses_debtcurrent_without_double_count(self):
        # PG: DebtCurrent(10,229) == LongTermDebtCurrent + CP + OtherST exactly.
        # Naive "LTnoncurrent + LTcurrent + one-of{...DebtCurrent}" would add
        # 3,951 twice and land at 38,558 instead of 34,607.
        v, p = ex.derive_total_debt(CF["PG"], 2023)
        self.assertAlmostEqual(v, 34_607.0 * M, delta=2 * M)
        self.assertIn("DebtCurrent", p["tag"])
        self.assertNotIn("LongTermDebtCurrent", p["tag"])

    def test_ko_short_term_aggregate(self):
        # KO FY2020: ShortTermBorrowings(2,183) == CP(1,329)+OtherST(854).
        # Use the aggregate, add current LTD(485). 40,125+2,183+485 = 42,793.
        v, p = ex.derive_total_debt(CF["KO"], 2020)
        self.assertAlmostEqual(v, 42_793.0 * M, delta=2 * M)
        self.assertIn("ShortTermBorrowings", p["tag"])
        self.assertNotIn("CommercialPaper", p["tag"])

    def test_ko_components_when_no_aggregate(self):
        # FY2023 has neither DebtCurrent nor ShortTermBorrowings:
        # 35,547 + 1,960 + 4,209 + 348 = 42,064
        v, _ = ex.derive_total_debt(CF["KO"], 2023)
        self.assertAlmostEqual(v, 42_064.0 * M, delta=2 * M)

    def test_ko_2024_capital_lease_migration(self):
        # LongTermDebtNoncurrent stops after FY2023; the capital-lease tag
        # takes over. Without the migration this returns None.
        v, p = ex.derive_total_debt(CF["KO"], 2024)
        self.assertIsNotNone(v)
        self.assertIn("LongTermDebtAndCapitalLeaseObligations", p["tag"])

    def test_kr_debt_components_tie_to_reported_longtermdebt(self):
        # KR FY2020: noncurrent 11,566 + current 844 == LongTermDebt 12,410.
        # total_debt is now lease-INCLUSIVE, so the total sits above that by
        # exactly the finance-lease liability (936 + 67 = 1,003).
        nc, _ = ex.extract_field(CF["KR"], ex.COMPONENTS["_lt_noncurrent"], 2020)
        cu, _ = ex.extract_field(CF["KR"], ex.COMPONENTS["_lt_current"], 2020)
        self.assertAlmostEqual(nc + cu, 12_410.0 * M, delta=2 * M)

        lnc, _ = ex.extract_field(CF["KR"], ex.COMPONENTS["_lease_noncurrent"], 2020)
        lcu, _ = ex.extract_field(CF["KR"], ex.COMPONENTS["_lease_current"], 2020)
        v, _ = ex.derive_total_debt(CF["KR"], 2020)
        self.assertAlmostEqual(v, nc + cu + lnc + lcu, delta=2 * M)
        self.assertAlmostEqual(v, 13_413.0 * M, delta=2 * M)


class Derivations(unittest.TestCase):

    def test_ebitda_is_operating_income_plus_da(self):
        oi, _ = ex.extract_field(CF["KO"], ex.COMPONENTS["_operating_income"], 2022)
        da, _ = ex.extract_field(CF["KO"], ex.SPECS["depreciation_amortization"], 2022)
        v, p = ex.derive_ebitda(CF["KO"], 2022)
        self.assertAlmostEqual(v, oi + da, delta=1)
        self.assertTrue(p["selector"].startswith("derived:"))

    def test_fcf_is_cfo_minus_capex(self):
        cfo, _ = ex.extract_field(CF["KO"], ex.COMPONENTS["_cfo"], 2022)
        cap, _ = ex.extract_field(CF["KO"], ex.SPECS["capex"], 2022)
        v, _ = ex.derive_free_cash_flow(CF["KO"], 2022)
        self.assertAlmostEqual(v, cfo - cap, delta=1)

    def test_kr_capex_uses_productive_assets(self):
        v, p = ex.extract_field(CF["KR"], ex.SPECS["capex"], 2024)
        self.assertIsNotNone(v)
        self.assertEqual(p["tag"], "us-gaap:PaymentsToAcquireProductiveAssets")

    def test_derived_returns_none_not_zero(self):
        v, p = ex.derive_ebitda(CF["KVUE"], 2015)
        self.assertIsNone(v)
        self.assertIsNone(p["tag"])


class SharesOutstanding(unittest.TestCase):

    def test_bf_b_multiclass_uses_usgaap(self):
        # BF-B's dei section holds only EntityPublicFloat -- the share count is
        # class-dimensioned and stripped by companyfacts.
        self.assertNotIn("EntityCommonStockSharesOutstanding",
                         CF["BF-B"]["facts"].get("dei", {}))
        v, p = ex.extract_field(CF["BF-B"], ex.SPECS["shares_outstanding"], 2024)
        self.assertAlmostEqual(v, 472_669_000, delta=1000)
        self.assertEqual(p["tag"], "us-gaap:CommonStockSharesOutstanding")

    def test_dei_maps_to_preceding_fye_not_period_end(self):
        fye = ex.fiscal_period_ends(CF["KR"])
        v, p = ex.dei_shares_outstanding(CF["KR"], 2024, fye)
        self.assertIsNotNone(v)
        # FY2024 ends 2025-02-01; the cover date is ~2 months later.
        self.assertEqual(p["period_end"], "2025-02-01")
        self.assertGreater(p["selector"], "cover_date:2025-02-01")

    def test_weighted_diluted_available_for_crosscheck(self):
        wd, _ = ex.extract_field(CF["KO"], ex.COMPONENTS["_weighted_diluted_shares"], 2022)
        ni, _ = ex.extract_field(CF["KO"], ex.SPECS["net_income"], 2022)
        eps, _ = ex.extract_field(CF["KO"], ex.SPECS["eps"], 2022)
        self.assertAlmostEqual(ni / wd, eps, delta=0.05)


class NoFabrication(unittest.TestCase):

    def test_kr_sga_is_none_never_plugged(self):
        # Kroger tags its opex line with a custom extension element, which
        # companyfacts strips. Must stay NULL, never a residual.
        for fy in range(2014, 2026):
            v, p = ex.extract_field(CF["KR"], ex.SPECS["sga"], fy)
            self.assertIsNone(v, f"KR sga fabricated for FY{fy}")
            self.assertEqual(p["selector"], "missing")

    def test_ko_rnd_absent(self):
        v, _ = ex.extract_field(CF["KO"], ex.SPECS["rnd"], 2022)
        self.assertIsNone(v)

    def test_missing_year_returns_none(self):
        v, p = rev("KVUE", 2015)
        self.assertIsNone(v)
        self.assertEqual(p["selector"], "missing")


class BungeMerge(unittest.TestCase):
    """BG re-domesticated in 2023 and took a new CIK; FY2014-2022 lives under
    the predecessor."""

    def test_fixtures_are_the_entities_we_think(self):
        self.assertEqual(CF["BG"]["entityName"].upper(), "BUNGE GLOBAL SA")
        self.assertEqual(CF["BGPRED"]["entityName"].upper(), "BUNGELTD")

    def test_successor_lacks_early_years(self):
        self.assertIsNone(rev("BG", 2020)[0])

    def test_predecessor_has_them(self):
        v, _ = rev("BGPRED", 2020)
        self.assertIsNotNone(v)
        self.assertGreater(v, 40_000 * M)

    def test_merge_prefers_successor_on_overlap(self):
        from edgar_fetch import merge_entities
        merged = merge_entities([("0001996862", CF["BG"]), ("0001144519", CF["BGPRED"])])
        v2020, cik2020 = merged(ex.SPECS["revenue"], 2020)
        v2025, cik2025 = merged(ex.SPECS["revenue"], 2025)
        self.assertEqual(cik2020, "0001144519")   # predecessor fills the gap
        self.assertEqual(cik2025, "0001996862")   # successor wins where present
        self.assertIsNotNone(v2020)


class UnitHygiene(unittest.TestCase):

    def test_eps_is_per_share_not_dollars(self):
        v, _ = ex.extract_field(CF["KO"], ex.SPECS["eps"], 2022)
        self.assertLess(v, 100)

    def test_shares_is_a_count(self):
        # KO has no CommonStockSharesOutstanding; resolution goes through the
        # fetch-layer ladder, so exercise it there.
        from edgar_fetch import parse_entities
        rows, _ = parse_entities("KO", [("0000021344", CF["KO"])])
        v = {r["fiscal_year"]: r["shares_outstanding"] for r in rows}[2022]
        self.assertGreater(v, 1e9)

    def test_revenue_is_raw_dollars(self):
        v, _ = rev("KO", 2024)
        self.assertGreater(v, 1e10)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SharesFallbackOrder(unittest.TestCase):
    """CommonStockSharesIssued must never outrank dei -- it is issued-incl-
    treasury and overstates buyback-heavy filers by ~3x."""

    def test_kr_issued_is_a_constant_trap(self):
        vals = {fy: ex.extract_field(CF["KR"], ex.COMPONENTS["_shares_issued"], fy)[0]
                for fy in range(2016, 2026)}
        self.assertEqual(len(set(vals.values())), 1, "expected the flat 1,918M trap")

    def test_kr_resolves_to_dei_not_issued(self):
        from edgar_fetch import parse_entities
        rows, provs = parse_entities("KR", [("0000056873", CF["KR"])])
        by_fy = {r["fiscal_year"]: r["shares_outstanding"] for r in rows}
        self.assertLess(by_fy[2024], 800e6)
        self.assertGreater(by_fy[2024], 500e6)
        # and it must actually decline over the buyback era
        self.assertLess(by_fy[2025], by_fy[2016])

    def test_shares_track_ni_over_eps_except_flagged_split_years(self):
        from edgar_fetch import parse_entities, coverage_notes, SHARE_CONSISTENCY_TOL
        rows, provs = parse_entities("KR", [("0000056873", CF["KR"])])
        notes = coverage_notes("KR", rows, provs)
        flagged = {int(m) for n in notes
                   for m in re.findall(r"FY(\d{4}): shares_outstanding", n)}
        for r in rows:
            if not (r["shares_outstanding"] and r["net_income"] and r["eps"]):
                continue
            gap = abs(r["shares_outstanding"] - r["net_income"] / r["eps"]) \
                / abs(r["net_income"] / r["eps"])
            if r["fiscal_year"] in flagged:
                self.assertGreater(gap, SHARE_CONSISTENCY_TOL)
            else:
                self.assertLess(gap, SHARE_CONSISTENCY_TOL,
                                f"FY{r['fiscal_year']} off vs ni/eps and NOT flagged")

    def test_split_basis_mismatch_is_surfaced(self):
        # KR FY2014: cover-date count 953.7M vs ni/eps 502.3M (1.9x = the
        # 2-for-1 split). Must be reported, never emitted silently.
        from edgar_fetch import parse_entities, coverage_notes
        rows, provs = parse_entities("KR", [("0000056873", CF["KR"])])
        notes = " ".join(coverage_notes("KR", rows, provs))
        self.assertIn("FY2014: shares_outstanding", notes)
        self.assertIn("split", notes)


class InterestMigration(unittest.TestCase):

    def test_kr_2024_interest_resolves(self):
        v, p = ex.extract_field(CF["KR"], ex.SPECS["interest_expense"], 2024)
        self.assertIsNotNone(v, "KR migrated to InterestIncomeExpenseNonoperatingNet")
        self.assertIn(p["tag"], ex.NET_INTEREST_TAGS)

    def test_gross_tag_still_preferred_when_present(self):
        _, p = ex.extract_field(CF["KR"], ex.SPECS["interest_expense"], 2022)
        self.assertEqual(p["tag"], "us-gaap:InterestExpense")


class SignConventions(unittest.TestCase):
    """interest_expense must be expense-positive for every year, even where the
    filer migrated to a net, income-positive element."""

    def test_kr_interest_never_flips_sign(self):
        vals = [ex.extract_field(CF["KR"], ex.SPECS["interest_expense"], fy)[0]
                for fy in range(2014, 2026)]
        vals = [v for v in vals if v is not None]
        self.assertEqual(len(vals), 12)
        self.assertTrue(all(v > 0 for v in vals), vals)

    def test_flip_is_recorded_in_provenance(self):
        _, p = ex.extract_field(CF["KR"], ex.SPECS["interest_expense"], 2024)
        self.assertIn("sign_flipped", p["selector"])

    def test_series_is_continuous_across_the_migration(self):
        a, _ = ex.extract_field(CF["KR"], ex.SPECS["interest_expense"], 2023)
        b, _ = ex.extract_field(CF["KR"], ex.SPECS["interest_expense"], 2024)
        self.assertLess(abs(b - a) / a, 0.5, "discontinuity at the tag migration")


class CoverageNotesRobustness(unittest.TestCase):
    """coverage_notes must survive gaps -- a missing year must not crash the
    tag-break detector (this took SYY out of the first full run)."""

    def test_survives_missing_cogs_years(self):
        from edgar_fetch import coverage_notes, parse_entities
        for tk in ["KVUE", "KHC", "KDP", "KR", "BF-B", "SYY"]:
            rows, provs = parse_entities(tk, [("x", CF[tk])])
            coverage_notes(tk, rows, provs)   # must not raise

    def test_break_detector_ignores_none_tags(self):
        from edgar_fetch import coverage_notes
        rows = [{"fiscal_year": y, **{f: None for f in ex.FUNDAMENTAL_FIELDS}}
                for y in (2014, 2015, 2016)]
        provs = [{"fiscal_year": 2014, "field": "cogs", "tag": "us-gaap:A"},
                 {"fiscal_year": 2015, "field": "cogs", "tag": None},
                 {"fiscal_year": 2016, "field": "cogs", "tag": "us-gaap:A"}]
        notes = coverage_notes("X", rows, provs)
        self.assertFalse([n for n in notes if "cogs definition" in n])


class NetIncomeTagMigration(unittest.TestCase):
    """SYY/TGT only migrate to NetIncomeLoss at FY2020; the earlier years live
    under NetIncomeLossAvailableToCommonStockholdersBasic."""

    def test_syy_early_years_resolve(self):
        for fy in range(2014, 2020):
            v, p = ex.extract_field(CF["SYY"], ex.SPECS["net_income"], fy)
            self.assertIsNotNone(v, f"SYY FY{fy} net_income vanished")
            self.assertEqual(p["tag"], ex.NET_INCOME_TO_COMMON_TAG)

    def test_headline_tag_still_wins_when_present(self):
        _, p = ex.extract_field(CF["SYY"], ex.SPECS["net_income"], 2025)
        self.assertEqual(p["tag"], "us-gaap:NetIncomeLoss")

    def test_series_is_continuous_across_the_migration(self):
        a, _ = ex.extract_field(CF["SYY"], ex.SPECS["net_income"], 2019)
        b, _ = ex.extract_field(CF["SYY"], ex.SPECS["net_income"], 2020)
        self.assertGreater(a, 1e9)
        self.assertGreater(b, 1e8)

    def test_ni_over_eps_is_sane(self):
        # a wrong tag here would show up as an absurd implied share count
        for fy in (2014, 2019):
            ni, _ = ex.extract_field(CF["SYY"], ex.SPECS["net_income"], fy)
            eps, _ = ex.extract_field(CF["SYY"], ex.SPECS["eps"], fy)
            self.assertLess(ni / eps, 1e9)
            self.assertGreater(ni / eps, 3e8)


class FinanceLeasesInTotalDebt(unittest.TestCase):
    """total_debt must mean interest-bearing debt INCLUDING finance leases for
    every filer, without double-counting filers whose debt tag already bundles
    them."""

    def test_kr_2025_ties_to_balance_sheet(self):
        # filed balance sheet: 15,764M long-term + 1,802M current = 17,566M
        v, p = ex.derive_total_debt(CF["KR"], 2025)
        self.assertAlmostEqual(v, 17_566.0 * M, delta=0.01 * 17_566.0 * M)
        self.assertIn("FinanceLeaseLiabilityNoncurrent", p["tag"])
        self.assertIn("FinanceLeaseLiabilityCurrent", p["tag"])

    def test_kr_2014_pre_842_bundled_leases_recovered(self):
        # filed balance sheet: 9,771M + 1,885M = 11,656M. No separate lease tag
        # exists; the leases sit inside LongTermDebtAndCapitalLeaseObligations.
        v, p = ex.derive_total_debt(CF["KR"], 2014)
        self.assertAlmostEqual(v, 11_656.0 * M, delta=0.01 * 11_656.0 * M)
        self.assertIn("bundled", p["tag"])

    def test_kr_series_has_no_break_at_2018(self):
        # fixing only FY2018+ would leave a ~6% step in Kroger's own series
        a, _ = ex.derive_total_debt(CF["KR"], 2017)
        b, _ = ex.derive_total_debt(CF["KR"], 2018)
        self.assertLess(abs(b - a) / a, 0.10)

    def test_ko_lease_inclusive_years_unchanged(self):
        # KO FY2024-25 already use LongTermDebtAndCapitalLeaseObligations
        for fy, want in ((2024, 44_522.0), (2025, 45_492.0)):
            v, p = ex.derive_total_debt(CF["KO"], fy)
            self.assertAlmostEqual(v, want * M, delta=2 * M)
            self.assertNotIn("FinanceLease", p["tag"])

    def test_ko_debt_only_years_with_no_leases_unchanged(self):
        # KO reports no separate finance-lease tags at all
        for fy, want in ((2020, 42_793.0), (2023, 42_064.0)):
            v, _ = ex.derive_total_debt(CF["KO"], fy)
            self.assertAlmostEqual(v, want * M, delta=2 * M)

    def test_no_double_count_when_both_present(self):
        # PM is one of 11 filers carrying a lease-inclusive debt tag AND
        # separate finance-lease tags; nothing may be added.
        for fy in (2020, 2024):
            _, p = ex.derive_total_debt(CF["PM"], fy)
            self.assertIn("CapitalLeaseObligations", p["tag"])
            self.assertNotIn("FinanceLease", p["tag"])

    def test_lease_tags_recorded_in_provenance(self):
        _, p = ex.derive_total_debt(CF["KR"], 2025)
        self.assertTrue(p["selector"].startswith("derived:"))
        self.assertGreaterEqual(len(p["tag"].split("+")), 4)

    def test_operating_leases_never_included(self):
        for tk in ("KR", "KO", "SYY", "BG"):
            for fy in (2020, 2025):
                _, p = ex.derive_total_debt(CF[tk], fy)
                if p["tag"]:
                    self.assertNotIn("OperatingLease", p["tag"])
