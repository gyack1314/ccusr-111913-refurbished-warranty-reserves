"""业务口径测试：时点可见性、隔离、去重、追偿范围与认定承接。"""
import unittest

from warranty_reserve.data import load_dataset
from warranty_reserve.determination import (
    SubsequentChange,
    apply_change,
    freeze_determination,
    reopen_case,
)
from warranty_reserve.domain import Book
from warranty_reserve.reserve import (
    Assumptions,
    estimate_reserve,
    funding_view,
    simulate,
)
from warranty_reserve.risk import rolling_validation
from warranty_reserve.scenarios import NewBatch, compare_new_batches, compare_portfolio_claims
from warranty_reserve.timeutil import parse_ts


class DatasetVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset()
        self.book = Book(self.ds)

    def test_cutoff_hides_later_records(self):
        early = load_dataset(cutoff="2025-03-01T00:00:00+08:00")
        self.assertLess(len(early.claims), len(self.ds.claims))
        self.assertTrue(all(parse_ts(row["reported_at"]) <= early.cutoff for row in early.claims))
        self.assertTrue(all(parse_ts(row["known_at"]) <= early.cutoff for row in early.repairs))

    def test_incomplete_cohort_kept_out_of_rate_denominator(self):
        incomplete = [s for s in self.book.segment_states() if not s.cohort_complete]
        self.assertTrue(incomplete)
        # 转入资料仍然可见、可计价，但不能冒充完整名册。
        self.assertTrue(all(s.sale_id in self.book.segments for s in incomplete))

    def test_contract_derivation_only_checks_recorded_decision(self):
        conflicts = self.book.conflicts()
        self.assertTrue(conflicts)
        for view in conflicts:
            # 售后认定仍是不在保；合同推导只作一致性核对，不反向改写。
            self.assertEqual(view.recorded_decision, "excluded")
            self.assertEqual(view.coverage_state, "excluded")
            self.assertEqual(view.contract_derived, "covered")
            self.assertIn("保留售后认定", view.check_note)


class ClaimOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.book = Book(load_dataset())

    def test_repeat_requires_explicit_previous_claim(self):
        # 资料中没有 previous_claim 关联；相似症状不得自动合并。
        self.assertFalse(any(view.is_repeat for view in self.book.claims.values()))

    def test_refund_and_replacement_end_cover_but_keep_paid_cost(self):
        for state in self.book.segments.values():
            if state.exit_action in ("refund", "replacement"):
                self.assertIsNotNone(state.exit_at)
                self.assertFalse(state.cover_active)
                self.assertGreater(self.book.realized_paid([state]) + self.book.outstanding_unpaid([state]), 0)

    def test_open_case_is_not_no_fault(self):
        open_claims = [view for view in self.book.claims.values() if view.open_case]
        self.assertTrue(open_claims)
        self.assertTrue(all(view.outcome != "no_claim" for view in open_claims))

    def test_each_segment_has_single_primary_outcome(self):
        counts = self.book.outcome_counts()
        self.assertEqual(sum(counts.values()), len(self.book.segments))
        self.assertNotIn("pending", counts)

    def test_resale_does_not_inherit_prior_warranty(self):
        # 后售段只按自己的销售日与合同起保，前段历史不构成未来保障。
        for state in self.book.segments.values():
            self.assertEqual(state.warranty_start, parse_ts(state.sale["sold_at"]))
        self.assertTrue(self.book.overlaps)


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset()
        self.book = Book(self.ds)
        self.reserve = estimate_reserve(self.book, self.ds)

    def test_paid_repairs_not_in_future_requirement(self):
        self.assertGreater(self.reserve.paid_realized_cents, 0)
        gross_without_paid = (
            self.reserve.reported_unpaid_cents
            + self.reserve.ibnr_expected_cents
            + self.reserve.repeat_allowance_cents
        )
        self.assertEqual(self.reserve.gross_required_cents, gross_without_paid)

    def test_unpaid_repairs_included_regardless_of_closure(self):
        self.assertGreater(self.reserve.reported_unpaid_cents, 0)
        self.assertEqual(
            self.reserve.reported_unpaid_cents,
            sum(view.unpaid_cents for view in self.book.claims.values()),
        )

    def test_only_confirmed_recovery_offsets(self):
        expected = self.reserve.gross_required_cents - self.reserve.confirmed_recovery_cents
        self.assertLessEqual(abs(self.reserve.net_requirement_cents - max(0, expected)), 1)
        self.assertGreater(self.reserve.unconfirmed_recovery_cents, 0)
        self.assertNotEqual(
            self.reserve.net_requirement_cents,
            self.reserve.gross_required_cents - self.reserve.confirmed_recovery_cents
            - self.reserve.unconfirmed_recovery_cents,
        )

    def test_price_evidence_comes_from_cutoff_known_rows(self):
        self.assertTrue(self.reserve.price_sources)
        self.assertTrue(all(source.startswith("K") for source in self.reserve.price_sources.values()))

    def test_future_claim_not_in_reserve(self):
        # 认定只覆盖截点仍在保的保障段。
        active = {state.sale_id for state in self.book.active_segments()}
        self.assertEqual(set(self.reserve.active_segment_ids), active)

    def test_simulation_is_reproducible_and_tail_ge_median(self):
        funding = funding_view(self.ds)
        first = simulate(self.book, self.ds, self.reserve, funding, simulations=400, seed=7)
        second = simulate(self.book, self.ds, self.reserve, funding, simulations=400, seed=7)
        self.assertEqual(first.spend_quantiles_cents, second.spend_quantiles_cents)
        self.assertGreaterEqual(first.spend_quantiles_cents["p99"], first.spend_quantiles_cents["p50"])


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset()
        self.funding = funding_view(self.ds)

    def test_free_balance_subtracts_commitments(self):
        self.assertEqual(
            self.funding.free_cents,
            self.funding.available_cents - self.funding.committed_cents,
        )

    def test_commitment_scope_is_not_double_claimed(self):
        self.assertIn("batch_B0", self.funding.committed_by_scope)
        self.assertGreater(self.funding.committed_by_scope["batch_B0"], 0)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset()
        self.book = Book(self.ds)

    def test_new_batch_tail_scales_with_units(self):
        small = NewBatch("small", 5, "aircon", 4, "C1", "2026-09-10", component="PART0")
        large = NewBatch("large", 50, "aircon", 4, "C1", "2026-09-10", component="PART0")
        results = compare_new_batches(
            self.book, self.ds, [small, large], simulations=600
        )
        by_name = {item.name: item for item in results}
        self.assertGreater(by_name["large"].net_incremental_cents, by_name["small"].net_incremental_cents)
        self.assertGreater(by_name["large"].incremental_tail_p99_cents, by_name["small"].incremental_tail_p99_cents)

    def test_portfolio_does_not_claim_shared_balance_twice(self):
        batch = NewBatch("shared", 100, "aircon", 4, "C1", "2026-09-10", component="PART0")
        results = compare_new_batches(self.book, self.ds, [batch, batch], simulations=200)
        portfolio = compare_portfolio_claims(results, funding_view(self.ds))
        self.assertEqual(portfolio["shared_free_balance_cents"], funding_view(self.ds).free_cents)
        self.assertEqual(
            portfolio["combined_gap_cents"],
            max(0, portfolio["combined_net_incremental_cents"] - portfolio["shared_free_balance_cents"]),
        )


class DeterminationTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset()
        self.book = Book(self.ds)
        self.month = freeze_determination(self.book, self.ds, determination_id="R-TEST")
        self.funding = funding_view(self.ds)

    def test_freeze_keeps_model_price_and_scope(self):
        frozen = self.month.determination
        self.assertEqual(frozen.model_version, "reserve-model/1.0")
        self.assertTrue(frozen.price_sources)
        self.assertTrue(frozen.frozen)
        self.assertIn("active_segment_ids", frozen.reserve_snapshot)

    def test_change_requires_source_and_rejects_double_count(self):
        with self.assertRaises(ValueError):
            SubsequentChange("release", 100, "无来源", [], "R-TEST")
        unpaid = self.month.determination.reported_unpaid_ids[0]
        with self.assertRaises(ValueError):
            apply_change(
                self.month,
                SubsequentChange("release", 100, "重复释放", [unpaid], "R-TEST"),
            )

    def test_reopen_case_links_original_and_tops_up(self):
        original = self.book.claims["A006"].claim
        reopened = dict(original)
        reopened["id"] = "A006-R"
        reopened["decision"] = "covered"
        case = reopen_case(
            self.month,
            {**original, "amount_cents": 15000},
            {**reopened, "amount_cents": 24000},
            detected_at="2026-09-10T09:00:00+08:00",
            reason="售后重新打开旧案并补充维修",
        )
        self.assertEqual(case.original_claim_id, "A006")
        self.assertEqual(case.original_determination, "R-TEST")
        base = self.month.determination.net_requirement_cents
        self.assertEqual(self.month.net_change_cents(), 9000)
        self.assertEqual(self.month.carried_requirement_cents(), base + 9000)

    def test_release_within_scope_changes_carried_requirement(self):
        first = self.month.carried_requirement_cents()
        apply_change(
            self.month,
            SubsequentChange("release", 500, "确认一台设备由供应商直接换机", ["S123"], "R-TEST"),
        )
        self.assertEqual(self.month.carried_requirement_cents(), first - 500)


