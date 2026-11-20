"""分析入口。

命令：
  python -m warranty_reserve materials              资料进入与可见性
  python -m warranty_reserve risk                   批次风险与结局构成
  python -m warranty_reserve reserve                准备金认定
  python -m warranty_reserve scenarios --file x.json  新批次接收情景
  python -m warranty_reserve validate               向后滚动验证
  python -m warranty_reserve month-end --out PATH   月度认定承接
  python -m warranty_reserve all                    全流程摘要
所有命令支持 --json 输出机器可读结果，支持 --cutoff 指定截点。
"""
from __future__ import annotations

import argparse
import json
import pathlib

from .data import load_dataset
from .determination import (
    FundingChange,
    SubsequentChange,
    apply_change,
    freeze_determination,
    load_month,
    reopen_case,
    save_month,
)
from .domain import Book, outcome_table
from .report import money, section, table
from .reserve import Assumptions, estimate_reserve, funding_view, gap_view, simulate
from .risk import batch_view, build_estimates, rolling_validation, validation_summary
from .scenarios import NewBatch, compare_new_batches, compare_portfolio_claims


def _json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _assumptions(args) -> Assumptions:
    return Assumptions(
        common_mode_probability=args.common_mode_probability,
        common_mode_hit_rate=args.common_mode_hit_rate,
        severity_log_sigma=args.severity_sigma,
    )


def _dataset(args):
    root = pathlib.Path(args.data_root) if getattr(args, "data_root", None) else None
    return load_dataset(root=root, cutoff=args.cutoff)


def cmd_materials(args) -> None:
    ds = _dataset(args)
    book = Book(ds)
    payload = {
        "version": ds.version,
        "cutoff": ds.cutoff_text,
        "tables_raw": ds.raw_counts(),
        "tables_visible_at_cutoff": ds.table_counts(),
        "segments_complete_cohort": sum(1 for state in book.segments.values() if state.cohort_complete),
        "segments_incomplete_cohort": sum(1 for state in book.segments.values() if not state.cohort_complete),
        "segments_active": len(book.active_segments()),
        "segments_excluded_decision_conflicts": len(book.conflicts()),
        "contract_check_examples": [
            {"claim": view.claim_id, "decision": view.recorded_decision, "contract": view.contract_derived,
             "note": view.check_note}
            for view in book.conflicts()[:5]
        ],
        "resale_overlaps": len(book.overlaps),
        "prices": {
            component: {
                "source": price.source_id,
                "basis": price.basis,
                "repair_cost_cents": price.repair_cost_cents,
                "recovery_confirmed": price.recovery_confirmed,
            }
            for component, price in sorted(ds.repair_prices().items())
        },
        "funds": {
            "available_cents": ds.available_funds,
            "commitments": ds.commitments,
        },
    }
    if args.json:
        _json(payload)
        return
    print(f"资料版本 {payload['version']}，截点 {payload['cutoff']}")
    rows = []
    for name in ("sales", "claims", "repairs", "contracts", "costs", "funds"):
        rows.append([name, str(payload["tables_raw"].get(name, 0)), str(payload["tables_visible_at_cutoff"].get(name, 0))])
    print(table(["资料表", "原始行数", "截点可见"], rows, ["l", "r", "r"]))
    print(f"\n保障段 {len(book.segments)}：完整名册 {payload['segments_complete_cohort']}，"
          f"不完整 {payload['segments_incomplete_cohort']}，截点仍在保 {payload['segments_active']}")
    print(f"售后认定与合同推导不一致：{payload['segments_excluded_decision_conflicts']} 件"
          "（保留售后认定，列入一致性核对）")
    print(f"同一实物后售段与前段保修期重叠：{payload['resale_overlaps']} 件（风险分别计量）")
    print(section("价格证据（截点已知）"))
    print(table(
        ["零件组", "来源", "依据", "维修价", "追偿确认"],
        [[key, value["source"], value["basis"], money(value["repair_cost_cents"]),
          "是" if value["recovery_confirmed"] else "否"] for key, value in payload["prices"].items()],
    ))
    print(section("资金事实"))
    print(f"可用 {money(ds.available_funds)}；已占用 {money(sum(int(row['cents']) for row in ds.commitments))}；"
          f"共享未占用余额 {money(ds.free_funds())}")


