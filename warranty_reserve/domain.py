"""保障段、申请与处置的领域状态。

本模块只依据资料契约中的明确关系判定：
- 是否在保以 claims.decision（售后认定）为准；合同期间与症状范围只做一致性核对，
  不一致时保留原认定并单列，不反向改写售后结论；
- 重复报修只在存在 previous_claim 且间隔不超过 repeat_days 时并入旧案；
- 退款/换机是否结束原保障由合同条款决定；
- 未结案不等于无故障，退款/换机也不删除已经发生的费用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .data import Dataset
from .timeutil import days_between, parse_ts, warranty_end

EXIT_ACTIONS = ("refund", "replacement")
OUTCOME_LABELS = {
    "no_claim": "观察期内无申报",
    "excluded": "确认不在保",
    "covered_repair": "在保维修",
    "covered_repeat": "同一问题重复维修",
    "covered_refund": "在保退款退出",
    "covered_replacement": "在保换机退出",
    "pending_repair": "待决维修",
    "awaiting_diagnosis": "已申报待诊断",
}


@dataclass
class ClaimView:
    claim: dict
    sale: dict
    contract: dict
    repair: dict | None
    # 售后认定（资料权威）与合同推导（一致性核对）分开保存。
    coverage_state: str  # covered / excluded / under_review
    contract_derived: str  # covered / excluded
    contract_check_ok: bool
    check_note: str
    recorded_decision: str
    is_repeat: bool
    repeat_of: str | None
    outcome: str
    paid_cents: int
    unpaid_cents: int
    open_case: bool
    exit_at: datetime | None
    source_ids: list[str] = field(default_factory=list)

    @property
    def claim_id(self) -> str:
        return self.claim["id"]

    @property
    def sale_id(self) -> str:
        return self.sale["id"]

    @property
    def decision_conflict(self) -> bool:
        """资料认定与合同推导不一致。"""
        return not self.contract_check_ok

    @property
    def eligible(self) -> bool:
        """申请未被售后排除（pending 视为待决，不当作不在保）。"""
        return self.coverage_state != "excluded"


@dataclass
class SegmentState:
    sale: dict
    contract: dict
    claims: list[ClaimView]
    warranty_start: datetime
    warranty_end: datetime
    exit_at: datetime | None
    exit_action: str | None
    cover_active: bool
    observed_days: int
    remaining_cover_days: int
    elapsed_cover_days: int
    observed_no_claim: bool
    cohort_complete: bool
    source_ids: list[str] = field(default_factory=list)

    @property
    def sale_id(self) -> str:
        return self.sale["id"]

    @property
    def batch(self) -> str:
        return self.sale["batch"]

    @property
    def device(self) -> str:
        return self.sale["device"]

    @property
    def contract_id(self) -> str:
        return self.sale["contract"]


def _primary_outcome(views: list[ClaimView]) -> str:
    """一段保障的主结局；重复维修不替代主事件，未结另行标记。"""
    priority = [
        "covered_refund",
        "covered_replacement",
        "excluded",
        "pending_repair",
        "covered_repair",
        "awaiting_diagnosis",
        "covered_repeat",
    ]
    present = {view.outcome for view in views}
    for outcome in priority:
        if outcome in present:
            return outcome
    return "excluded"


class Book:
    """截点下的完整业务视图。"""

    def __init__(self, dataset: Dataset):
        self.ds = dataset
        self.cutoff = dataset.cutoff
        self.segments: dict[str, SegmentState] = {}
        self.claims: dict[str, ClaimView] = {}
        self.overlaps: list[dict] = []
        self._build()
        self._detect_overlaps()

    def _detect_overlaps(self) -> None:
        """同一实物在不同保障段之间的期间重叠：后段不能替前段承接未来风险。"""
        for device, rows in self.ds.sales_by_device.items():
            ordered = sorted(rows, key=lambda row: row["sold_at"])
            for earlier, later in zip(ordered, ordered[1:]):
                early_state = self.segments.get(earlier["id"])
                if early_state is None:
                    continue
                if parse_ts(later["sold_at"]) < early_state.warranty_end:
                    overlap_end = min(early_state.warranty_end, parse_ts(later["sold_at"]))
                    self.overlaps.append(
                        {
                            "device": device,
                            "earlier_sale": earlier["id"],
                            "later_sale": later["id"],
                            "overlap_days": max(0, (overlap_end - parse_ts(later["sold_at"])).days),
                            "earlier_ended_by_exit": early_state.exit_at is not None,
                            "note": "后售段与前段保修期重叠；前段未来暴露与后段风险需分别计量",
                        }
                    )

    def _build(self) -> None:
        for sale in self.ds.sales:
            contract = self.ds.contracts[sale["contract"]]
            start = parse_ts(sale["sold_at"])
            end = warranty_end(sale["sold_at"], contract["months"])
            claim_rows = self.ds.claims_by_sale.get(sale["id"], [])
            claim_views = [self._claim_view(row, sale, contract) for row in claim_rows]
            exit_at, exit_action = self._exit(claim_views, contract)
            cover_end = min(end, exit_at) if exit_at else end
            cover_active = start <= self.cutoff < cover_end
            observed_end = min(self.cutoff, parse_ts(sale["observed_until"]), cover_end)
            observed_days = max(0, (observed_end - start).days)
            remaining = max(0, (cover_end - self.cutoff).days) if cover_active else 0
            elapsed = max(0, (self.cutoff - start).days)
            state = SegmentState(
                sale=sale,
                contract=contract,
                claims=claim_views,
                warranty_start=start,
                warranty_end=end,
                exit_at=exit_at,
                exit_action=exit_action,
                cover_active=cover_active,
                observed_days=observed_days,
                remaining_cover_days=remaining,
                elapsed_cover_days=elapsed,
                observed_no_claim=not claim_views,
                cohort_complete=bool(sale["cohort_complete"]),
                source_ids=[sale["id"]] + [view.claim_id for view in claim_views],
            )
            self.segments[sale["id"]] = state
            for view in claim_views:
                self.claims[view.claim_id] = view

    def _claim_view(self, row: dict, sale: dict, contract: dict) -> ClaimView:
        occurred = parse_ts(row["occurred_at"])
        end = warranty_end(sale["sold_at"], contract["months"])
        in_window = parse_ts(sale["sold_at"]) <= occurred < end
        covered_symptom = row["symptom"] in contract["covered_symptoms"]
        contract_derived = "covered" if (in_window and covered_symptom) else "excluded"

        repairs = self.ds.repairs_by_claim.get(row["id"], [])
        repair = repairs[0] if repairs else None
        repeat_of, is_repeat = self._repeat_of(row)
        decision = row["decision"]

        # 售后认定是资料权威；pending 表示尚未排除，不当作不在保。
        if decision == "covered":
            coverage_state = "covered"
        elif decision == "excluded":
            coverage_state = "excluded"
        else:
            coverage_state = "under_review"

        check_ok = contract_derived == ("excluded" if coverage_state == "excluded" else "covered")
        if check_ok:
            check_note = "资料认定与合同推导一致"
        else:
            detail = []
            if not in_window:
                detail.append("发生时间不在保障期")
            if not covered_symptom:
                detail.append("症状不在保障范围")
            check_note = (
                f"资料认定为 {decision}，合同推导为 {contract_derived}"
                + (f"（{'；'.join(detail)}）" if detail else "")
                + "；保留售后认定，列入资料一致性核对"
            )

        paid = sum(int(item["cost_cents"]) for item in repairs if item["paid"])
        unpaid = sum(int(item["cost_cents"]) for item in repairs if not item["paid"])
        open_case = row["closed_at"] is None

        # 退款/换机是按实物发生的处置：设备已退回或被换走，原保障即按合同结束，
        # 不论当时售后是否认定为在保；已发生费用仍保留，不因结束保障而消失。
        exit_at = None
        if (
            repair is not None
            and repair["action"] in EXIT_ACTIONS
            and contract.get(f"{repair['action']}_ends_cover", False)
        ):
            exit_at = parse_ts(repair["occurred_at"])

        # 结局按售后认定 + 已发生处置判定；未结只是状态，不改变已发生事实。
        if is_repeat and coverage_state != "excluded":
            outcome = "covered_repeat"
        elif coverage_state == "excluded":
            outcome = "excluded"
        elif repair is None:
            outcome = "awaiting_diagnosis"
        elif repair["action"] == "refund" and exit_at is not None:
            outcome = "covered_refund"
        elif repair["action"] == "replacement" and exit_at is not None:
            outcome = "covered_replacement"
        elif coverage_state == "under_review":
            outcome = "pending_repair"
        else:
            outcome = "covered_repair"

        return ClaimView(
            claim=row,
            sale=sale,
            contract=contract,
            repair=repair,
            coverage_state=coverage_state,
            contract_derived=contract_derived,
            contract_check_ok=check_ok,
            check_note=check_note,
            recorded_decision=decision,
            is_repeat=is_repeat,
            repeat_of=repeat_of,
            outcome=outcome,
            paid_cents=paid,
            unpaid_cents=unpaid,
            open_case=open_case,
            exit_at=exit_at,
            source_ids=[row["id"]] + ([repair["id"]] if repair else []),
        )

    def _repeat_of(self, row: dict) -> tuple[str | None, bool]:
        previous = row.get("previous_claim")
        if not previous or previous not in self.ds.claim_by_id:
            return None, False
        prior = self.ds.claim_by_id[previous]
        if prior["sale"] != row["sale"]:
            return previous, False
        gap = days_between(prior["occurred_at"], row["occurred_at"])
        within = gap <= int(
            self.ds.contracts[self.ds.sale_by_id[row["sale"]]["contract"]]["repeat_days"]
        )
        return previous, bool(within)

    def _exit(self, views: Iterable[ClaimView], contract: dict) -> tuple[datetime | None, str | None]:
        exits = [view for view in views if view.exit_at is not None]
        if not exits:
            return None, None
        first = min(exits, key=lambda view: view.exit_at)
        return first.exit_at, first.repair["action"]

    # ---- 汇总 -------------------------------------------------------
    def segment_states(self) -> list[SegmentState]:
        return sorted(self.segments.values(), key=lambda state: state.sale_id)

    def batches(self) -> list[str]:
        return sorted({state.batch for state in self.segments.values()})

    def batch_segments(self, batch: str) -> list[SegmentState]:
        return [state for state in self.segment_states() if state.batch == batch]

    def outcome_counts(self, segments: Iterable[SegmentState] | None = None) -> dict[str, int]:
        rows = list(segments if segments is not None else self.segment_states())
        counts = {key: 0 for key in OUTCOME_LABELS}
        for state in rows:
            if not state.claims:
                counts["no_claim"] += 1
                continue
            counts[_primary_outcome(state.claims)] += 1
        return counts

    def realized_paid(self, segments: Iterable[SegmentState] | None = None) -> int:
        rows = list(segments if segments is not None else self.segment_states())
        return sum(view.paid_cents for state in rows for view in state.claims)

    def outstanding_unpaid(self, segments: Iterable[SegmentState] | None = None) -> int:
        rows = list(segments if segments is not None else self.segment_states())
        return sum(view.unpaid_cents for state in rows for view in state.claims)

    def active_segments(self) -> list[SegmentState]:
        return [state for state in self.segment_states() if state.cover_active]

    def exited_segments(self) -> list[SegmentState]:
        return [
            state
            for state in self.segment_states()
            if state.exit_at is not None and state.exit_at <= self.cutoff
        ]

    def conflicts(self) -> list[ClaimView]:
        return [view for view in self.claims.values() if not view.contract_check_ok]


def outcome_table(book: Book) -> list[dict]:
    """按批次展示结局构成、暴露与已实现/未付金额。"""
    rows = []
    for batch in book.batches():
        segments = book.batch_segments(batch)
        counts = book.outcome_counts(segments)
        complete = [state for state in segments if state.cohort_complete]
        rows.append(
            {
                "batch": batch,
                "segments": len(segments),
                "complete_cohort": len(complete),
                "cover_active": sum(1 for state in segments if state.cover_active),
                "exited": sum(1 for state in segments if state.exit_at is not None),
                "open_cases": sum(1 for state in segments for view in state.claims if view.open_case),
                "observed_days": sum(state.observed_days for state in segments),
                "remaining_cover_days": sum(state.remaining_cover_days for state in segments),
                "outcomes": counts,
                "realized_paid_cents": book.realized_paid(segments),
                "unpaid_cents": book.outstanding_unpaid(segments),
                "contract_checks": sum(
                    1 for state in segments for view in state.claims if not view.contract_check_ok
                ),
            }
        )
    return rows
