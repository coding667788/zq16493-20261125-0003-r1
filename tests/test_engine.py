"""引擎语义测试：继承环、故障域、单域离线、清理合法性、重绑、迁移。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from archivecut.engine import Snapshot, _apply_op, simulate  # noqa: E402
from archivecut.model import parse_case  # noqa: E402


def base_case():
    return parse_case({
        "case_id": "t", "required_copies": 2, "batch_count": 2,
        "domains": ["D1", "D2", "D3", "D4"],
        "root_cabinets": ["R1", "R2", "R3", "R4"],
        "cabinets": [
            # R1 -> CX -> R1 成环，验证继承图允许环且可达计算不卡死。
            {"id": "R1", "domain": "D1", "edges": ["CX"]},
            {"id": "R2", "domain": "D2", "edges": []},
            {"id": "R3", "domain": "D3", "edges": []},
            {"id": "R4", "domain": "D4", "edges": []},
            {"id": "CX", "domain": "D3", "edges": ["R1"]},
        ],
        "copies": [
            {"id": "c1", "digest": "A", "holder": "R1"},
            {"id": "c2", "digest": "A", "holder": "R2"},
            {"id": "c3", "digest": "A", "holder": "R3"},
            {"id": "c4", "digest": "B", "holder": "R1"},
            {"id": "c5", "digest": "B", "holder": "R2"},
            {"id": "c6", "digest": "B", "holder": "R4"},
        ],
        "envelopes": [],
        "manifests": [
            {"id": "M1", "content_digests": ["A", "B"],
             "envelope_digests": []},
        ],
        "work_orders": [],
        "decommissions": [],
        "operations": [],
    })


class EngineSemanticsTest(unittest.TestCase):
    def test_inheritance_graph_allows_cycle(self):
        case = base_case()  # R1 -> CX -> R1 成环
        snap = Snapshot.initial(case)
        reach = snap.reachable(None)
        self.assertEqual(reach, {"R1", "R2", "R3", "R4", "CX"})

    def test_reachability_respects_offline_domain(self):
        case = base_case()
        snap = Snapshot.initial(case)
        reach = snap.reachable("D1")
        self.assertNotIn("R1", reach)
        self.assertNotIn("CX", reach)  # CX 的唯一入口经过 D1 的 R1
        self.assertIn("R2", reach)

    def test_duplicate_digest_does_not_share_failure_domain(self):
        case = base_case()
        snap = Snapshot.initial(case)
        # 三个 A 副本分布在三个不同域；重复摘要仍按域计独立副本。
        self.assertEqual(snap.independent_domains("A"),
                         {"D1", "D2", "D3"})
        self.assertEqual(snap.independent_domains("A", "D1"),
                         {"D2", "D3"})

    def test_single_domain_offline_audit(self):
        case = base_case()
        snap = Snapshot.initial(case)
        self.assertEqual(snap.audit(None), [])
        for dom in case.domains:
            self.assertEqual(snap.audit(dom), [], dom)

    def test_pinned_domain_overrides_holder_domain(self):
        case = parse_case({
            "case_id": "p", "required_copies": 1, "batch_count": 1,
            "domains": ["D1", "D2"],
            "root_cabinets": ["R1", "R2"],
            "cabinets": [
                {"id": "R1", "domain": "D1", "edges": []},
                {"id": "R2", "domain": "D2", "edges": []},
            ],
            "copies": [
                {"id": "x1", "digest": "X", "holder": "R1"},
                {"id": "x2", "digest": "X", "holder": "R2",
                 "pinned_domain": "D1"},
            ],
            "envelopes": [],
            "manifests": [{"id": "M", "content_digests": ["X"],
                           "envelope_digests": [], "copies_required": 1}],
            "work_orders": [], "decommissions": [], "operations": [],
        })
        snap = Snapshot.initial(case)
        # x2 物理在 D2 柜但固定域 D1：离线 D2 时它仍算 D1 的副本。
        self.assertEqual(snap.effective_domain("x2"), "D1")
        self.assertEqual(snap.independent_domains("X", "D2"), {"D1"})

    def test_cleanup_blocked_while_digest_referenced(self):
        case = base_case()
        case.operations = []
        res = simulate(case, {})  # 基线可行
        self.assertTrue(res.feasible)
        # 直接构造一个清理被引用摘要的操作并仿真。
        case2 = base_case()
        case2.operations = [
            _op("clean", "cleanup", 1, 1, digest="A"),
        ]
        r = simulate(case2, {"clean": 1})
        self.assertFalse(r.feasible)
        self.assertIn("still_referenced", r.reason)

    def test_invalidation_only_removes_own_references(self):
        case = parse_case({
            "case_id": "m", "required_copies": 1, "batch_count": 1,
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
                {"id": "M2", "content_digests": ["A"],
                 "envelope_digests": [], "copies_required": 1},
            ],
            "work_orders": [], "decommissions": [],
            "operations": [
                _op_dict(_op("kill", "invalidate", 1, 1, manifest_id="M1")),
                _op_dict(_op("purge", "cleanup", 1, 1, digest="A")),
            ],
        })
        # M1 失效但 M2 仍引用 A：清理仍非法。
        r = simulate(case, {"kill": 1, "purge": 1})
        self.assertFalse(r.feasible)
        self.assertIn("still_referenced", r.reason)

    def test_rebind_requires_preregistered_digest(self):
        case = parse_case({
            "case_id": "r", "required_copies": 1, "batch_count": 1,
            "domains": ["D1", "D2"],
            "root_cabinets": ["R1", "R2"],
            "cabinets": [
                {"id": "R1", "domain": "D1", "edges": []},
                {"id": "R2", "domain": "D2", "edges": []},
            ],
            "copies": [
                {"id": "a1", "digest": "A", "holder": "R1"},
                {"id": "a2", "digest": "A", "holder": "R2"},
                {"id": "n1", "digest": "N", "holder": "R1"},
                {"id": "n2", "digest": "N", "holder": "R2"},
            ],
            "envelopes": [],
            "manifests": [{"id": "M", "content_digests": ["A"],
                           "envelope_digests": [], "copies_required": 1}],
            "work_orders": [], "decommissions": [],
            "operations": [
                _op_dict(_op("rb", "rebind", 1, 1, manifest_id="M",
                    old_digest="A", new_digest="N")),
            ],
        })
        r = simulate(case, {"rb": 1})
        self.assertTrue(r.feasible, r.reason)
        self.assertEqual(r.final_snapshot.mdigests["M"], ["N"])

    def test_migration_changes_domain_and_edges(self):
        case = base_case()
        case.operations = [
            _op("mg", "migrate", 1, 1, cabinet_id="CX",
                new_domain="D1", new_edges=[]),
        ]
        r = simulate(case, {"mg": 1})
        self.assertTrue(r.feasible, r.reason)
        snap = r.final_snapshot
        self.assertEqual(snap.cab_domain["CX"], "D1")
        self.assertEqual(snap.cab_edges["CX"], ())


def _op(oid, kind, bf, bt, **kw):
    from archivecut.model import Operation
    return Operation(op_id=oid, kind=kind, batch_from=bf, batch_to=bt,
                     cost=1, **kw)


def _op_dict(op):
    """把 Operation dataclass 转回 parse_case 期望的原始 dict 形态。"""
    d = {"id": op.op_id, "kind": op.kind,
         "batch_from": op.batch_from, "batch_to": op.batch_to,
         "cost": op.cost}
    for src, key in (
        ("manifest_id", "manifest_id"), ("old_digest", "old_digest"),
        ("new_digest", "new_digest"), ("cabinet_id", "cabinet_id"),
        ("new_domain", "new_domain"), ("new_edges", "new_edges"),
        ("copy_id", "copy_id"), ("digest", "digest"),
    ):
        val = getattr(op, src)
        if val is not None:
            d[key] = val
    return d


if __name__ == "__main__":
    unittest.main()
