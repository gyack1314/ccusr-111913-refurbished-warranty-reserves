"""资料装载、时点可见性与价格证据选择。

所有分析只使用 known_at/reported_at 在截点之前可见的记录；不修改 data/。
"""
from __future__ import annotations

import pathlib
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from .timeutil import CUTOFF_DEFAULT, parse_ts

DATA_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_materials(root: pathlib.Path | None = None):
    from support.inspect_data import load

    return load(root or DATA_ROOT)


def _date(value: str) -> date:
    return date.fromisoformat(value[:10])


@dataclass(frozen=True)
class PriceEvidence:
    """某一零件组在截点可用的维修价与追偿证据。"""

    component: str
    repair_cost_cents: int
    source_id: str
    basis: str
    recovery_cents: int = 0
    recovery_confirmed: bool = False
    valid_from: date | None = None
    valid_to: date | None = None

    @property
    def confirmed_recovery(self) -> int:
        return self.recovery_cents if self.recovery_confirmed else 0

    @property
    def unconfirmed_recovery(self) -> int:
        return 0 if self.recovery_confirmed else self.recovery_cents


class Dataset:
    """按截点裁剪后的只读资料视图。"""

    def __init__(self, manifest: dict[str, Any], tables: dict[str, list[dict]], cutoff: str | None = None):
        self.manifest = manifest
        self.version = manifest.get("version", "unknown")
        self.cutoff = parse_ts(cutoff or CUTOFF_DEFAULT)
        self.cutoff_text = self.cutoff.isoformat()
        self.raw = tables
        self.contracts: dict[str, dict] = {row["id"]: row for row in tables["contracts"]}

        self.sales = sorted(
            (row for row in tables["sales"] if parse_ts(row["known_at"]) <= self.cutoff),
            key=lambda row: (row["sold_at"], row["id"]),
        )
        self.sale_by_id = {row["id"]: row for row in self.sales}

        self.claims = sorted(
            (
                row
                for row in tables["claims"]
                if parse_ts(row["reported_at"]) <= self.cutoff and row["sale"] in self.sale_by_id
            ),
            key=lambda row: (row["occurred_at"], row["id"]),
        )
        self.claim_by_id = {row["id"]: row for row in self.claims}

        self.repairs = sorted(
            (
                row
                for row in tables["repairs"]
                if parse_ts(row["known_at"]) <= self.cutoff and row["claim"] in self.claim_by_id
            ),
            key=lambda row: (row["occurred_at"], row["id"]),
        )
        self.repairs_by_claim: dict[str, list[dict]] = {}
        for row in self.repairs:
            self.repairs_by_claim.setdefault(row["claim"], []).append(row)

        self.claims_by_sale: dict[str, list[dict]] = {}
        for row in self.claims:
            self.claims_by_sale.setdefault(row["sale"], []).append(row)

        self.sales_by_device: dict[str, list[dict]] = {}
        for row in self.sales:
            self.sales_by_device.setdefault(row["device"], []).append(row)

        self.funds = sorted(
            (row for row in tables["funds"] if parse_ts(row["known_at"]) <= self.cutoff),
            key=lambda row: (row["known_at"], row["id"]),
        )
        self.contract_rows = [row for row in tables["contracts"]]
        self.cost_rows = sorted(
            (row for row in tables["costs"] if parse_ts(row["known_at"]) <= self.cutoff),
            key=lambda row: (row["valid_from"], row["id"]),
        )

    # ---- 金额与范围 -------------------------------------------------
    @property
    def available_funds(self) -> int:
        return sum(int(row["cents"]) for row in self.funds if row["kind"] == "available")

    @property
    def commitments(self) -> list[dict]:
        return [row for row in self.funds if row["kind"] == "other_commitment"]

    def commitment_total(self, scope: str = "all") -> int:
        return sum(int(row["cents"]) for row in self.commitments if _scope_matches(row["scope"], scope))

    def free_funds(self, scope: str = "all") -> int:
        """全局可认领余额 = 总可用 − 已占用。

        所有范围共享这一余额；范围只影响“已有占用”的归属展示，
        不允许每个方案各自再领一次同一笔余额。
        """
        committed = sum(int(row["cents"]) for row in self.commitments)
        return max(0, self.available_funds - committed)

    # ---- 价格与追偿 -------------------------------------------------
    def cost_row_for(self, component: str, at: date) -> dict | None:
        candidates = [
            row
            for row in self.cost_rows
            if row["component_group"] == component
            and _date(row["valid_from"]) <= at < _date(row["valid_to"])
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: (row["valid_from"], row["known_at"]))

    def price_for(self, component: str, at: date | None = None) -> PriceEvidence | None:
        at = at or self.cutoff.date()
        row = self.cost_row_for(component, at)
        if row is not None:
            return PriceEvidence(
                component=component,
                repair_cost_cents=int(row["repair_cost_cents"]),
                source_id=row["id"],
                basis="cost_table",
                recovery_cents=int(row["recovery_cents"]),
                recovery_confirmed=bool(row["recovery_confirmed"]),
                valid_from=_date(row["valid_from"]),
                valid_to=_date(row["valid_to"]),
            )
        # 截点尚无标准价时，用截点前可见的实际维修价作为原价格依据。
        actual = [
            int(repair["cost_cents"])
            for repair in self.repairs
            if repair["component_group"] == component
            and repair["action"] == "repair"
            and repair["paid"]
            and _date(repair["occurred_at"]) <= at
        ]
        if actual:
            return PriceEvidence(
                component=component,
                repair_cost_cents=int(statistics.median(actual)),
                source_id=f"empirical:{component}",
                basis="empirical_paid_repair",
                recovery_cents=0,
                recovery_confirmed=False,
            )
        return None

    def repair_prices(self) -> dict[str, PriceEvidence]:
        components = sorted({row["component_group"] for row in self.cost_rows} | {
            repair["component_group"] for repair in self.repairs
        })
        result = {}
        for component in components:
            price = self.price_for(component)
            if price is not None:
                result[component] = price
        return result

    # ---- 基础统计 ---------------------------------------------------
    def table_counts(self) -> dict[str, int]:
        return {
            "sales": len(self.sales),
            "claims": len(self.claims),
            "repairs": len(self.repairs),
            "contracts": len(self.contract_rows),
            "costs": len(self.cost_rows),
            "funds": len(self.funds),
        }

    def raw_counts(self) -> dict[str, int]:
        return {name: len(rows) for name, rows in self.raw.items()}


def _scope_matches(scope: str, target: str) -> bool:
    if scope in ("all", "*"):
        return True
    if target in ("all", "*"):
        return True
    if scope == target:
        return True
    if scope.startswith("batch_") and target.startswith("batch_"):
        return scope == target
    return False


def load_dataset(root: pathlib.Path | None = None, cutoff: str | None = None) -> Dataset:
    # root 是包含 data/ 的项目根目录；新增资料可放在独立项目根，不覆盖历史原始文件。
    manifest, tables = _load_materials(root)
    return Dataset(manifest, tables, cutoff)


def is_known(record: dict, field: str, cutoff: datetime) -> bool:
    return parse_ts(record[field]) <= cutoff


def filter_known(rows: Iterable[dict], field: str, cutoff: datetime) -> list[dict]:
    return [row for row in rows if is_known(row, field, cutoff)]