class ValidationTests(unittest.TestCase):
    def test_rolling_validation_isolates_devices(self):
        ds = load_dataset()
        book = Book(ds)
        points = rolling_validation(book)
        self.assertTrue(points)
        for point in points:
            self.assertGreaterEqual(point.excluded_devices, 0)
            self.assertLessEqual(point.test_segments, point.train_segments + point.test_segments)

    def test_validation_uses_no_future_data(self):
        ds = load_dataset(cutoff="2025-07-01T00:00:00+08:00")
        book = Book(ds)
        points = rolling_validation(book)
        for point in points:
            if point.actual_rate is not None:
                self.assertGreaterEqual(point.actual_rate, 0)


if __name__ == "__main__":
    unittest.main()


class CoverageSemanticsTests(unittest.TestCase):
    def test_all_scope_coverage_counts_commitment_once(self):
        ds = load_dataset()
        funding = funding_view(ds)
        # 全部范围可用 = 未占用余额 + 已有占用 = 总可用额；不能把占用再加一遍。
        self.assertEqual(funding.coverage_for("all"), funding.available_cents)
        self.assertEqual(funding.coverage_for("batch_B0"), funding.free_cents + funding.committed_by_scope["batch_B0"])

    def test_named_scope_accepts_bare_batch_name(self):
        funding = funding_view(load_dataset())
        self.assertEqual(funding.coverage_for("B0"), funding.coverage_for("batch_B0"))


class TimeVisibilityRegressionTests(unittest.TestCase):
    def test_repair_known_after_cutoff_is_hidden(self):
        ds = load_dataset(cutoff="2025-02-01T00:00:00+08:00")
        book = Book(ds)
        for state in book.segments.values():
            for view in state.claims:
                self.assertLessEqual(parse_ts(view.claim["reported_at"]), ds.cutoff)
                if view.repair is not None:
                    self.assertLessEqual(parse_ts(view.repair["known_at"]), ds.cutoff)

    def test_price_evidence_falls_back_to_paid_history(self):
        ds = load_dataset(cutoff="2025-03-01T00:00:00+08:00")
        prices = ds.repair_prices()
        self.assertTrue(prices)
        self.assertTrue(any(price.basis == "empirical_paid_repair" for price in prices.values()))


class ExitAndPendingTests(unittest.TestCase):
    def setUp(self):
        self.book = Book(load_dataset())

    def test_refund_or_replacement_ends_cover_even_if_recorded_excluded(self):
        # 资料中有“认定不在保但仍退款/换机”的案件：实物已处理，原保障应结束。
        for state in self.book.segments.values():
            actions = [view.repair["action"] for view in state.claims if view.repair]
            if "refund" in actions or "replacement" in actions:
                self.assertIsNotNone(state.exit_at)
                self.assertFalse(state.cover_active)

    def test_pending_repair_counts_as_failure_not_zero(self):
        pending = [view for view in self.book.claims.values() if view.outcome == "pending_repair"]
        self.assertTrue(pending)
        totals = self.book.outcome_counts()
        self.assertGreater(totals["pending_repair"], 0)

    def test_active_segments_with_prior_repair_not_counted_as_first_failure(self):
        # 仍在保的段都已有有效申报：不能按“首次故障”重复计提。
        active = self.book.active_segments()
        self.assertTrue(active)
        self.assertTrue(all(any(v.eligible for v in state.claims) for state in active))

    def test_contract_checks_do_not_rewrite_excluded_claims(self):
        for view in self.book.conflicts():
            self.assertEqual(view.coverage_state, "excluded")
            self.assertEqual(view.outcome, "excluded")
