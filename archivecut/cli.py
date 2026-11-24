"""UTF-8 JSON 命令行入口：plan CASE 与 verify CASE RESULT。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .engine import simulate
from .model import InputError, load_case
from .solver import build_components, solve


def _emit(obj: Any, out_path: str | None = None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.buffer.write(text.encode("utf-8") + b"\n")


def _component_summary(case, components, all_options) -> list[dict]:
    out = []
    for comp, opts in zip(components, all_options):
        out.append({
            "operations": comp["operations"],
            "work_orders": comp["work_orders"],
            "feasible_assignments": len(opts),
        })
    return out


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        case = load_case(args.case)
        outcome = solve(case)
    except InputError as exc:
        _emit({"status": "input_error", "error": str(exc)})
        return 2

    base = {
        "case_id": case.case_id,
        "batch_count": case.batch_count,
        "required_copies": case.required_copies,
        "enumeration": {
            "method": "conflict_connected_components",
            "components": _component_summary(
                case, outcome.components, outcome.component_options),
            "component_count": len(outcome.components),
            "product_of_component_feasible_assignments": outcome.total_combos,
            "audit_evaluations": outcome.eval_count,
        },
    }

    if not outcome.feasible:
        base.update({
            "status": "infeasible",
            "relaxation_remove_one_work_order": {
                "checked": [wo.wo_id for wo in case.work_orders],
                # 仅列实际把结果从无解变为可行的工单；空列表表示撤任何一个都不够。
                "effective": outcome.relaxation,
            },
        })
        _emit(base, args.output)
        return 1

    assignment = outcome.best_assignment
    detail = simulate(case, assignment, detail=True,
                      witness_limit=args.witness_limit)
    assert detail.feasible and detail.final_snapshot is not None
    base.update({
        "status": "feasible",
        "objectives": {
            "total_cost": detail.total_cost,
            "migration_proof_batches": detail.migration_proof_batches,
            "sequence": [{"batch": b, "op_id": oid}
                         for b, oid in detail.sequence],
        },
        "operation_decisions": [
            {"op_id": op.op_id, "kind": op.kind, "cost": op.cost,
             "window": [op.batch_from, op.batch_to],
             "decision": "skip" if assignment.get(op.op_id, 0) == 0
                         else "execute",
             "batch": assignment.get(op.op_id, 0)}
            for op in case.operations
        ],
        "batch_reports": detail.batch_reports,
        "final_state": {
            "active_manifests": sorted(detail.final_snapshot.active),
            "live_copies": sorted(detail.final_snapshot.live),
            "retired_cabinets": sorted(detail.final_snapshot.retired),
        },
        "work_orders": [
            {"wo_id": wo.wo_id, "kind": wo.kind,
             "deadline_batch": wo.deadline_batch, "fulfilled": True}
            for wo in case.work_orders
        ],
    })
    _emit(base, args.output)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        case = load_case(args.case)
        with open(args.result, "r", encoding="utf-8") as fh:
            result = json.load(fh)
    except (InputError, json.JSONDecodeError, OSError) as exc:
        _emit({"status": "input_error", "error": str(exc)})
        return 2

    report: dict[str, Any] = {"case_id": case.case_id}
    if result.get("status") == "infeasible":
        outcome = solve(case)
        report["claimed_status"] = "infeasible"
        report["recomputed_status"] = (
            "infeasible" if not outcome.feasible else "feasible")
        claimed = {r["work_order"] for r in
                   result.get("relaxation_remove_one_work_order", {})
                   .get("effective", [])}
        recomputed = {r["work_order"] for r in outcome.relaxation}
        report["relaxation_match"] = claimed == recomputed
        report["effective_work_orders"] = sorted(recomputed)
        report["verified"] = (
            report["recomputed_status"] == "infeasible"
            and report["relaxation_match"])
        _emit(report, args.output)
        return 0 if report["verified"] else 1

    decisions = result.get("operation_decisions", [])
    assignment: dict[str, int] = {}
    for d in decisions:
        if d.get("decision") == "execute":
            assignment[d["op_id"]] = int(d["batch"])
    sim = simulate(case, assignment, detail=True,
                   witness_limit=args.witness_limit)
    report["claimed_status"] = result.get("status")
    report["recomputed_feasible"] = sim.feasible
    report["failure_reason"] = sim.reason
    if sim.feasible:
        claimed_obj = result.get("objectives", {})
        recomputed_key = (sim.total_cost, -sim.migration_proof_batches,
                          tuple(sim.sequence))
        report["recomputed_objectives"] = {
            "total_cost": sim.total_cost,
            "migration_proof_batches": sim.migration_proof_batches,
            "sequence": [{"batch": b, "op_id": oid}
                         for b, oid in sim.sequence],
        }
        report["objective_match"] = (
            claimed_obj.get("total_cost") == sim.total_cost
            and claimed_obj.get("migration_proof_batches")
            == sim.migration_proof_batches
            and [(x["batch"], x["op_id"]) for x in
                 claimed_obj.get("sequence", [])] == list(sim.sequence)
        )
        # 最优性复核：重新求解，比较完整字典序目标键。
        outcome = solve(case)
        if outcome.feasible:
            report["optimal_key"] = list(outcome.best_key[0:1]) + [
                -outcome.best_key[1],
                [{"batch": b, "op_id": oid} for b, oid in
                 outcome.best_key[2]]]
            report["is_optimal"] = outcome.best_key == recomputed_key
        else:
            report["is_optimal"] = False
        report["all_work_orders_fulfilled"] = True
        report["verified"] = (
            report["objective_match"] and report["is_optimal"])
    else:
        report["is_optimal"] = False
        report["verified"] = False
    _emit(report, args.output)
    return 0 if report["verified"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archivecut",
        description="数字保全删除计划精确求解器（plan / verify）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="求解 CASE，输出最优删除计划")
    p_plan.add_argument("case", help="输入 CASE 的 UTF-8 JSON 路径")
    p_plan.add_argument("-o", "--output", help="结果输出路径（缺省 stdout）")
    p_plan.add_argument("--witness-limit", type=int, default=None,
                        help="每批最多输出多少份清单的见证明细")
    p_plan.set_defaults(func=cmd_plan)

    p_ver = sub.add_parser("verify", help="复算 RESULT 的成功路径与最优性")
    p_ver.add_argument("case", help="输入 CASE 的 UTF-8 JSON 路径")
    p_ver.add_argument("result", help="plan 产出的 RESULT JSON 路径")
    p_ver.add_argument("-o", "--output", help="复核报告输出路径（缺省 stdout）")
    p_ver.add_argument("--witness-limit", type=int, default=None)
    p_ver.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
