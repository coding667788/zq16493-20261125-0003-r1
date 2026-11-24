"""规模测试：5 万清单下的精确求解正确性与分组审计效率。

夹具在测试时生成到 .runtime-tmp（已被 .gitignore 忽略），不把大文件入库。
不设固定时间阈值，只断言结果正确、枚举只沿冲突组件展开。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from archivecut.model import parse_case  # noqa: E402
from archivecut.solver import build_components, solve  # noqa: E402
from tools.make_large_fixture import build as build_large  # noqa: E402

MANIFEST_COUNT = 50000


class LargeScaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = build_large(MANIFEST_COUNT)
        cls.case = parse_case(cls.payload)

    def test_manifest_scale_meets_floor(self):
        # 5 万背景清单 + 16 份可失效目标清单。
        self.assertGreaterEqual(len(self.case.manifests), 50000)
        self.assertEqual(len(self.case.manifests),
                         MANIFEST_COUNT + 16)
        self.assertLessEqual(len(self.case.operations), 17)

    def test_solver_exact_and_components_bounded(self):
        outcome = solve(self.case)
        self.assertTrue(outcome.feasible)
        assignment = outcome.best_assignment
        # 16 张截止工单都必须在批次 2 前完成；同价失效操作按字典序取最早批 1。
        for i in range(16):
            self.assertIn(assignment[f"op-kill-{i:02d}"], (1, 2))
        # 清理无工单强制，费用最优跳过它；它只在 MT00 失效后才合法（另测）。
        self.assertNotIn("op-purge-env0", assignment)
        # 16 个互不共享实体的失效操作拆成独立组件，绝不合并成 2^16 大组件。
        self.assertEqual(len(outcome.components), 16)
        product = 1
        for opts in outcome.component_options:
            product *= len(opts)
        self.assertEqual(product, outcome.total_combos)
        # 纯失效组件只有“批1/批2”两个可行赋值（截止工单禁止跳过）；
        # 含清理动作的组件额外叠加 purge 的选/跳，选项数为 4。
        for comp, opts in zip(outcome.components, outcome.component_options):
            if "op-purge-env0" in comp["operations"]:
                self.assertEqual(len(opts), 4)
            elif comp["operations"][0].startswith("op-kill"):
                self.assertEqual(len(opts), 2)

    def test_cleanup_only_legal_after_referencing_manifest_invalidated(self):
        from archivecut.engine import simulate
        # MT00 未失效时清理 ENVRETIRE 非法；先失效再清理才合法。
        illegal = simulate(self.case, {"op-purge-env0": 2,
                                       **{f"op-kill-{i:02d}": 1
                                          for i in range(16)}})
        # 上面所有失效都执行后，ENVRETIRE 已无引用，清理应当合法。
        self.assertTrue(illegal.feasible, illegal.reason)
        only_purge = simulate(self.case, {"op-purge-env0": 2})
        self.assertFalse(only_purge.feasible)
        self.assertIn("still_referenced", only_purge.reason)

    def test_grouped_audit_covers_all_manifests(self):
        from archivecut.engine import Snapshot
        snap = Snapshot.initial(self.case)
        groups = snap._groups()
        covered = sum(count for _key, (_rep, count) in groups.items())
        self.assertEqual(covered, MANIFEST_COUNT + 16)
        self.assertEqual(snap.audit(None), [])
        for dom in self.case.domains:
            self.assertEqual(snap.audit(dom), [])

    def test_worst_failure_domain_reports_tight_entry(self):
        from archivecut.engine import simulate
        outcome = solve(self.case)
        r = simulate(self.case, outcome.best_assignment, detail=True,
                     witness_limit=5)
        self.assertTrue(r.feasible)
        wfd = r.batch_reports[0]["worst_failure_domain"]
        self.assertIn(wfd["domain"], self.case.domains)
        self.assertGreaterEqual(len(wfd["tight_entries"]), 1)
        # 见证受 witness_limit 限制，避免 5 万清单的输出膨胀。
        self.assertLessEqual(len(r.batch_reports[0]["witnesses"]), 5)

    def test_payload_serializes_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "large.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(self.payload, fh, ensure_ascii=False)
            with open(p, "rb") as fh:
                json.loads(fh.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
