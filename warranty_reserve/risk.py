"""风险分析：可解释的批次基准、暴露度量与向后滚动验证。

原则：
- 事件单位是保障段而不是工单行；同一保障段内已确认关联的重复维修不另计首次故障。
- 风险分母只用观察完整的名册（cohort_complete=true）；转来的不完整资料单列。
- 目标以销售保障段的实际观察时长计暴露，不把尚未到来的期间算成无故障。
- 验证按时间向后滚动，并按实物身份隔离，同一设备的重售不跨段泄漏。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .domain import Book, SegmentState
from .timeutil import add_days, days_between, parse_ts, percentile

DAY = 86400.0


@dataclass(frozen=True)
class Stratum:
    contract: str
    kind: str
    age_band: str

    def label(self) -> str:
        return f"{self.contract}/{self.kind}/{self.age_band}"


@dataclass
class RiskEstimate:
    stratum: Stratum
    segments: int
    exposure_days: int
    first_failure_events: int
    repeat_events: int
    exit_events: int
    pending_events: int
    observed_no_claim: int
    empirical_rate_per_year: float | None
    gated: bool
    gate_reason: str | None
    interval: tuple[float, float] | None = None

    @property
    def rate_per_year(self) -> float | None:
        return self.empirical_rate_per_year

    def as_dict(self) -> dict:
        return {
            "stratum": self.stratum.label(),
            "segments": self.segments,
            "exposure_years": round(self.exposure_days / 365.25, 3),
            "first_failure_events": self.first_failure_events,
            "repeat_events": self.repeat_events,
            "exit_events": self.exit_events,
            "pending_events": self.pending_events,
            "no_claim_segments": self.observed_no_claim,
            "rate_per_segment_year": None if self.empirical_rate_per_year is None else round(self.empirical_rate_per_year, 4),
            "poisson_95_interval": None
            if self.interval is None
            else [round(self.interval[0], 4), round(self.interval[1], 4)],
            "gated": self.gated,
            "gate_reason": self.gate_reason,
        }


def age_band(age_years: int) -> str:
    if age_years <= 3:
        return "2-3年"
    if age_years <= 6:
        return "4-6年"
    return "7年以上"


def _rate_interval(events: int, exposure_days: int) -> tuple[float, float] | None:
    """Poisson 精确 95% 区间；无暴露时返回 None。"""
    if exposure_days <= 0:
        return None
    years = exposure_days / 365.25
    if events == 0:
        return (0.0, 3.689 / years)
    try:
        from scipy.stats import chi2

        lo = chi2.ppf(0.025, 2 * events) / 2 / years
        hi = chi2.ppf(0.975, 2 * (events + 1)) / 2 / years
        return (lo, hi)
    except Exception:  # pragma: no cover - scipy 缺失时的保守近似
        lo = max(0.0, (events - 1.96 * math.sqrt(events)) / years)
        hi = (events + 1.96 * math.sqrt(events)) / years
        return (lo, hi)


def segment_events(state: SegmentState) -> tuple[int, int, int, int]:
    """(首次故障, 重复维修, 退出保障, 未结或待诊断) —— 每段至多各计一次。"""
    first = repeat = exit_count = pending = 0
    for view in state.claims:
        if view.outcome == "covered_repeat":
            # 重复维修以首次故障已发生为前提，不另算首次。
            repeat = 1
            continue
        if view.outcome in (
            "covered_repair",
            "covered_refund",
            "covered_replacement",
            "pending_repair",
        ):
            # 已发生维修/退换即计为一次故障；未决不等于无故障。
            first = 1
        if view.outcome in ("covered_refund", "covered_replacement"):
            exit_count = 1
        if view.outcome in ("awaiting_diagnosis", "pending_repair"):
            pending = 1
    return first, repeat, exit_count, pending


def build_estimates(book: Book, complete_only: bool = True) -> list[RiskEstimate]:
    groups: dict[Stratum, list[SegmentState]] = {}
    for state in book.segment_states():
        if complete_only and not state.cohort_complete:
            continue
        key = Stratum(state.contract_id, state.sale["kind"], age_band(int(state.sale["age_years"])))
        groups.setdefault(key, []).append(state)

    estimates = []
    for stratum, states in sorted(groups.items(), key=lambda item: item[0].label()):
        exposure = sum(state.observed_days for state in states)
        first = repeat = exits = pending = no_claim = 0
        for state in states:
            f, r, e, p = segment_events(state)
            first += f
            repeat += r
            exits += e
            pending += p
            if not state.claims:
                no_claim += 1
        events = first + repeat
        gated = len(states) < 8 or exposure < 60
        reason = None
        if gated:
            if len(states) < 8:
                reason = f"有效保障段仅 {len(states)} 条，不足以形成批次结论"
            else:
                reason = "总观察不足 60 个保障段日"
        rate = (events / (exposure / 365.25)) if exposure > 0 else None
        interval = _rate_interval(events, exposure)
        estimates.append(
            RiskEstimate(
                stratum=stratum,
                segments=len(states),
                exposure_days=exposure,
                first_failure_events=first,
                repeat_events=repeat,
                exit_events=exits,
                pending_events=pending,
                observed_no_claim=no_claim,
                empirical_rate_per_year=rate,
                gated=gated,
                gate_reason=reason,
                interval=interval,
            )
        )
    return estimates


def pooled_rate(estimates: Iterable[RiskEstimate]) -> float | None:
    exposure = sum(item.exposure_days for item in estimates)
    events = sum(item.first_failure_events + item.repeat_events for item in estimates)
    if exposure <= 0:
        return None
    return events / (exposure / 365.25)


def shrunk_rate(item: RiskEstimate, pooled: float | None, prior_strength_years: float = 4.0) -> float | None:
    """向池化率收缩的批次率；强度以年计，避免小样本极端值。"""
    if item.empirical_rate_per_year is None:
        return pooled
    if pooled is None:
        return item.empirical_rate_per_year
    exposure_years = item.exposure_days / 365.25
    if exposure_years <= 0:
        return pooled
    weight = exposure_years / (exposure_years + prior_strength_years)
    return weight * item.empirical_rate_per_year + (1 - weight) * pooled


def batch_view(book: Book) -> list[dict]:
    """按批次汇总风险构成与解释性基准。"""
    pooled = pooled_rate(build_estimates(book))
    rows = []
    for batch in book.batches():
        states = book.batch_segments(batch)
        complete = [state for state in states if state.cohort_complete]
        exposure = sum(state.observed_days for state in complete)
        first = sum(segment_events(state)[0] for state in complete)
        repeat = sum(segment_events(state)[1] for state in complete)
        exits = sum(segment_events(state)[2] for state in complete)
        pending = sum(segment_events(state)[3] for state in complete)
        no_claim = sum(1 for state in complete if not state.claims)
        rate = ((first + repeat) / (exposure / 365.25)) if exposure > 0 else None
        interval = _rate_interval(first + repeat, exposure)
        rows.append(
            {
                "batch": batch,
                "segments": len(states),
                "complete_cohort": len(complete),
                "incomplete_excluded": len(states) - len(complete),
                "exposure_years": round(exposure / 365.25, 3),
                "first_failure": first,
                "repeat_repair": repeat,
                "exits": exits,
                "pending": pending,
                "no_claim": no_claim,
                "rate_per_segment_year": None if rate is None else round(rate, 4),
                "poisson_95_interval": None
                if interval is None
                else [round(interval[0], 4), round(interval[1], 4)],
                "shrunk_rate": None
                if (shrunk := shrunk_rate(
                    RiskEstimate(
                        stratum=Stratum("", "", ""),
                        segments=len(complete),
                        exposure_days=exposure,
                        first_failure_events=first,
                        repeat_events=repeat,
                        exit_events=exits,
                        pending_events=pending,
                        observed_no_claim=no_claim,
                        empirical_rate_per_year=rate,
                        gated=False,
                        gate_reason=None,
                    ),
                    pooled,
                ))
                is None
                else round(shrunk, 4),
                "evidence_limit": "完整名册" if len(complete) == len(states) else f"{len(states) - len(complete)} 段转来资料不并入分母",
                "retained_limit": len(complete) < 8 or exposure < 60,
            }
        )
    return rows


# ---- 向后滚动验证 ---------------------------------------------------
@dataclass
class RollingPoint:
    origin: str
    train_segments: int
    test_segments: int
    predicted_rate: float | None
    actual_rate: float | None
    predicted_events: float | None
    actual_events: int
    excluded_devices: int


def rolling_validation(book: Book, origins: list[str] | None = None) -> list[RollingPoint]:
    """按销售时点向后推进；测试段与训练段按实物身份隔离。

    每个切点只使用切点前已申报的资料训练，在切点后新售出的保障段上
    比较“销售后 180 天内首次故障”的预测与实际，避免用未来申报泄漏。
    训练标签按切点时刻的已知状态、以销售后月龄计暴露，不把退款/换机后的
    期间算成继续观察。
    """
    segments = sorted(book.segment_states(), key=lambda state: state.sale["sold_at"])
    if not segments:
        return []
    starts = [parse_ts(state.sale["sold_at"]) for state in segments]
    lo, hi = min(starts), max(starts)
    horizon_days = 180
    if origins is None:
        points = []
        cursor = add_days(lo, horizon_days)
        while cursor <= hi:
            points.append(cursor.isoformat())
            cursor = add_days(cursor, 30)
        origins = points
    output = []
    for origin_text in origins:
        origin = parse_ts(origin_text)
        # 切点前已售出、且到切点已有至少 180 天观察的保障段才进入训练。
        train = [
            state
            for state in segments
            if add_days(state.sale["sold_at"], horizon_days) <= origin
        ]
        if not train:
            continue
        train_exposure = 0
        train_events = 0
        for state in train:
            # 训练标签只取切点前已知的申报；退款/换机结束观察，不把空档算作无故障。
            cut = min(add_days(state.sale["sold_at"], horizon_days), book.cutoff)
            if state.exit_at is not None:
                cut = min(cut, state.exit_at)
            exposure = max(0, min(horizon_days, days_between(state.sale["sold_at"], cut)))
            train_exposure += exposure
            train_events += sum(
                1
                for view in state.claims
                if parse_ts(view.claim["occurred_at"]) <= cut
                and parse_ts(view.claim["reported_at"]) <= origin
                and view.outcome != "excluded"
            )
        # 训练侧不足时不出点，避免一条早退出保障段把率放大成噪声。
        if len(train) < 20 or train_exposure < 365.25 * 5:
            continue
        predicted = (train_events / (train_exposure / 365.25)) if train_exposure > 0 else None

        used_devices = {state.device for state in train}
        cohort = [
            state
            for state in segments
            if parse_ts(state.sale["sold_at"]) >= origin
            and parse_ts(state.sale["sold_at"]) < add_days(origin, 90)
        ]
        test = [state for state in cohort if state.device not in used_devices]
        # 实际结果只用切点后销售且 180 天前向窗口内可观察的部分；不读取未来申报。
        actual_exposure = 0
        actual_events = 0
        for state in test:
            label_end = min(
                add_days(state.sale["sold_at"], horizon_days),
                parse_ts(state.sale["observed_until"]),
                book.cutoff,
            )
            exposure = max(0, days_between(state.sale["sold_at"], label_end))
            actual_exposure += exposure
            actual_events += sum(
                1
                for view in state.claims
                if parse_ts(view.claim["occurred_at"]) <= label_end
                and view.outcome != "excluded"
            )
        if not test:
            continue
        output.append(
            RollingPoint(
                origin=origin.date().isoformat(),
                train_segments=len(train),
                test_segments=len(test),
                predicted_rate=predicted,
                actual_rate=(actual_events / (actual_exposure / 365.25)) if actual_exposure > 0 else None,
                predicted_events=(predicted * actual_exposure / 365.25) if predicted is not None else None,
                actual_events=actual_events,
                excluded_devices=len(cohort) - len(test),
            )
        )
    return output


def validation_summary(points: list[RollingPoint]) -> dict:
    if not points:
        return {"points": 0, "note": "没有足够的向后滚动切点"}
    errors = [
        abs(point.predicted_rate - point.actual_rate)
        for point in points
        if point.predicted_rate is not None and point.actual_rate is not None
    ]
    covered = sum(
        1
        for point in points
        if point.predicted_rate is not None
        and point.actual_rate is not None
        and abs(point.predicted_rate - point.actual_rate) <= max(0.5 * point.predicted_rate, 0.5)
    )
    return {
        "points": len(points),
        "mean_abs_rate_error": round(sum(errors) / len(errors), 4) if errors else None,
        "within_tolerance": covered,
        "note": "滚动验证只检查可解释的批次率是否随新材料稳定，不构成可靠性认证",
    }
