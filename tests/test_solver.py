"""求解器测试：冲突组件枚举、最优字典序、截止工单松弛、题面两个例子。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from archivecut.engine import simulate  # noqa: E402
from archivecut.model import Operation, parse_case  # noqa: E402
from archivecut.solver import build_components, enumerate_component, solve  # noqa: E402


def op_dict(op):
    """Operation dataclass -> parse_case 期望的原始 dict。"""
    d = {"id": op.op_id, "kind": op.kind,
         "batch_from": op.batch_from, "batch_to": op.batch_to,
         "cost": op.cost}
    for key in ("manifest_id", "old_digest", "new_digest", "cabinet_id",
                "new_domain", "new_edges", "copy_id", "digest"):
        val = getattr(op, key)
        if val is not None:
            d[key] = val
    return d

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures")


def load_fixture(name):
    import json
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as fh:
        return parse_case(json.load(fh))


class ComponentEnumerationTest(unittest.TestCase):
    def test_independent_operations_split_into_components(self):
        case = parse_case({
            "case_id": "c", "required_copies": 1, "batch_count": 1,
            "domains": ["D1", "D2", "D3", "D4"],
            "root_cabinets": ["R1", "R2", "R3", "R4"],
            "cabinets": [
                {"id": "R1", "domain": "D1", "edges": []},
                {"id": "R2", "domain": "D2", "edges": []},
                {"id": "R3", "domain": "D3", "edges": []},
                {"id": "R4", "domain": "D4", "edges": []},
            ],
            "copies": [
                {"id": "a1", "digest": "A", "holder": "R1"},
                {"id": "a2", "digest": "A", "holder": "R3"},
                {"id": "b1", "digest": "B", "holder": "R2"},
                {"id": "b2", "digest": "B", "holder": "R4"},
            ],
            "envelopes": [],
            "manifests": [
                {"id": "M1", "content_digests": ["A"],
                 "envelope_digests": [], "copies_required": 1},
                {"id": "M2", "content_digests": ["B"],
                 "envelope_digests": [], "copies_required": 1},
            ],
            "work_orders": [], "decommissions": [],
            "operations": [
                op_dict(Operation(op_id="k1", kind="invalidate", batch_from=1,
                          batch_to=1, cost=1, manifest_id="M1")),
                op_dict(Operation(op_id="k2", kind="invalidate", batch_from=1,
                          batch_to=1, cost=1, manifest_id="M2")),
            ],
        })
        comps = build_components(case)
        self.assertEqual(len(comps), 2)
        # 每个组件只含一个操作，枚举空间是两个 2 选项之和，而非 2x2 整体展开。
        for comp in comps:
            opts, _ = enumerate_component(case, comp)
            self.assertEqual(len(opts), 2)

    def test_shared_manifest_merges_components(self):
        case = parse_case({
            "case_id": "c", "required_copies": 1, "batch_count": 1,
            "domains": ["D1", "D2", "D3"],
            "root_cabinets": ["R1", "R2", "R3"],
            "cabinets": [
                {"id": "R1", "domain": "D1", "edges": []},
                {"id": "R2", "domain": "D2", "edges": []},
                {"id": "R3", "domain": "D3", "edges": []},
            ],
            "copies": [
                {"id": "a1", "digest": "A", "holder": "R1"},
                {"id": "a2", "digest": "A", "holder": "R2"},
                {"id": "b1", "digest": "B", "holder": "R3"},
                {"id": "b2", "digest": "B", "holder": "R1"},
            ],
            "envelopes": [],
            "manifests": [
                {"id": "M1", "content_digests": ["A", "B"],
                 "envelope_digests": [], "copies_required": 1},
            ],
            "work_orders": [], "decommissions": [],
            "operations": [
                op_dict(Operation(op_id="ca", kind="cleanup", batch_from=1,
                          batch_to=1, cost=1, copy_id="a1")),
                op_dict(Operation(op_id="cb", kind="cleanup", batch_from=1,
                          batch_to=1, cost=1, copy_id="b1")),
            ],
        })
        comps = build_components(case)
        self.assertEqual(len(comps), 1)  # 经 M1 的不可拆证明连通

    def test_enumeration_respects_batch_windows(self):
        case = load_fixture("case_demo_transient.json")
        comps = build_components(case)
        for comp in comps:
            opts, _ = enumerate_component(case, comp)
            for opt in opts:
                for oid, b in opt.assignment.items():
                    op = case.op_index()[oid]
                    self.assertTrue(op.batch_from <= b <= op.batch_to)


class OptimalityTest(unittest.TestCase):
    def test_cost_is_primary_objective(self):
        case = parse_case({
            "case_id": "o", "required_copies": 1, "batch_count": 1,
            "domains": ["D1", "D2"],
            "root_cabinets": ["R1", "R2"],
            "cabinets": [
                {"id": "R1", "domain": "D1", "edges": []},
                {"id": "R2", "domain": "D2", "edges": []},
            ],
            "copies": [
                {"id": "a1", "digest": "A", "holder": "R1"},
                {"id": "a2", "digest": "A", "holder": "R2"},
            ],
            "envelopes": [],
            "manifests": [
                {"id": "M1", "content_digests": ["A"],
                 "envelope_digests": [], "copies_required": 1},
            ],
            "work_orders": [
                {"id": "W1", "kind": "invalidate",
                 "manifest_id": "M1", "deadline_batch": 1},
            ],
            "decommissions": [],
            "operations": [
                op_dict(Operation(op_id="cheap", kind="invalidate", batch_from=1,
                          batch_to=1, cost=1, manifest_id="M1")),
                op_dict(Operation(op_id="pricey", kind="invalidate", batch_from=1,
                          batch_to=1, cost=9, manifest_id="M1")),
            ],
        })
        outcome = solve(case)
        self.assertTrue(outcome.feasible)
        self.assertEqual(outcome.best_key[0], 1)
        self.assertEqual(outcome.best_assignment, {"cheap": 1})

    def test_migration_proof_batches_break_cost_ties(self):
        case = load_fixture("case_migration_proof.json")
        outcome = solve(case)
        self.assertTrue(outcome.feasible)
        # 早迁移（批1）与晚迁移（批2）同价；序列按 (批次, 操作ID) 取更早者。
        self.assertEqual(outcome.best_key[0], 5)
        self.assertEqual(-outcome.best_key[1], 1)
        self.assertEqual(outcome.best_assignment["op-mig-early"], 1)

    def test_sequence_lexicographic_tiebreak(self):
        case = load_fixture("case_demo_transient.json")
        outcome = solve(case)
        self.assertTrue(outcome.feasible)
        # 同价清理动作都被跳过（费用最优），序列只含必要的失效与清理。
        seq = list(outcome.best_key[2])
        self.assertTrue(all(isinstance(x, tuple) for x in seq))
        self.assertEqual(seq, sorted(seq))


class TransientMarginExampleTest(unittest.TestCase):
    """题面例 1：只核对最终状态时两项同价动作都行，

    但其中一个在前一批会只剩一份独立副本（这里由仿真与求解共同裁决）。
    """

    def setUp(self):
        self.case = load_fixture("case_demo_transient.json")

    def test_two_same_cost_paths_have_different_interim_margin(self):
        base = {"op-kill-m1a": 1, "op-purge-a": 2}
        early = dict(base, **{"op-consolidate-early": 1})
        late = dict(base, **{"op-consolidate-late": 2})
        r_early = simulate(self.case, early, detail=True)
        r_late = simulate(self.case, late, detail=True)
        self.assertTrue(r_early.feasible and r_late.feasible)
        self.assertEqual(r_early.total_cost, r_late.total_cost)

        def b_domains(result, batch_no):
            for rep in result.batch_reports:
                if rep["batch"] != batch_no:
                    continue
                for w in rep["witnesses"]:
                    for proof in w["proofs"]:
                        if proof["digest"] == "B":
                            return set(proof["independent_domains"])
            return set()

        # 批 1 末：早迁移已把 C3 并入 D2，B 只剩 3 个独立域；晚迁移尚未发生，
        # B 仍占 4 个独立域。两条路径在批 2 收敛到相同的最终状态。
        self.assertEqual(b_domains(r_early, 1), {"D1", "D2", "D4"})
        self.assertEqual(b_domains(r_late, 1), {"D1", "D2", "D3", "D4"})
        self.assertEqual(b_domains(r_early, 2), b_domains(r_late, 2))
        # 两条路径的每批最不利故障域报告都存在且裕度非负（均合法）。
        for result in (r_early, r_late):
            for rep in result.batch_reports:
                self.assertGreaterEqual(
                    rep["worst_failure_domain"]["min_margin"], 0)


class SharedEnvelopeExampleTest(unittest.TestCase):
    """题面例 2：两张工单同时删除释放空封套，但被第三张保全单引用的内容不能删。"""

    def setUp(self):
        self.case = load_fixture("case_shared_envelope.json")

    def test_empty_envelope_releasable_after_both_invalidations(self):
        r = simulate(self.case, {
            "op-kill-m1": 1, "op-kill-m2": 1, "op-clean-env": 2})
        self.assertTrue(r.feasible, r.reason)

    def test_protected_content_not_deletable_without_rebind(self):
        r = simulate(self.case, {
            "op-kill-m1": 1, "op-kill-m2": 1, "op-clean-z": 2})
        self.assertFalse(r.feasible)
        self.assertIn("still_referenced", r.reason)

    def test_rebind_then_cleanup_is_legal(self):
        r = simulate(self.case, {
            "op-kill-m1": 1, "op-kill-m2": 1,
            "op-rebind-m3": 1, "op-clean-z": 2})
        self.assertTrue(r.feasible, r.reason)


class RelaxationTest(unittest.TestCase):
    def test_only_effective_work_orders_listed(self):
        case = load_fixture("case_infeasible_relax.json")
        outcome = solve(case)
        self.assertFalse(outcome.feasible)
        effective = {r["work_order"] for r in outcome.relaxation}
        # 撤掉 retire 工单可解；撤掉 invalidate 工单仍被 retire 卡死。
        self.assertEqual(effective, {"WO-RETIRE-Q"})

    def test_relaxed_solution_is_itself_feasible(self):
        case = load_fixture("case_infeasible_relax.json")
        outcome = solve(case)
        relax = outcome.relaxation[0]
        enforced = frozenset(wo.wo_id for wo in case.work_orders
                             if wo.wo_id != relax["work_order"])
        assignment = {x["op_id"]: x["batch"]
                      for x in relax["best_sequence"]}
        r = simulate(case, assignment, enforced_wos=enforced)
        self.assertTrue(r.feasible, r.reason)


class WitnessTest(unittest.TestCase):
    def test_batch_reports_carry_witnesses_and_worst_domain(self):
        case = load_fixture("case_migration_proof.json")
        outcome = solve(case)
        r = simulate(case, outcome.best_assignment, detail=True)
        self.assertTrue(r.feasible)
        self.assertEqual(len(r.batch_reports), case.batch_count + 1)
        for rep in r.batch_reports:
            wfd = rep["worst_failure_domain"]
            self.assertIn("domain", wfd)
            self.assertIn("min_margin", wfd)
            self.assertIn("tight_entries", wfd)
            for w in rep["witnesses"]:
                for proof in w["proofs"]:
                    self.assertGreaterEqual(
                        len(proof["independent_domains"]), proof["required"])


if __name__ == "__main__":
    unittest.main()