def cmd_risk(args) -> None:
    ds = _dataset(args)
    book = Book(ds)
    rows = batch_view(book)
    validation = validation_summary(rolling_validation(book))
    if args.json:
        _json({"batches": rows, "strata": [item.as_dict() for item in build_estimates(book)], "validation": validation})
        return
    print("批次风险（分母只含完整名册；转入资料单列）")
    rendered = []
    for row in rows:
        rendered.append([
            row["batch"],
            str(row["complete_cohort"]),
            str(row["incomplete_excluded"]),
            f"{row['exposure_years']:.2f}",
            str(row["first_failure"]),
            str(row["repeat_repair"]),
            str(row["exits"]),
            str(row["pending"]),
            "—" if row["rate_per_segment_year"] is None else f"{row['rate_per_segment_year']:.3f}",
            "—" if row["shrunk_rate"] is None else f"{row['shrunk_rate']:.3f}",
            "保留限制" if row["retained_limit"] else "",
        ])
    print(table(
        ["批次", "段", "转来", "暴露年", "首次", "重复", "退出", "未决*", "经验率", "收缩率", "限制"],
        rendered,
        ["l", "r", "r", "r", "r", "r", "r", "r", "r", "r", "l"],
    ))
    print("\n* 未决 = 已申报待诊断或待决维修；首次事件包含这些已发生但未结的故障。")
    print(section("结局构成（按保障段计，不按工单行数）"))
    outcomes = outcome_table(book)
    for row in outcomes:
        counts = row["outcomes"]
        print(f"{row['batch']}: 无申报 {counts['no_claim']}，在保维修 {counts['covered_repair']}，"
              f"重复 {counts['covered_repeat']}，退款退出 {counts['covered_refund']}，"
              f"换机退出 {counts['covered_replacement']}，不在保 {counts['excluded']}，"
              f"待决维修 {counts['pending_repair']}，待诊断 {counts['awaiting_diagnosis']}，"
              f"未结案 {row['open_cases']}；已付 {money(row['realized_paid_cents'])}，"
              f"未付 {money(row['unpaid_cents'])}")
    print(section("向后滚动验证"))
    print(json.dumps(validation, ensure_ascii=False, indent=2))


def cmd_reserve(args) -> None:
    ds = _dataset(args)
    book = Book(ds)
    assumptions = _assumptions(args)
    reserve = estimate_reserve(book, ds, args.scope, assumptions)
    funding = funding_view(ds, args.scope)
    gap = gap_view(reserve, funding)
    simulation = simulate(book, ds, reserve, funding, simulations=args.simulations, assumptions=assumptions)
    if args.json:
        _json({
            "reserve": reserve.to_dict(),
            "funding": funding.to_dict(),
            "gap": gap.to_dict(),
            "simulation": simulation.to_dict(),
        })
        return
    print(f"准备金认定　截点 {reserve.as_of}　范围 {reserve.scope}　模型 {reserve.model_version}")
    print(section("构成"))
    print(table(
        ["项目", "金额", "口径"],
        [
            ["已付维修（已实现，不再计提）", money(reserve.paid_realized_cents), "只展示，未重复计入"],
            ["已报未决（未付维修）", money(reserve.reported_unpaid_cents), "不因结案或未结案消失"],
            ["未报有效保障（IBNR 期望）", money(reserve.ibnr_expected_cents), f"{reserve.ibnr_segments} 段在保"],
            ["重复维修上限备抵", money(reserve.repeat_allowance_cents),
             f"已关联重复 {reserve.repeat_detail['linked_repeat_events']} 件；无关联时按上限"],
            ["毛计要求", money(reserve.gross_required_cents), "未扣追偿"],
            ["确认追偿抵减", money(reserve.confirmed_recovery_cents), ",".join(reserve.confirmed_recovery_ids) or "—"],
            ["未确认追偿（备忘，不抵减）", money(reserve.unconfirmed_recovery_cents), ",".join(reserve.unconfirmed_recovery_ids) or "—"],
            ["净准备金要求", money(reserve.net_requirement_cents), "确认范围内抵减后"],
        ],
        ["l", "r", "l"],
    ))
    print(section("资金与缺口"))
    print(table(
        ["可用总额", "已有占用", "共享未占用", "本范围覆盖", "缺口", "覆盖率"],
        [[money(funding.available_cents), money(funding.committed_cents), money(funding.free_cents),
          money(gap.coverage_cents), money(gap.gap_cents),
          "—" if gap.coverage_ratio is None else f"{gap.coverage_ratio:.2f}"]],
        ["r"] * 6,
    ))
    if gap.stale_warning:
        print(f"注意：{gap.stale_warning}")
    print(section("未报有效保障明细（仅无既往申报的首次故障）"))
    if reserve.ibnr_detail:
        print(table(
            ["保障段", "批次", "剩余月", "首次故障概率", "期望单案", "IBNR"],
            [[row["sale"], row["batch"], f"{row['remaining_months']:.2f}",
              f"{row['claim_probability']:.3%}", money(row["expected_cost_cents"]), money(row["ibnr_cents"])]
             for row in reserve.ibnr_detail],
            ["l", "l", "r", "r", "r", "r"],
        ))
    else:
        print("无：仍在保的保障段都已有有效申报，不按首次故障重复计提。")
    print(section("维修后再出险备抵（证据不足上限）"))
    if reserve.recurrence_detail:
        print(table(
            ["保障段", "批次", "剩余月", "再出险概率", "单案价", "备抵"],
            [[row["sale"], row["batch"], f"{row['remaining_months']:.2f}",
              f"{row['recurrence_probability']:.3%}", money(row["unit_cost_cents"]),
              money(row["allowance_cents"])] for row in reserve.recurrence_detail],
            ["l", "l", "r", "r", "r", "r"],
        ))
    else:
        print("无仍在保的已维修保障段。")
    print(section("支出尾部（含共同冲击）"))
    quantiles = simulation.spend_quantiles_cents
    print(table(
        ["模拟", "均值", "p50", "p90", "p95", "p99", "资金不足概率"],
        [[str(simulation.simulations), money(quantiles["mean"]), money(quantiles["p50"]),
          money(quantiles["p90"]), money(quantiles["p95"]), money(quantiles["p99"]),
          f"{simulation.shortfall_probability:.1%}"]],
        ["r"] * 7,
    ))
    print("独立对照均值 " + money(simulation.independent_expected_cents)
          + "；共同冲击下均值 " + money(simulation.correlated_expected_cents)
          + "。尾部不代表精算置信，只是资金安全观察。")
    print(section("证据限制"))
    for item in reserve.evidence_limits:
        print(f"- {item}")


