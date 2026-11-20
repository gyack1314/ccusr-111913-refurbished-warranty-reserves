"""新批次接收情景：增量风险、资金占用与现金缺口比较。

情景只回答“若按这些假设接收，准备金与现金安全如何变化”，
不构成报价建议或财务合规结论。所有情景共用同一份资金事实，
组合比较时不允许重复认领共享余额。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .data import Dataset
from .domain import Book
from .reserve import (
    Assumptions,
    estimate_reserve,
    funding_view,
    simulate,
)
from .timeutil import warranty_end


@dataclass
class NewBatch:
    name: str
    segments: int
    kind: str
    age_years: int
    contract: str
    sold_at: str
    component: str | None = None

    def months(self, ds: Dataset) -> int:
        return int(ds.contracts[self.contract]["months"])

    def warranty_end(self, ds: Dataset) -> str:
        return warranty_end(self.sold_at, self.months(ds)).isoformat()

    def as_segment(self, ds: Dataset) -> dict:
        return {
            "name": self.name,
            "contract": self.contract,
            "months": self.months(ds),
            "component": self.component,
            "segments": self.segments,
            "kind": self.kind,
            "age_years": self.age_years,
        }


@dataclass
class ScenarioResult:
    name: str
    new_batch: NewBatch
    incremental_ibnr_cents: int
    incremental_tail_p90_cents: int
    incremental_tail_p99_cents: int
    incremental_repeat_allowance_cents: int
    incremental_gross_cents: int
    confirmed_recovery_cents: int
    net_incremental_cents: int
    funding_before_cents: int
    funding_after_cents: int
    gap_before_cents: int
    gap_after_cents: int
    shortfall_probability: float
    assumptions_note: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scenario": self.name,
            "batch": {
                "segments": self.new_batch.segments,
                "kind": self.new_batch.kind,
                "age_years": self.new_batch.age_years,
                "contract": self.new_batch.contract,
                "sold_at": self.new_batch.sold_at,
                "component": self.new_batch.component,
            },
            "incremental_ibnr_cents": self.incremental_ibnr_cents,
            "incremental_repeat_allowance_cents": self.incremental_repeat_allowance_cents,
            "incremental_gross_cents": self.incremental_gross_cents,
            "confirmed_recovery_cents": self.confirmed_recovery_cents,
            "net_incremental_cents": self.net_incremental_cents,
            "tail_p90_cents": self.incremental_tail_p90_cents,
            "tail_p99_cents": self.incremental_tail_p99_cents,
            "funding_before_cents": self.funding_before_cents,
            "funding_after_cents": self.funding_after_cents,
            "gap_before_cents": self.gap_before_cents,
            "gap_after_cents": self.gap_after_cents,
            "shortfall_probability": round(self.shortfall_probability, 3),
            "assumption_notes": self.assumptions_note,
        }


def _unit_expected_cost(contract: str, severity, component: str | None) -> tuple[float, float]:
    """返回单台期望成本与其中只在维修动作下确认的追偿额。"""
    mix = severity.action_mix.get(contract) or {"repair": 1.0, "refund": 0.0, "replacement": 0.0}
    groups = {component: 1.0} if component is not None else severity.component_mix
    total = 0.0
    recovery = 0.0
    for group, weight in groups.items():
        price = severity.component_prices.get(group)
        repair = price.repair_cost_cents if price else severity.action_costs.get("repair", 0)
        refund = severity.action_costs.get("refund", repair)
        replacement = severity.action_costs.get("replacement", repair)
        total += weight * (
            mix.get("repair", 0.0) * repair
            + mix.get("refund", 0.0) * refund
            + mix.get("replacement", 0.0) * replacement
        )
        if price is not None:
            recovery += weight * mix.get("repair", 0.0) * price.confirmed_recovery
    return total, recovery


def compare_new_batches(
    book: Book,
    ds: Dataset,
    batches: list[NewBatch],
    assumptions: Assumptions | None = None,
    simulations: int = 2000,
) -> list[ScenarioResult]:
    assumptions = assumptions or Assumptions()
    base_reserve = estimate_reserve(book, ds, "all", assumptions)
    funding = funding_view(ds, "all")
    base_sim = simulate(book, ds, base_reserve, funding, simulations=simulations, assumptions=assumptions)
    results = []
    for batch in batches:
        months = batch.months(ds)
        probability = base_reserve.hazard.remaining_claim_probability(batch.contract, months)
        unit_cost, unit_recovery = _unit_expected_cost(
            batch.contract, base_reserve.severity, batch.component
        )
        ibnr = probability * unit_cost * batch.segments
        recovery = probability * unit_recovery * batch.segments
        # 新批次尚无维修记录；按每段 1% 的弱先验上限计提重复备抵。
        repeat = batch.segments * 0.01 * base_reserve.severity.action_costs.get("repair", 0)
        gross = ibnr + repeat
        net_incremental = max(0.0, gross - recovery)

        # 单个方案可动用共享未占用余额；多个方案并列时各自不能重复认领。
        coverage = funding.coverage_for(f"scenario_{batch.name}")
        gap_before = max(0, base_reserve.net_requirement_cents - coverage)
        gap_after = max(0, base_reserve.net_requirement_cents + int(round(net_incremental)) - coverage)
        scenario_sim = simulate(
            book,
            ds,
            base_reserve,
            funding,
            simulations=simulations,
            assumptions=assumptions,
            new_segments=[batch.as_segment(ds)],
        )
        notes = []
        if probability < 0.05:
            notes.append(
                f"借用合同风险率得到的剩余期出险概率仅 {probability:.1%}，该批次在此口径下接近零增量；"
                "证据不足时不得解读为安全"
            )
        if batch.segments < 8:
            notes.append(f"新批次仅 {batch.segments} 段，风险率借用合同/机龄基准，不单独拟合")
        if batch.component:
            notes.append("同组零件若存在批次共同缺陷，多台会同时出险；尾部不可按独立事件解读")
        if gross == 0:
            notes.append("按现有完整名册的合同风险率，该批次剩余期望接近零；证据不足不代表安全")
        results.append(
            ScenarioResult(
                name=batch.name,
                new_batch=batch,
                incremental_ibnr_cents=int(round(ibnr)),
                incremental_repeat_allowance_cents=int(round(repeat)),
                incremental_gross_cents=int(round(gross)),
                confirmed_recovery_cents=int(round(recovery)),
                net_incremental_cents=int(round(net_incremental)),
                funding_before_cents=coverage,
                funding_after_cents=coverage,
                gap_before_cents=gap_before,
                gap_after_cents=gap_after,
                incremental_tail_p90_cents=max(
                    0, scenario_sim.spend_quantiles_cents["p90"] - base_sim.spend_quantiles_cents["p90"]
                ),
                incremental_tail_p99_cents=max(
                    0, scenario_sim.spend_quantiles_cents["p99"] - base_sim.spend_quantiles_cents["p99"]
                ),
                shortfall_probability=scenario_sim.shortfall_probability,
                assumptions_note=notes,
            )
        )
    return results


def compare_portfolio_claims(results: list[ScenarioResult], funding) -> dict:
    """组合层检查：多个新批次同时接收时共享余额只能认领一次。"""
    total = sum(result.net_incremental_cents for result in results)
    available = funding.free_cents
    return {
        "scenarios": len(results),
        "combined_net_incremental_cents": total,
        "shared_free_balance_cents": available,
        "combined_gap_cents": max(0, total - available),
        "note": "各方案单独看可能都有覆盖；同时接收时共享余额只能认领一次，缺口按组合重算",
    }
