"""准备金认定：已报未决、未报有效保障、追偿与资金缺口。

计算口径（与资料契约一致）：
- 已支付维修属于已实现成本，只展示，不再进入未来准备金；
- 未支付维修按价格证据计入“已报未决”，不因结案或未结案而消失；
- 退款/换机结束原保障，但不删除此前已发生费用；
- 只在确认范围内抵减追偿，未确认追偿单列备忘；
- 有效保障中尚未申报的部分按首次故障风险计提，但明确标注证据不足；
- 同一零件组可能同时出险，尾部模拟支持共同冲击而不是默认独立。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable

from .data import Dataset, PriceEvidence
from .domain import Book, SegmentState
from .timeutil import add_months, parse_ts, percentile

MODEL_VERSION = "reserve-model/1.0"
DEFAULT_SIMULATIONS = 4000


@dataclass
class Assumptions:
    """明确列出的情景假设；默认值不是拟合可靠性结论。"""

    repeat_prior_alpha: float = 0.5
    repeat_prior_beta: float = 0.5
    repeat_upper_confidence: float = 0.95
    common_mode_probability: float = 0.05
    common_mode_hit_rate: float = 0.35
    severity_log_sigma: float = 0.60
    simulate_components: bool = True

    def describe(self) -> dict:
        return {
            "repeat_prior": (
                f"重复维修按每案 Beta({self.repeat_prior_alpha:g}, {self.repeat_prior_beta:g}) "
                f"的 Jeffreys 先验取 {self.repeat_upper_confidence:.0%} 上限，仅作证据不足备抵"
            ),
            "common_mode_probability": f"{self.common_mode_probability:.1%} 概率/零件组/批次出现共同冲击",
            "common_mode_hit_rate": f"共同冲击下同组有效保障额外 {self.common_mode_hit_rate:.0%} 出险",
            "severity_log_sigma": f"单案费用对数标准差 {self.severity_log_sigma}",
            "note": "这些是情景假设，不是从数据拟合出的可靠参数；证据不足批次保留限制",
        }


@dataclass
class HazardModel:
    """按合同估计的月度首次故障风险（完整名册、按销售后月龄）。"""

    monthly: dict[str, list[float]]
    monthly_counts: dict[str, list[tuple[int, int]]]
    max_months: int
    complete_segments: int
    source_ids: list[str] = field(default_factory=list)

    def remaining_claim_probability(
        self, contract: str, remaining_months: float, elapsed_months: float = 0.0
    ) -> float:
        """从当前时点起，剩余保障期内至少一次首次故障的概率。

        elapsed_months 是该保障段在截点已过的保障月龄；老设备不能按新售首月风险起算。
        """
        if remaining_months <= 0:
            return 0.0
        hazards = self.monthly.get(contract, [])
        if not hazards:
            return 0.0
        survival = 1.0
        whole = int(math.floor(remaining_months))
        start = max(0, int(math.floor(elapsed_months)))
        for offset in range(whole):
            index = start + offset
            h = hazards[index] if index < len(hazards) else hazards[-1]
            survival *= 1.0 - h
        fraction = remaining_months - whole
        if fraction > 0:
            index = start + whole
            h = hazards[index] if index < len(hazards) else hazards[-1]
            survival *= 1.0 - h * fraction
        return max(0.0, min(1.0, 1.0 - survival))

    def to_dict(self) -> dict:
        return {
            "complete_segments": self.complete_segments,
            "max_months": self.max_months,
            "monthly_hazard": {
                contract: [round(value, 4) for value in self.monthly[contract]]
                for contract in sorted(self.monthly)
            },
            "monthly_events_at_risk": {
                contract: [[event, risk] for event, risk in self.monthly_counts[contract]]
                for contract in sorted(self.monthly_counts)
            },
            "note": "按完整名册的销售后月龄暴露估计；转入资料不进入分母",
        }


def fit_hazard(book: Book) -> HazardModel:
    """按合同、销售后月龄估计首次故障风险；退款/换机作为竞争退出。"""
    complete = [state for state in book.segment_states() if state.cohort_complete]
    max_months = max(
        [int(state.contract["months"]) for state in complete] or [12]
    )
    monthly: dict[str, list[float]] = {}
    counts: dict[str, list[tuple[int, int]]] = {}
    for contract in sorted({state.contract_id for state in complete}):
        states = [state for state in complete if state.contract_id == contract]
        events = [0] * max_months
        at_risk = [0] * max_months
        for state in states:
            start = state.warranty_start
            observed_end = min(book.cutoff, parse_ts(state.sale["observed_until"]))
            if state.exit_at is not None:
                observed_end = min(observed_end, state.exit_at)
            first_claim = next(
                (
                    view
                    for view in state.claims
                    if view.eligible
                    and view.outcome
                    in (
                        "covered_repair",
                        "covered_refund",
                        "covered_replacement",
                        "covered_repeat",
                        "pending_repair",
                    )
                ),
                None,
            )
            claim_time = parse_ts(first_claim.claim["occurred_at"]) if first_claim else None
            if claim_time is not None and claim_time < start:
                claim_time = None
            for month in range(max_months):
                month_start = add_months(start, month)
                if month_start >= observed_end:
                    break
                at_risk[month] += 1
                if claim_time is not None and month_start <= claim_time < add_months(start, month + 1):
                    events[month] += 1
        hazards = []
        for event, risk in zip(events, at_risk):
            # Jeffreys 平滑，避免零事件月份把风险压成绝对零。
            hazards.append((event + 0.5) / (risk + 1.0) if risk > 0 else 0.0)
        monthly[contract] = hazards
        counts[contract] = list(zip(events, at_risk))
    return HazardModel(
        monthly=monthly,
        monthly_counts=counts,
        max_months=max_months,
        complete_segments=len(complete),
        source_ids=[state.sale_id for state in complete],
    )


@dataclass
class SeverityModel:
    component_mix: dict[str, float]
    action_mix: dict[str, dict[str, float]]
    action_costs: dict[str, int]
    component_prices: dict[str, PriceEvidence]
    contract_action_samples: dict[str, dict[str, int]]

    @property
    def repair_cost_cents(self) -> int:
        return self.action_costs.get("repair", 0)


def fit_severity(book: Book, ds: Dataset) -> SeverityModel:
    component_counts: dict[str, int] = {}
    action_counts: dict[str, dict[str, int]] = {}
    for view in book.claims.values():
        if view.outcome == "excluded" or view.repair is None:
            continue
        component_counts[view.repair["component_group"]] = component_counts.get(view.repair["component_group"], 0) + 1
        bucket = action_counts.setdefault(view.sale["contract"], {})
        bucket[view.repair["action"]] = bucket.get(view.repair["action"], 0) + 1
    total = sum(component_counts.values()) or 1
    component_mix = {key: value / total for key, value in sorted(component_counts.items())}
    contract_action_samples = {}
    for contract, bucket in action_counts.items():
        contract_action_samples[contract] = dict(bucket)
    action_mix = {}
    for contract, bucket in sorted(action_counts.items()):
        count = sum(bucket.values()) or 1
        action_mix[contract] = {action: bucket.get(action, 0) / count for action in ("repair", "refund", "replacement")}
    paid_by_action: dict[str, list[int]] = {}
    for view in book.claims.values():
        if view.repair is None:
            continue
        paid_by_action.setdefault(view.repair["action"], []).append(int(view.repair["cost_cents"]))
    action_costs = {
        action: int(sorted(values)[len(values) // 2])
        for action, values in paid_by_action.items()
        if values
    }
    return SeverityModel(
        component_mix=component_mix,
        action_mix=action_mix,
        action_costs=action_costs,
        component_prices=ds.repair_prices(),
        contract_action_samples=contract_action_samples,
    )


@dataclass
class ReserveComponents:
    as_of: str
    scope: str
    reported_unpaid_cents: int
    reported_unpaid_ids: list[str]
    paid_realized_cents: int
    ibnr_expected_cents: int
    ibnr_segments: int
    ibnr_detail: list[dict]
    active_without_claim: int
    repeat_allowance_cents: int
    repeat_detail: dict
    recurrence_detail: list[dict]
    gross_required_cents: int
    confirmed_recovery_cents: int
    confirmed_recovery_ids: list[str]
    unconfirmed_recovery_cents: int
    unconfirmed_recovery_ids: list[str]
    net_requirement_cents: int
    active_segment_ids: list[str]
    price_sources: dict[str, str]
    hazard: HazardModel
    severity: SeverityModel
    assumptions: Assumptions
    evidence_limits: list[str]
    model_version: str = MODEL_VERSION

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "scope": self.scope,
            "reported_unpaid_cents": self.reported_unpaid_cents,
            "reported_unpaid_ids": self.reported_unpaid_ids,
            "paid_realized_cents": self.paid_realized_cents,
            "ibnr_expected_cents": self.ibnr_expected_cents,
            "ibnr_segments": self.ibnr_segments,
            "ibnr_detail": self.ibnr_detail,
            "active_without_claim": self.active_without_claim,
            "repeat_allowance_cents": self.repeat_allowance_cents,
            "repeat_detail": self.repeat_detail,
            "recurrence_detail": self.recurrence_detail,
            "gross_required_cents": self.gross_required_cents,
            "confirmed_recovery_cents": self.confirmed_recovery_cents,
            "confirmed_recovery_ids": self.confirmed_recovery_ids,
            "unconfirmed_recovery_cents": self.unconfirmed_recovery_cents,
            "unconfirmed_recovery_ids": self.unconfirmed_recovery_ids,
            "net_requirement_cents": self.net_requirement_cents,
            "active_segment_ids": self.active_segment_ids,
            "price_sources": self.price_sources,
            "hazard": self.hazard.to_dict(),
            "severity": {
                "component_mix": {key: round(value, 3) for key, value in self.severity.component_mix.items()},
                "action_mix": {
                    key: {action: round(value, 3) for action, value in mix.items()}
                    for key, mix in self.severity.action_mix.items()
                },
                "action_costs_cents": self.severity.action_costs,
            },
            "assumptions": self.assumptions.describe(),
            "evidence_limits": self.evidence_limits,
            "model_version": MODEL_VERSION,
        }


def _monthly_marginal_probabilities(
    hazard: HazardModel, contract: str, remaining_months: float, elapsed_months: float
) -> list[float]:
    """剩余期内逐月的条件首次故障概率，供模拟按时间累积。"""
    hazards = hazard.monthly.get(contract, [])
    if not hazards or remaining_months <= 0:
        return []
    start = max(0, int(math.floor(elapsed_months)))
    whole = int(math.floor(remaining_months))
    fraction = remaining_months - whole
    output = []
    for offset in range(whole):
        index = start + offset
        output.append(hazards[index] if index < len(hazards) else hazards[-1])
    if fraction > 0:
        index = start + whole
        output.append((hazards[index] if index < len(hazards) else hazards[-1]) * fraction)
    return output


def _expected_future_claim_cost(
    contract: str, severity: SeverityModel
) -> tuple[float, dict[str, float]]:
    component_terms = {}
    total = 0.0
    mix = severity.action_mix.get(contract) or {"repair": 1.0, "refund": 0.0, "replacement": 0.0}
    for component, weight in severity.component_mix.items():
        price = severity.component_prices.get(component)
        repair_cost = price.repair_cost_cents if price else severity.action_costs.get("repair", 0)
        refund_cost = severity.action_costs.get("refund", repair_cost)
        replacement_cost = severity.action_costs.get("replacement", repair_cost)
        expected = (
            mix.get("repair", 0.0) * repair_cost
            + mix.get("refund", 0.0) * refund_cost
            + mix.get("replacement", 0.0) * replacement_cost
        )
        component_terms[component] = expected
        total += weight * expected
    return total, component_terms


def estimate_reserve(
    book: Book,
    ds: Dataset,
    scope: str = "all",
    assumptions: Assumptions | None = None,
    states: Iterable[SegmentState] | None = None,
) -> ReserveComponents:
    assumptions = assumptions or Assumptions()
    hazard = fit_hazard(book)
    severity = fit_severity(book, ds)
    if states is not None:
        selected = list(states)
    else:
        selected = _segments_for_scope(book, scope)

    unpaid_ids = []
    unpaid = 0
    paid = 0
    for state in selected:
        for view in state.claims:
            if view.unpaid_cents:
                unpaid += view.unpaid_cents
                unpaid_ids.extend(
                    repair["id"] for repair in ds.repairs_by_claim.get(view.claim_id, []) if not repair["paid"]
                )
            paid += view.paid_cents

    # 有效保障分两类计提，避免把“首次故障”重复套在已出险设备上：
    # 1) 从未有过有效申报的保障段 -> 首次故障 IBNR；
    # 2) 已有有效维修的保障段 -> 维修后再次出险的弱证据上限备抵。
    first_failure_detail = []
    first_failure_ibnr = 0.0
    first_failure_segments = 0
    recurrence_detail = []
    recurrence_allowance = 0.0
    recurrence_segments = 0

    for state in selected:
        if not state.cover_active:
            continue
        remaining_months = state.remaining_cover_days / 30.4375
        if remaining_months <= 0:
            continue
        prior_eligible = [
            view
            for view in state.claims
            if view.eligible
            and view.outcome
            in (
                "covered_repair",
                "covered_repeat",
                "covered_refund",
                "covered_replacement",
                "pending_repair",
            )
        ]
        if prior_eligible:
            continue
        probability = hazard.remaining_claim_probability(
            state.contract_id, remaining_months, state.elapsed_cover_days / 30.4375
        )
        if probability <= 0:
            continue
        expected_cost, terms = _expected_future_claim_cost(state.contract_id, severity)
        amount = probability * expected_cost
        first_failure_ibnr += amount
        first_failure_segments += 1
        first_failure_detail.append(
            {
                "sale": state.sale_id,
                "batch": state.batch,
                "contract": state.contract_id,
                "remaining_months": round(remaining_months, 2),
                "claim_probability": round(probability, 4),
                "expected_cost_cents": round(expected_cost, 2),
                "ibnr_cents": round(amount, 2),
                "component_terms_cents": {key: round(value, 2) for key, value in terms.items()},
            }
        )

    # 重复/再次出险只能凭 previous_claim 关联；资料中没有关联案件时，
    # 用每案 Beta 先验的 95% 上限给限制性备抵，而不是把首次故障率再算一遍。
    repeat_events = sum(
        1 for view in book.claims.values() if view.outcome == "covered_repeat"
    )
    repaired_cases = [
        view
        for state in selected
        for view in state.claims
        if view.repair is not None and view.repair["action"] == "repair" and view.eligible
    ]
    repaired = len(repaired_cases)
    repeat_exposure_years = 0.0
    for view in repaired_cases:
        repair_time = parse_ts(view.repair["occurred_at"])
        state = book.segments[view.sale_id]
        follow_end = min(book.cutoff, state.warranty_end)
        if state.exit_at is not None:
            follow_end = min(follow_end, state.exit_at)
        repeat_exposure_years += max(0.0, (follow_end - repair_time).days / 365.25)
    try:
        from scipy.stats import beta as beta_dist
        from scipy.stats import chi2

        posterior_mean = (repeat_events + assumptions.repeat_prior_alpha) / (
            repaired + assumptions.repeat_prior_alpha + assumptions.repeat_prior_beta
        ) if repaired else 0.0
        upper_probability = (
            float(
                beta_dist.ppf(
                    assumptions.repeat_upper_confidence,
                    repeat_events + assumptions.repeat_prior_alpha,
                    repaired - repeat_events + assumptions.repeat_prior_beta,
                )
            )
            if repaired
            else 0.0
        )
        # 每案上限换算为年化上限，用于按剩余保障期折算；无事件时用 Gamma 上限。
        if repeat_exposure_years > 0:
            upper_rate_per_year = float(
                chi2.ppf(assumptions.repeat_upper_confidence, 2 * (repeat_events + 1))
            ) / (2 * repeat_exposure_years)
        else:
            upper_rate_per_year = 0.0
    except Exception:  # pragma: no cover
        posterior_mean = (repeat_events + 0.5) / (repaired + 1.0) if repaired else 0.0
        upper_probability = min(0.5, posterior_mean * 3.0)
        upper_rate_per_year = (
            min(0.5, 3.0 / repeat_exposure_years) if repeat_exposure_years > 0 else 0.0
        )

    for state in selected:
        if not state.cover_active:
            continue
        repaired_here = [
            view
            for view in state.claims
            if view.repair is not None and view.repair["action"] == "repair" and view.eligible
        ]
        if not repaired_here:
            continue
        remaining_years = state.remaining_cover_days / 365.25
        probability = 1.0 - math.exp(-upper_rate_per_year * remaining_years) if upper_rate_per_year else 0.0
        component = repaired_here[-1].repair["component_group"]
        price = severity.component_prices.get(component)
        unit = price.repair_cost_cents if price else severity.action_costs.get("repair", 0)
        amount = probability * unit
        recurrence_allowance += amount
        recurrence_segments += 1
        recurrence_detail.append(
            {
                "sale": state.sale_id,
                "batch": state.batch,
                "remaining_months": round(state.remaining_cover_days / 30.4375, 2),
                "recurrence_probability": round(probability, 4),
                "unit_cost_cents": unit,
                "component": component,
                "allowance_cents": round(amount, 2),
            }
        )

    ibnr = first_failure_ibnr
    ibnr_segments = first_failure_segments
    detail = first_failure_detail
    repeat_allowance = recurrence_allowance
    repeat_detail = {
        "linked_repeat_events": repeat_events,
        "repaired_cases": repaired,
        "post_repair_exposure_years": round(repeat_exposure_years, 2),
        "posterior_mean_probability_per_case": round(posterior_mean, 4),
        "upper_95_probability_per_case": round(upper_probability, 4),
        "upper_95_rate_per_year": round(upper_rate_per_year, 4),
        "active_segments_with_repair": recurrence_segments,
        "note": "无 previous_claim 关联时不按相似症状合并；仅对仍在保的已维修段按年化上限折算，属证据不足备抵",
    }

    # 追偿只在“有效且已确认”的范围内抵减：
    # 1) 未报有效保障按期望索赔数与确认追偿额；
    # 2) 已报未付维修按同零件组确认追偿额抵减；
    # 3) 已支付维修的追偿不进入未来准备金，避免与已实现成本重复。
    confirmed_recovery = 0.0
    unconfirmed_recovery = 0.0
    confirmed_ids: list[str] = []
    unconfirmed_ids: list[str] = []
    expected_claims = 0.0
    for state in selected:
        if not state.cover_active:
            continue
        remaining_months = state.remaining_cover_days / 30.4375
        if remaining_months <= 0:
            continue
        expected_claims += hazard.remaining_claim_probability(
            state.contract_id, remaining_months, state.elapsed_cover_days / 30.4375
        )
    for component, weight in severity.component_mix.items():
        price = severity.component_prices.get(component)
        if price is None:
            continue
        if price.recovery_confirmed:
            confirmed_recovery += weight * expected_claims * price.recovery_cents
            confirmed_ids.append(price.source_id)
        else:
            unconfirmed_recovery += weight * expected_claims * price.recovery_cents
            unconfirmed_ids.append(price.source_id)
    # 已报未付维修：只对可确认追偿的零件组抵减，且不碰已支付案件。
    for state in selected:
        for view in state.claims:
            if view.repair is None or view.paid_cents > 0 or not view.eligible:
                continue
            if view.unpaid_cents <= 0:
                continue
            price = severity.component_prices.get(view.repair["component_group"])
            if price is None:
                continue
            if price.recovery_confirmed:
                confirmed_recovery += price.recovery_cents
                confirmed_ids.append(price.source_id)
            else:
                unconfirmed_recovery += price.recovery_cents
                unconfirmed_ids.append(price.source_id)

    gross = unpaid + ibnr + repeat_allowance
    net = max(0.0, gross - confirmed_recovery)
    price_sources = {
        component: price.source_id for component, price in severity.component_prices.items()
    }
    limits = []
    incomplete_active = [state for state in selected if state.cover_active and not state.cohort_complete]
    if incomplete_active:
        limits.append(f"{len(incomplete_active)} 段有效保障来自不完整名册，不计入批次率分母")
    if ibnr_segments == 0 and any(state.cover_active for state in selected):
        limits.append("仍在保的保障段都已有有效申报，未按首次故障重复计提；仅保留维修后再出险备抵")
    if any(state.cover_active and not state.claims for state in selected):
        limits.append("仍有从未申报的在保段，但剩余观察很短；首次故障概率按合同月龄上界展示")
    if ibnr_segments and ibnr < 1:
        limits.append("有效保障剩余期内未报暴露很小，尾部仍按模拟展示")
    if not any(view.outcome == "covered_repeat" for view in book.claims.values()):
        limits.append("资料中没有 previous_claim 关联的重复维修；再出险按每案 Beta 先验上限，而非点估计")
    if repeat_allowance > 0:
        limits.append("维修后再出险备抵证据不足，仅保留限制性金额，不据此声称可靠")
    return ReserveComponents(
        as_of=book.cutoff.isoformat(),
        scope=scope,
        reported_unpaid_cents=int(round(unpaid)),
        reported_unpaid_ids=sorted(unpaid_ids),
        paid_realized_cents=int(paid),
        ibnr_expected_cents=int(round(ibnr)),
        ibnr_segments=ibnr_segments,
        ibnr_detail=detail,
        active_without_claim=sum(
            1 for state in selected if state.cover_active and not state.claims
        ),
        repeat_allowance_cents=int(round(repeat_allowance)),
        repeat_detail=repeat_detail,
        recurrence_detail=recurrence_detail,
        gross_required_cents=int(round(gross)),
        confirmed_recovery_cents=int(round(confirmed_recovery)),
        confirmed_recovery_ids=sorted(set(confirmed_ids)),
        unconfirmed_recovery_cents=int(round(unconfirmed_recovery)),
        unconfirmed_recovery_ids=sorted(set(unconfirmed_ids)),
        net_requirement_cents=int(round(net)),
        active_segment_ids=sorted(state.sale_id for state in selected if state.cover_active),
        price_sources=price_sources,
        hazard=hazard,
        severity=severity,
        assumptions=assumptions,
        evidence_limits=limits,
    )


def _segments_for_scope(book: Book, scope: str) -> list[SegmentState]:
    """范围过滤：all 为全部；batch_X 为该批次；其他按合同号匹配。"""
    if scope in ("all", "*"):
        return book.segment_states()
    if scope.startswith("batch_"):
        return book.batch_segments(scope[len("batch_"):])
    if scope in book.batches():
        return book.batch_segments(scope)
    if scope in book.ds.contracts:
        return [state for state in book.segment_states() if state.contract_id == scope]
    return []


# ---- 资金与缺口 -----------------------------------------------------
@dataclass
class FundingView:
    available_cents: int
    committed_cents: int
    free_cents: int
    committed_by_scope: dict[str, int]
    commitment_ids: list[str]
    fund_ids: list[str]

    def coverage_for(self, scope: str) -> int:
        """方案可使用的资金：全局未占用余额 + 本范围已有占用。

        多个方案并列时必须用组合口径复核，不能各自把 free_cents 全额领走。
        """
        if scope in ("all", "*"):
            # 全部范围：未占用余额 + 全部已有占用 = 总可用额，占用只算一次。
            return self.free_cents + self.committed_cents
        own = self.committed_by_scope.get(scope)
        if own is None:
            # 资料用 batch_B0 这样的范围键；兼容调用方传 B0。
            own = self.committed_by_scope.get(f"batch_{scope}", 0)
        return self.free_cents + own

    def to_dict(self) -> dict:
        return {
            "available_cents": self.available_cents,
            "committed_cents": self.committed_cents,
            "free_cents": self.free_cents,
            "committed_by_scope": self.committed_by_scope,
            "commitment_ids": self.commitment_ids,
            "fund_ids": self.fund_ids,
            "note": "可用额是总额，已占用部分从余额扣除；各批次方案不重复认领同一笔共享余额",
        }


def funding_view(ds: Dataset, scope: str = "all") -> FundingView:
    by_scope: dict[str, int] = {}
    for row in ds.commitments:
        by_scope[row["scope"]] = by_scope.get(row["scope"], 0) + int(row["cents"])
    committed = sum(by_scope.values())
    return FundingView(
        available_cents=ds.available_funds,
        committed_cents=committed,
        free_cents=max(0, ds.available_funds - committed),
        committed_by_scope=by_scope,
        commitment_ids=[row["id"] for row in ds.commitments],
        fund_ids=[row["id"] for row in ds.funds],
    )


@dataclass
class GapView:
    scope: str
    requirement_cents: int
    coverage_cents: int
    gap_cents: int
    coverage_ratio: float | None
    stale_warning: str | None = None

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "requirement_cents": self.requirement_cents,
            "coverage_cents": self.coverage_cents,
            "gap_cents": self.gap_cents,
            "coverage_ratio": None if self.coverage_ratio is None else round(self.coverage_ratio, 3),
            "stale_warning": self.stale_warning,
        }


def gap_view(reserve: ReserveComponents, funding: FundingView) -> GapView:
    coverage = funding.coverage_for(reserve.scope)
    gap = max(0, reserve.net_requirement_cents - coverage)
    ratio = (
        None
        if reserve.net_requirement_cents <= 0
        else coverage / reserve.net_requirement_cents
    )
    warning = None
    if reserve.scope != "all" and funding.free_cents > 0:
        warning = "共享可用余额同时被其他方案引用时可能重复认领，需在组合层面再核对"
    return GapView(
        scope=reserve.scope,
        requirement_cents=reserve.net_requirement_cents,
        coverage_cents=coverage,
        gap_cents=gap,
        coverage_ratio=ratio,
        stale_warning=warning,
    )


# ---- 尾部模拟 -------------------------------------------------------
@dataclass
class SimulationResult:
    simulations: int
    spend_quantiles_cents: dict[str, int]
    gap_quantiles_cents: dict[str, int]
    shortfall_probability: float
    common_mode_share: float
    independent_expected_cents: int
    correlated_expected_cents: int
    note: str

    def to_dict(self) -> dict:
        return {
            "simulations": self.simulations,
            "spend_quantiles_cents": self.spend_quantiles_cents,
            "gap_quantiles_cents": self.gap_quantiles_cents,
            "shortfall_probability": round(self.shortfall_probability, 3),
            "common_mode_share": round(self.common_mode_share, 3),
            "independent_expected_cents": self.independent_expected_cents,
            "correlated_expected_cents": self.correlated_expected_cents,
            "note": self.note,
        }


def _quantiles(values: list[float]) -> dict[str, int]:
    ordered = sorted(values)
    return {
        "p50": int(round(percentile(ordered, 0.5) or 0)),
        "p90": int(round(percentile(ordered, 0.9) or 0)),
        "p95": int(round(percentile(ordered, 0.95) or 0)),
        "p99": int(round(percentile(ordered, 0.99) or 0)),
        "mean": int(round(sum(ordered) / len(ordered))) if ordered else 0,
    }


def _draw_event_cost(
    state_contract: str,
    component: str,
    severity: SeverityModel,
    rng: random.Random,
    assumptions: Assumptions,
    factor: float = 1.0,
) -> float:
    action = rng.choices(
        ["repair", "refund", "replacement"],
        weights=[
            severity.action_mix.get(state_contract, {}).get(key, 0.0) or 0.0001
            for key in ("repair", "refund", "replacement")
        ],
        k=1,
    )[0]
    price = severity.component_prices.get(component)
    base = (
        price.repair_cost_cents
        if action == "repair" and price
        else severity.action_costs.get(action, severity.action_costs.get("repair", 0))
    )
    if base <= 0:
        return 0.0
    amount = base * math.exp(rng.gauss(0, assumptions.severity_log_sigma)) * factor
    recovery = price.confirmed_recovery if (price and action == "repair") else 0
    return max(0.0, amount - recovery)


def _simulate_path(
    states: list,
    severity: SeverityModel,
    hazard: HazardModel,
    components: list[str],
    component_weights: list[float],
    rng: random.Random,
    assumptions: Assumptions,
    common_mode: bool,
    extra: list[dict],
    recurrence_rate_per_year: float,
) -> tuple[float, int]:
    """逐月推进：先抽零件组共同冲击，再分别模拟首次故障与维修后再出险。"""
    hit_groups: set[str] = set()
    if common_mode:
        for component in components:
            if rng.random() < assumptions.common_mode_probability:
                hit_groups.add(component)
    hits = 0
    total = 0.0
    for state in states:
        remaining = state.remaining_cover_days / 30.4375
        if remaining <= 0:
            continue
        previously_claimed = any(
            view.eligible
            and view.outcome
            in (
                "covered_repair",
                "covered_repeat",
                "covered_refund",
                "covered_replacement",
                "pending_repair",
            )
            for view in state.claims
        )
        component = rng.choices(components, weights=component_weights, k=1)[0]
        factor = 1.0
        boosted = False
        if common_mode and component in hit_groups:
            factor = 1.0 + assumptions.common_mode_hit_rate
            boosted = True
        if previously_claimed:
            # 已出险设备只模拟“再出险”，用上限年化率；不与首次故障率叠加。
            remaining_years = remaining / 12.0
            probability = (
                1.0 - math.exp(-recurrence_rate_per_year * remaining_years)
                if recurrence_rate_per_year > 0
                else 0.0
            )
            if boosted:
                probability = min(0.95, probability * factor)
            if rng.random() < probability:
                repaired = [
                    view
                    for view in state.claims
                    if view.repair is not None and view.repair["action"] == "repair" and view.eligible
                ]
                component_for_cost = repaired[-1].repair["component_group"] if repaired else component
                total += _draw_event_cost(
                    state.contract_id, component_for_cost, severity, rng, assumptions, factor
                )
                hits += 1
            continue
        marginals = _monthly_marginal_probabilities(
            hazard, state.contract_id, remaining, state.elapsed_cover_days / 30.4375
        )
        for probability in marginals:
            draw = probability * (1.0 + assumptions.common_mode_hit_rate) if boosted else probability
            if rng.random() < min(0.95, draw):
                total += _draw_event_cost(state.contract_id, component, severity, rng, assumptions, factor)
                hits += 1
                break
    for segment in extra:
        total += _simulate_new_segment(segment, severity, hazard, rng, assumptions)
    return total, hits


def simulate(
    book: Book,
    ds: Dataset,
    reserve: ReserveComponents,
    funding: FundingView,
    simulations: int = DEFAULT_SIMULATIONS,
    seed: int = 20260902,
    assumptions: Assumptions | None = None,
    new_segments: list[dict] | None = None,
) -> SimulationResult:
    """对未报有效保障与未支付案件做蒙特卡洛；默认含零件组共同冲击。"""
    assumptions = assumptions or reserve.assumptions
    rng = random.Random(seed)
    active_states = [
        state for state in book.segment_states()
        if state.cover_active and state.sale_id in set(reserve.active_segment_ids)
    ]
    severity = reserve.severity
    hazard = reserve.hazard
    components = sorted(severity.component_mix) or ["UNKNOWN"]
    component_weights = [severity.component_mix.get(item, 1.0 / len(components)) for item in components]
    unpaid = reserve.reported_unpaid_cents
    repeat_allowance = reserve.repeat_allowance_cents
    coverage = funding.coverage_for(reserve.scope)
    extra = new_segments or []

    spend_samples = []
    gap_samples = []
    hits_total = 0
    for _ in range(simulations):
        cost, hits = _simulate_path(
            active_states,
            severity,
            hazard,
            components,
            component_weights,
            rng,
            assumptions,
            common_mode=True,
            extra=extra,
            recurrence_rate_per_year=_recurrence_rate(reserve),
        )
        # 再次出险已在路径中按同一上限率抽样，不能把备抵再确定性加一遍。
        total = float(unpaid) + cost
        hits_total += hits
        spend_samples.append(total)
        gap_samples.append(max(0.0, total - coverage))
    independent_samples = []
    for _ in range(simulations):
        cost, _ = _simulate_path(
            active_states,
            severity,
            hazard,
            components,
            component_weights,
            rng,
            assumptions,
            common_mode=False,
            extra=extra,
            recurrence_rate_per_year=_recurrence_rate(reserve),
        )
        independent_samples.append(float(unpaid) + cost)
    spend_quantiles = _quantiles(spend_samples)
    gap_quantiles = _quantiles(gap_samples)
    shortfall = sum(1 for value in gap_samples if value > 0) / simulations if simulations else 0.0
    note = "尾部含共同冲击与费用波动；p90/p99 用于资金安全观察，不是精算置信声明"
    return SimulationResult(
        simulations=simulations,
        spend_quantiles_cents=spend_quantiles,
        gap_quantiles_cents=gap_quantiles,
        shortfall_probability=shortfall,
        common_mode_share=(hits_total / simulations) if simulations else 0.0,
        independent_expected_cents=int(sum(independent_samples) / len(independent_samples)) if independent_samples else 0,
        correlated_expected_cents=int(sum(spend_samples) / len(spend_samples)) if spend_samples else 0,
        note=note,
    )


def _recurrence_rate(reserve: ReserveComponents) -> float:
    detail = reserve.repeat_detail or {}
    return float(detail.get("upper_95_rate_per_year", 0.0) or 0.0)


def _simulate_new_segment(
    segment: dict,
    severity: SeverityModel,
    hazard: HazardModel,
    rng: random.Random,
    assumptions: Assumptions,
) -> float:
    """新批次按台数逐台抽样；每台在整段保障期内至多一次首次故障。"""
    months = float(segment.get("months", 12))
    units = int(segment.get("segments", 1) or 1)
    probability = hazard.remaining_claim_probability(segment.get("contract", "C1"), months)
    if segment.get("claim_probability") is not None:
        probability = float(segment["claim_probability"])
    mix = severity.action_mix.get(segment.get("contract", "C1")) or {"repair": 1.0}
    component = segment.get("component")
    price = severity.component_prices.get(component) if component else None
    total = 0.0
    for _ in range(units):
        if rng.random() >= probability:
            continue
        action = rng.choices(
            ["repair", "refund", "replacement"],
            weights=[mix.get(key, 0.0) or 0.0001 for key in ("repair", "refund", "replacement")],
            k=1,
        )[0]
        base = (
            price.repair_cost_cents
            if action == "repair" and price
            else severity.action_costs.get(action, severity.action_costs.get("repair", 0))
        )
        confirmed = price.confirmed_recovery if (price and action == "repair") else 0
        amount = base * math.exp(rng.gauss(0, assumptions.severity_log_sigma))
        total += max(0.0, amount - confirmed)
    return total


def portfolio(book: Book, ds: Dataset, assumptions: Assumptions | None = None) -> dict:
    """组合层准备金、资金与尾部一览。"""
    assumptions = assumptions or Assumptions()
    reserve = estimate_reserve(book, ds, "all", assumptions)
    funding = funding_view(ds, "all")
    gap = gap_view(reserve, funding)
    simulation = simulate(book, ds, reserve, funding, assumptions=assumptions)
    return {
        "reserve": reserve,
        "funding": funding,
        "gap": gap,
        "simulation": simulation,
    }