def _load_scenarios(path: pathlib.Path) -> list[NewBatch]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [NewBatch(**item) for item in payload["batches"]]


def cmd_scenarios(args) -> None:
    ds = _dataset(args)
    book = Book(ds)
    batches = _load_scenarios(pathlib.Path(args.file))
    results = compare_new_batches(book, ds, batches, _assumptions(args), simulations=args.simulations)
    funding = funding_view(ds)
    portfolio = compare_portfolio_claims(results, funding)
    if args.json:
        _json({"scenarios": [item.to_dict() for item in results], "portfolio": portfolio})
        return
    print("新批次接收情景（增量口径，不构成报价建议）")
    print(table(
        ["情景", "台数", "合同", "增量 IBNR", "重复备抵", "确认追偿", "净增量", "尾部 p90", "尾部 p99", "缺口"],
        [[item.name, str(item.new_batch.segments), item.new_batch.contract,
          money(item.incremental_ibnr_cents), money(item.incremental_repeat_allowance_cents),
          money(item.confirmed_recovery_cents), money(item.net_incremental_cents),
          money(item.incremental_tail_p90_cents), money(item.incremental_tail_p99_cents),
          money(item.gap_after_cents)] for item in results],
        ["l", "r", "l", "r", "r", "r", "r", "r", "r", "r"],
    ))
    for item in results:
        if item.assumptions_note:
            print(f"- {item.name}：" + "；".join(item.assumptions_note))
    print(section("组合检查"))
    print(json.dumps(portfolio, ensure_ascii=False, indent=2))


def cmd_validate(args) -> None:
    ds = _dataset(args)
    book = Book(ds)
    points = rolling_validation(book, args.origins if hasattr(args, "origins") else None)
    summary = validation_summary(points)
    if args.json:
        _json({"points": [point.__dict__ for point in points], "summary": summary})
        return
    print("向后滚动验证（按实物身份隔离，测试段不读取未来申报）")
    print(table(
        ["切点", "训练段", "测试段", "隔离剔除", "预测率", "实际率", "预测事件", "实际事件"],
        [[point.origin, str(point.train_segments), str(point.test_segments), str(point.excluded_devices),
          "—" if point.predicted_rate is None else f"{point.predicted_rate:.3f}",
          "—" if point.actual_rate is None else f"{point.actual_rate:.3f}",
          "—" if point.predicted_events is None else f"{point.predicted_events:.1f}",
          str(point.actual_events)] for point in points],
        ["l", "r", "r", "r", "r", "r", "r", "r"],
    ))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_month_end(args) -> None:
    ds = _dataset(args)
    book = Book(ds)
    month = freeze_determination(book, ds, args.scope, args.id, _assumptions(args))
    funding = funding_view(ds, args.scope)
    if args.out:
        save_month(pathlib.Path(args.out), month, funding)
    adequacy = month.adequacy(funding)
    if args.json:
        _json(adequacy)
        return
    print(f"月度认定 {month.determination.determination_id}（冻结）")
    print(f"模型 {month.determination.model_version}；净要求 {money(month.determination.net_requirement_cents)}；"
          f"覆盖 {money(month.determination.coverage_cents)}；缺口 {money(month.determination.gap_cents)}")
    print(section("承接核对"))
    print(json.dumps(adequacy, ensure_ascii=False, indent=2))


