"""月度认定、后续变化与充足性核对。

关键约束：
- 认定一旦冻结，保留原模型版本、价格来源、保障范围与截点；
- 后续新增事件必须给出有来源的释放或补提，不能直接改写旧认定；
- 同一保障不能被重复覆盖；
- 售后重开旧案时，保留原认定关联。
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from .data import Dataset
from .domain import Book
from .reserve import (
    MODEL_VERSION,
    Assumptions,
    FundingView,
    ReserveComponents,
    estimate_reserve,
    funding_view,
)


@dataclass
class Determination:
    """一次冻结的准备金认定。"""

    determination_id: str
    as_of: str
    scope: str
    model_version: str
    assumptions: dict
    price_sources: dict
    active_segment_ids: list[str]
    reported_unpaid_ids: list[str]
    gross_required_cents: int
    confirmed_recovery_cents: int
    unconfirmed_recovery_cents: int
    net_requirement_cents: int
    coverage_cents: int
    gap_cents: int
    evidence_limits: list[str]
    funding_snapshot: dict = field(default_factory=dict)
    reserve_snapshot: dict = field(default_factory=dict)
    frozen: bool = True

    @classmethod
    def from_reserve(cls, reserve: ReserveComponents, funding: FundingView, determination_id: str) -> "Determination":
        coverage = funding.coverage_for(reserve.scope)
        return cls(
            determination_id=determination_id,
            as_of=reserve.as_of,
            scope=reserve.scope,
            model_version=MODEL_VERSION,
            assumptions=reserve.assumptions.describe(),
            price_sources=dict(reserve.price_sources),
            active_segment_ids=list(reserve.active_segment_ids),
            reported_unpaid_ids=list(reserve.reported_unpaid_ids),
            gross_required_cents=reserve.gross_required_cents,
            confirmed_recovery_cents=reserve.confirmed_recovery_cents,
            unconfirmed_recovery_cents=reserve.unconfirmed_recovery_cents,
            net_requirement_cents=reserve.net_requirement_cents,
            coverage_cents=coverage,
            gap_cents=max(0, reserve.net_requirement_cents - coverage),
            evidence_limits=list(reserve.evidence_limits),
            funding_snapshot=funding.to_dict(),
            reserve_snapshot=reserve.to_dict(),
        )

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    def key(self) -> str:
        return self.determination_id


@dataclass
class SubsequentChange:
    """认定之后发生的变化，必须带来源。"""

    kind: str  # release / top_up
    amount_cents: int
    reason: str
    source_ids: list[str]
    related_determination: str
    relates_to_segment: str | None = None
    double_count_guard: str | None = None

    def __post_init__(self):
        if self.amount_cents < 0:
            raise ValueError("变化金额必须为非负；释放与补提用 kind 区分")
        if not self.source_ids:
            raise ValueError("后续变化必须有资料或重新认定来源")
        if self.kind not in ("release", "top_up"):
            raise ValueError("kind 只能是 release 或 top_up")


@dataclass
class ReopenedCase:
    claim_id: str
    original_claim_id: str
    original_determination: str
    reason: str
    detected_at: str
    outcome_before: str
    outcome_after: str
    amount_before_cents: int
    amount_after_cents: int

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class FundingChange:
    """认定之后的资金事实变化；带来源，避免继续引用过期余额。"""

    kind: str  # new_commitment / release_commitment / new_available
    amount_cents: int
    scope: str
    reason: str
    source_ids: list[str]
    detected_at: str

    def __post_init__(self):
        if self.amount_cents < 0:
            raise ValueError("资金变化金额必须非负")
        if not self.source_ids:
            raise ValueError("资金变化必须有来源")
        if self.kind not in ("new_commitment", "release_commitment", "new_available"):
            raise ValueError("kind 只能是 new_commitment / release_commitment / new_available")


@dataclass
class MonthEnd:
    determination: Determination
    changes: list[SubsequentChange] = field(default_factory=list)
    reopened: list[ReopenedCase] = field(default_factory=list)
    funding_changes: list[FundingChange] = field(default_factory=list)

    def net_change_cents(self) -> int:
        release = sum(item.amount_cents for item in self.changes if item.kind == "release")
        top_up = sum(item.amount_cents for item in self.changes if item.kind == "top_up")
        return top_up - release

    def carried_requirement_cents(self) -> int:
        """承接原认定 + 有来源变化；不重算旧模型。"""
        return max(0, self.determination.net_requirement_cents + self.net_change_cents())

    def coverage_after_cents(self, funding: FundingView) -> int:
        """原始覆盖额 + 带来源的资金变化；不沿用冻结快照里的过期余额。"""
        base = self.determination.coverage_cents
        target = self.determination.scope
        for change in self.funding_changes:
            own_scope = _same_scope(change.scope, target)
            if change.kind == "new_commitment":
                # 共享池：本范围的新占用与余额减少相抵；其他范围占用只减少共享余额，
                # 本范围的“余额 + 本范围占用”随之下降。
                if not own_scope:
                    base -= change.amount_cents
            elif change.kind == "release_commitment":
                if not own_scope:
                    base += change.amount_cents
            elif change.kind == "new_available":
                base += change.amount_cents
        return max(0, base)

    def stale_funding_warning(self, funding: FundingView) -> str | None:
        snapshot = self.determination.funding_snapshot or {}
        if not snapshot:
            return None
        before = int(snapshot.get("available_cents", 0)) - int(snapshot.get("committed_cents", 0))
        after = funding.available_cents - funding.committed_cents
        if before != after and not self.funding_changes:
            return (
                f"资金事实自认定后变化（{before/100:.2f} → {after/100:.2f} 元），"
                "但未登记带来源的资金变化；不能继续使用冻结时的余额口径"
            )
        return None

    def adequacy(self, funding: FundingView) -> dict:
        carried = self.carried_requirement_cents()
        coverage = self.coverage_after_cents(funding)
        return {
            "determination": self.determination.determination_id,
            "as_of": self.determination.as_of,
            "frozen_model": self.determination.model_version,
            "carried_requirement_cents": carried,
            "coverage_cents": coverage,
            "gap_cents": max(0, carried - coverage),
            "net_change_cents": self.net_change_cents(),
            "changes": [change.__dict__ for change in self.changes],
            "reopened_cases": [case.to_dict() for case in self.reopened],
            "funding_changes": [change.__dict__ for change in self.funding_changes],
            "stale_funding_warning": self.stale_funding_warning(funding),
            "note": "承接认定不改写原冻结口径；新增事实以释放/补提单列",
        }


def freeze_determination(
    book: Book,
    ds: Dataset,
    scope: str = "all",
    determination_id: str | None = None,
    assumptions: Assumptions | None = None,
) -> MonthEnd:
    reserve = estimate_reserve(book, ds, scope, assumptions)
    funding = funding_view(ds, scope)
    determination = Determination.from_reserve(
        reserve, funding, determination_id or f"R-{reserve.as_of[:10]}-{scope}"
    )
    return MonthEnd(determination=determination)


def _same_scope(left: str, right: str) -> bool:
    """范围归属；all 不是通配，避免把其他批次占用误当本范围。"""
    return left == right


def _double_count(change: SubsequentChange, determination: Determination) -> bool:
    """释放不得引用仍受原认定覆盖的保障或已计入的未付维修。

    补提用于增加要求，允许引用已有未付维修（例如金额上调），
    但必须显式带来源，不能借补提掩盖重复覆盖。
    """
    sources = set(change.source_ids)
    if change.kind != "release":
        return False
    if change.relates_to_segment in determination.active_segment_ids:
        return True
    if sources & set(determination.reported_unpaid_ids):
        return True
    return False


def apply_change(month: MonthEnd, change: SubsequentChange) -> None:
    if _double_count(change, month.determination):
        raise ValueError("该变化引用仍受原认定覆盖的保障或已计入的未付维修，不能重复释放/补提")
    if any(
        item.kind == change.kind
        and item.related_determination == change.related_determination
        and set(item.source_ids) & set(change.source_ids)
        for item in month.changes
    ):
        raise ValueError("同一来源已登记过同类变化，不能重复释放或补提")
    month.changes.append(change)


def reopen_case(
    month: MonthEnd,
    original: dict,
    reopened: dict,
    detected_at: str,
    reason: str,
) -> ReopenedCase:
    """售后重开旧案：新记录显式指向原认定，不覆盖原认定金额。"""
    if any(item.original_claim_id == original["id"] for item in month.reopened):
        raise ValueError(f"原案 {original['id']} 已重开过；再次变化应以新的补提来源登记，不能重复挂接")
    before = _claim_amount(original)
    after = _claim_amount(reopened)
    case = ReopenedCase(
        claim_id=reopened["id"],
        original_claim_id=original["id"],
        original_determination=month.determination.determination_id,
        reason=reason,
        detected_at=detected_at,
        outcome_before=original.get("decision", "unknown"),
        outcome_after=reopened.get("decision", "unknown"),
        amount_before_cents=before,
        amount_after_cents=after,
    )
    month.reopened.append(case)
    if after > before:
        apply_change(
            month,
            SubsequentChange(
                kind="top_up",
                amount_cents=after - before,
                reason=f"重开旧案 {original['id']} → {reopened['id']}：{reason}",
                source_ids=[reopened["id"]],
                related_determination=month.determination.determination_id,
                relates_to_segment=reopened.get("sale"),
            ),
        )
    return case


def _claim_amount(claim: dict) -> int:
    return int(claim.get("amount_cents") or claim.get("cost_cents") or 0)


def save_month(path: pathlib.Path, month: MonthEnd, funding: FundingView) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "determination": month.determination.to_dict(),
        "changes": [change.__dict__ for change in month.changes],
        "reopened_cases": [case.to_dict() for case in month.reopened],
        "funding_changes": [change.__dict__ for change in month.funding_changes],
        "adequacy": month.adequacy(funding),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_month(path: pathlib.Path) -> MonthEnd:
    """从已保存认定继续承接；冻结的原模型与价格保持只读。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    determination = Determination(**{
        key: value for key, value in payload["determination"].items() if key in Determination.__dataclass_fields__
    })
    changes = [
        SubsequentChange(**{
            key: value for key, value in row.items() if key in SubsequentChange.__dataclass_fields__
        })
        for row in payload.get("changes", [])
    ]
    reopened = [
        ReopenedCase(**{key: value for key, value in row.items() if key in ReopenedCase.__dataclass_fields__})
        for row in payload.get("reopened_cases", [])
    ]
    funding_changes = [
        FundingChange(**{
            key: value for key, value in row.items() if key in FundingChange.__dataclass_fields__
        })
        for row in payload.get("funding_changes", [])
    ]
    return MonthEnd(
        determination=determination,
        changes=changes,
        reopened=reopened,
        funding_changes=funding_changes,
    )


def compare_determinations(previous: Determination, current: Determination) -> dict:
    released = max(0, previous.net_requirement_cents - current.net_requirement_cents)
    top_up = max(0, current.net_requirement_cents - previous.net_requirement_cents)
    moved = sorted(set(previous.active_segment_ids) ^ set(current.active_segment_ids))
    return {
        "previous": previous.determination_id,
        "current": current.determination_id,
        "net_change_cents": current.net_requirement_cents - previous.net_requirement_cents,
        "release_cents": released,
        "top_up_cents": top_up,
        "segment_membership_changes": moved,
        "price_source_changes": {
            key: [previous.price_sources.get(key), current.price_sources.get(key)]
            for key in sorted(set(previous.price_sources) | set(current.price_sources))
            if previous.price_sources.get(key) != current.price_sources.get(key)
        },
        "note": "跨月比较只作管理解释；正式承接以冻结认定 + 有来源变化为准",
    }