def cmd_change(args) -> None:
    """在已保存认定上登记有来源的释放/补提，并重新核对充足性。"""
    ds = _dataset(args)
    month = load_month(pathlib.Path(args.month))
    if args.kind == "release":
        change = SubsequentChange(
            kind="release",
            amount_cents=args.amount_cents,
            reason=args.reason,
            source_ids=args.source_ids,
            related_determination=month.determination.determination_id,
            relates_to_segment=args.segment,
        )
        try:
            apply_change(month, change)
        except ValueError as error:
            raise SystemExit(str(error)) from None
    elif args.kind == "top_up":
        change = SubsequentChange(
            kind="top_up",
            amount_cents=args.amount_cents,
            reason=args.reason,
            source_ids=args.source_ids,
            related_determination=month.determination.determination_id,
            relates_to_segment=args.segment,
        )
        try:
            apply_change(month, change)
        except ValueError as error:
            raise SystemExit(str(error)) from None
    elif args.kind in ("new_commitment", "release_commitment", "new_available"):
        if not args.scope:
            raise SystemExit("资金变化需要 --scope")
        month.funding_changes.append(
            FundingChange(
                kind=args.kind,
                amount_cents=args.amount_cents,
                scope=args.scope,
                reason=args.reason,
                source_ids=args.source_ids,
                detected_at=args.detected_at or ds.cutoff_text,
            )
        )
    else:
        raise SystemExit("未知变化类型")
    funding = funding_view(ds, month.determination.scope)
    changed = funding.available_cents - funding.committed_cents
    snapshot = month.determination.funding_snapshot or {}
    froze = int(snapshot.get("available_cents", 0)) - int(snapshot.get("committed_cents", 0))
    if changed != froze and not month.funding_changes:
        raise SystemExit(
            "资金事实已变化但未登记资金变化；请先以 --kind new_commitment / release_commitment / new_available 登记来源"
        )
    if args.out:
        save_month(pathlib.Path(args.out), month, funding)
    adequacy = month.adequacy(funding)
    if args.json:
        _json(adequacy)
        return
    print(section("承接核对（含新变化）"))
    print(json.dumps(adequacy, ensure_ascii=False, indent=2))


def cmd_reopen(args) -> None:
    """售后重开旧案：新记录保留与原认定的关系，金额差异走补提。"""
    ds = _dataset(args)
    month = load_month(pathlib.Path(args.month))
    original = next(
        (view.claim for view in Book(ds).claims.values() if view.claim_id == args.original),
        None,
    )
    if original is None:
        raise SystemExit("原案不在当前截点可见资料中")
    reopened = {
        "id": args.new_id,
        "sale": original["sale"],
        "decision": args.decision,
        "amount_cents": args.amount_cents,
        "reported_at": args.detected_at or ds.cutoff_text,
    }
    try:
        case = reopen_case(
            month,
            {**original, "amount_cents": args.original_amount_cents},
            reopened,
            detected_at=args.detected_at or ds.cutoff_text,
            reason=args.reason,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    funding = funding_view(ds, month.determination.scope)
    if args.out:
        save_month(pathlib.Path(args.out), month, funding)
    payload = {"reopened": case.to_dict(), "adequacy": month.adequacy(funding)}
    if args.json:
        _json(payload)
        return
    print(section("重开案件"))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _demo_scenarios(book, ds, assumptions, simulations):
    """全流程摘要中的示例情景：只用于展示接口，不是具体报价建议。"""
    batches = [
        NewBatch("示例-20台C1", 20, "aircon", 4, "C1", "2026-10-01", component="PART1"),
        NewBatch("示例-10台C2", 10, "washer", 7, "C2", "2026-10-01", component="PART3"),
    ]
    return compare_new_batches(book, ds, batches, assumptions, simulations=simulations)


def cmd_all(args) -> None:
    ds = _dataset(args)
    book = Book(ds)
    assumptions = _assumptions(args)
    reserve = estimate_reserve(book, ds, "all", assumptions)
    funding = funding_view(ds)
    simulation = simulate(book, ds, reserve, funding, simulations=args.simulations, assumptions=assumptions)
    validation = rolling_validation(book)
    scenarios = _demo_scenarios(book, ds, assumptions, min(args.simulations, 1500))
    payload = {
        "materials": ds.table_counts(),
        "active_segments": len(book.active_segments()),
        "outcomes": outcome_table(book),
        "risk": batch_view(book),
        "reserve": reserve.to_dict(),
        "funding": funding.to_dict(),
        "gap": gap_view(reserve, funding).to_dict(),
        "simulation": simulation.to_dict(),
        "validation": {
            "summary": validation_summary(validation),
            "points": [point.__dict__ for point in validation],
        },
        "demo_scenarios": [item.to_dict() for item in scenarios],
    }
    if args.json:
        _json(payload)
        return
    cmd_materials(args)
    print("\n" + "=" * 72)
    cmd_risk(args)
    print("\n" + "=" * 72)
    cmd_reserve(args)
    print("\n" + "=" * 72)
    print(section("示例新批次情景（接口演示，不是具体报价建议）"))
    demo = _demo_scenarios(book, ds, assumptions, min(args.simulations, 1500))
    print(table(
        ["情景", "台数", "净增量", "尾部 p99", "缺口"],
        [[item.name, str(item.new_batch.segments), money(item.net_incremental_cents),
          money(item.incremental_tail_p99_cents), money(item.gap_after_cents)] for item in demo],
        ["l", "r", "r", "r", "r"],
    ))
    print(validation_summary(validation))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warranty_reserve", description="翻新家电保修暴露与准备金充足性分析（合成资料）")
    parser.add_argument("--cutoff", default=None, help="分析截点（ISO 时间，含 +08:00）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cutoff", default=None)
    common.add_argument("--data-root", default=None, help="包含 data/ 的资料根目录，默认使用项目 data/")
    common.add_argument("--json", action="store_true")
    common.add_argument("--scope", default="all")
    common.add_argument("--simulations", type=int, default=4000)
    common.add_argument("--common-mode-probability", type=float, default=0.05)
    common.add_argument("--common-mode-hit-rate", type=float, default=0.35)
    common.add_argument("--severity-sigma", type=float, default=0.60)

    sub.add_parser("materials", parents=[common])
    sub.add_parser("risk", parents=[common])
    sub.add_parser("reserve", parents=[common])
    sub.add_parser("validate", parents=[common])
    month = sub.add_parser("month-end", parents=[common])
    month.add_argument("--id", default=None)
    month.add_argument("--out", default=None)
    change = sub.add_parser("change", parents=[common])
    change.add_argument("--month", required=True, help="已保存的月度认定 JSON")
    change.add_argument("--kind", required=True)
    change.add_argument("--amount-cents", type=int, required=True)
    change.add_argument("--reason", required=True)
    change.add_argument("--source-ids", nargs="+", required=True)
    change.add_argument("--segment", default=None)
    change.add_argument("--detected-at", default=None)
    change.add_argument("--out", default=None)
    reopen = sub.add_parser("reopen", parents=[common])
    reopen.add_argument("--month", required=True)
    reopen.add_argument("--original", required=True)
    reopen.add_argument("--new-id", required=True)
    reopen.add_argument("--decision", default="covered")
    reopen.add_argument("--original-amount-cents", type=int, default=0)
    reopen.add_argument("--amount-cents", type=int, required=True)
    reopen.add_argument("--reason", required=True)
    reopen.add_argument("--detected-at", default=None)
    reopen.add_argument("--out", default=None)
    scenario = sub.add_parser("scenarios", parents=[common])
    scenario.add_argument("--file", required=True)
    sub.add_parser("all", parents=[common])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "materials": cmd_materials,
        "risk": cmd_risk,
        "reserve": cmd_reserve,
        "scenarios": cmd_scenarios,
        "validate": cmd_validate,
        "month-end": cmd_month_end,
        "change": cmd_change,
        "reopen": cmd_reopen,
        "all": cmd_all,
    }
    handlers[args.command](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
