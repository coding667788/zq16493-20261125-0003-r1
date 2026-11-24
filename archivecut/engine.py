"""快照语义与批次审计引擎。

关键语义（与 README 一致）：
- 继承图允许环；可读性 = 存在一条从根柜出发、只经过在线柜的有向路径到达持有柜。
- 故障域按柜所在域判定（副本可显式固定域）；重复物理摘要不共享故障域，
  独立性只看“当前可读副本覆盖了多少个不同的域”。
- 每份仍保全（未失效）清单引用的每个摘要，都必须在正常情形和任一单域离线情形下
  拥有不少于该清单要求数目的、分布在不同域上的可读副本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .model import Case, ManifestRec, Operation, WorkOrder


class SimulationError(Exception):
    """执行阶段发现操作本身不可应用（例如清理已不存在的副本）。"""


@dataclass
class Snapshot:
    case: Case
    cab_domain: dict[str, str]
    cab_edges: dict[str, tuple[str, ...]]
    retired: set[str]
    # cpid -> 副本记录（记录本身在快照间不共享修改）
    copies: dict
    live: set[str]
    active: set[str]
    mdigests: dict[str, list[str]]
    _reach_cache: dict
    _readable_cache: dict
    # 增量分组：组键 -> 计数 / 代表清单；失效与重绑只做 O(1) 调整。
    _group_counts: dict
    _group_reps: dict
    # 增量引用计数：摘要 -> 当前被多少份活动清单引用（含内容与封套摘要）。
    _ref_counts: dict[str, int]

    @classmethod
    def initial(cls, case: Case) -> "Snapshot":
        snap = cls(
            case=case,
            cab_domain={c.cid: c.domain for c in case.cabinets.values()},
            cab_edges={c.cid: tuple(c.edges) for c in case.cabinets.values()},
            retired=set(),
            copies={cpid: rec for cpid, rec in case.copies.items()},
            live=set(case.copies),
            active=set(case.manifests),
            mdigests={m.mid: list(m.all_digests())
                      for m in case.manifests.values()},
            _reach_cache={},
            _readable_cache={},
            _group_counts={},
            _group_reps={},
            _ref_counts={},
        )
        for m in case.manifests.values():
            for d in m.all_digests():
                snap._ref_counts[d] = snap._ref_counts.get(d, 0) + 1
        snap._build_groups()
        return snap

    def clone(self) -> "Snapshot":
        # mdigests 只在 rebind 时按清单整体替换，浅拷贝即可安全共享列表，
        # 避免对 5 万清单逐个 list() 复制的高成本。
        return Snapshot(
            case=self.case,
            cab_domain=dict(self.cab_domain),
            cab_edges=dict(self.cab_edges),
            retired=set(self.retired),
            copies=dict(self.copies),
            live=set(self.live),
            active=set(self.active),
            mdigests=dict(self.mdigests),
            _reach_cache={},
            _readable_cache={},
            _group_counts=dict(self._group_counts),
            _group_reps=dict(self._group_reps),
            _ref_counts=dict(self._ref_counts),
        )

    def _adjust_refs(self, digest: str, delta: int) -> None:
        n = self._ref_counts.get(digest, 0) + delta
        if n <= 0:
            self._ref_counts.pop(digest, None)
        else:
            self._ref_counts[digest] = n

    def _invalidate_groups(self) -> None:
        # 兼容旧调用名；增量路径下分组始终保持最新，无需整体失效。
        return

    # ------------------------------------------------------------------ 图与域
    def effective_domain(self, cpid: str) -> str:
        rec = self.copies[cpid]
        if rec.pinned_domain is not None:
            return rec.pinned_domain
        return self.cab_domain[rec.holder]

    def reachable(self, offline_domain: str | None = None) -> set[str]:
        key = offline_domain
        cached = self._reach_cache.get(key)
        if cached is not None:
            return cached
        seen: set[str] = set()
        stack: list[str] = []
        for r in self.case.root_cabinets:
            if r in self.retired:
                continue
            if offline_domain is not None and self.cab_domain[r] == offline_domain:
                continue
            if r not in seen:
                seen.add(r)
                stack.append(r)
        while stack:
            cur = stack.pop()
            for nxt in self.cab_edges.get(cur, ()):  # 环由 visited 集合处理
                if nxt in seen or nxt in self.retired:
                    continue
                if offline_domain is not None and \
                        self.cab_domain[nxt] == offline_domain:
                    continue
                seen.add(nxt)
                stack.append(nxt)
        self._reach_cache[key] = seen
        return seen

    def readable_by_digest(self, offline_domain: str | None = None
                           ) -> dict[str, list[str]]:
        """摘要 -> 当前可读的存活副本 ID 列表（按 ID 排序）。"""
        key = offline_domain
        cached = self._readable_cache.get(key)
        if cached is not None:
            return cached
        reach = self.reachable(offline_domain)
        out: dict[str, list[str]] = {}
        for cpid in self.live:
            rec = self.copies[cpid]
            if rec.holder not in reach:
                continue
            if offline_domain is not None and \
                    self.effective_domain(cpid) == offline_domain:
                continue
            out.setdefault(rec.digest, []).append(cpid)
        for lst in out.values():
            lst.sort()
        self._readable_cache[key] = out
        return out

    def independent_domains(self, digest: str,
                            offline_domain: str | None = None) -> set[str]:
        readable = self.readable_by_digest(offline_domain).get(digest, ())
        return {self.effective_domain(cpid) for cpid in readable}

    # ------------------------------------------------------------ 清单分组审计
    def _group_key(self, mid: str) -> tuple[int, frozenset]:
        man = self.case.manifests[mid]
        return (man.copies_required, frozenset(self.mdigests[mid]))

    def _build_groups(self) -> None:
        """全量构建分组计数与代表清单；仅在初始快照时调用一次。"""
        counts: dict[tuple[int, frozenset], int] = {}
        reps: dict[tuple[int, frozenset], str] = {}
        for mid in self.active:
            key = self._group_key(mid)
            counts[key] = counts.get(key, 0) + 1
            if key not in reps or mid < reps[key]:
                reps[key] = mid
        self._group_counts = counts
        self._group_reps = reps

    def _move_group(self, mid: str, old_key: tuple[int, frozenset] | None,
                    new_key: tuple[int, frozenset] | None) -> None:
        """增量把清单 mid 从 old_key 组移到 new_key 组（None 表示移出/新增）。"""
        if old_key is not None:
            self._group_counts[old_key] -= 1
            if self._group_counts[old_key] == 0:
                del self._group_counts[old_key]
                self._group_reps.pop(old_key, None)
        if new_key is not None:
            self._group_counts[new_key] = self._group_counts.get(new_key, 0) + 1
            cur = self._group_reps.get(new_key)
            if cur is None or mid < cur:
                self._group_reps[new_key] = mid

    def _groups(self) -> dict[tuple[int, frozenset], tuple[str, int]]:
        """按 (要求副本数, 摘要集合) 分组，返回 -> (代表清单, 组内条数)。"""
        return {key: (self._group_reps[key], count)
                for key, count in self._group_counts.items()}

    def audit(self, offline_domain: str | None = None) -> list[dict]:
        """返回违规列表；空列表即该快照在该离线情形下合法。"""
        violations: list[dict] = []
        readable = self.readable_by_digest(offline_domain)
        for (required, digests), (rep, count) in self._groups().items():
            for d in sorted(digests):
                doms = {self.effective_domain(c)
                        for c in readable.get(d, ())}
                if len(doms) < required:
                    violations.append({
                        "manifest": rep,
                        "affected_manifests": count,
                        "digest": d,
                        "required": required,
                        "readable_domains": sorted(doms),
                        "offline_domain": offline_domain,
                    })
        return violations

    def min_margin(self, offline_domain: str | None) -> tuple[int, int]:
        """返回 (最紧裕度, 违规分组数)。裕度 = 独立域数 - 要求数 的最小值。"""
        readable = self.readable_by_digest(offline_domain)
        worst = 0
        bad_groups = 0
        for (required, digests), _rep in self._groups().items():
            for d in digests:
                doms = {self.effective_domain(c)
                        for c in readable.get(d, ())}
                margin = len(doms) - required
                worst = min(worst, margin)
                if margin < 0:
                    bad_groups += 1
        return worst, bad_groups

    # ---------------------------------------------------------------- 见证生成
    def witnesses(self, limit: int | None = None) -> list[dict]:
        readable = self.readable_by_digest(None)
        out: list[dict] = []
        for mid in sorted(self.active)[:limit] if limit else sorted(self.active):
            man = self.case.manifests[mid]
            digests_out = []
            for d in self.mdigests[mid]:
                per_domain: dict[str, str] = {}
                for cpid in readable.get(d, ()):
                    dom = self.effective_domain(cpid)
                    if dom not in per_domain or cpid < per_domain[dom]:
                        per_domain[dom] = cpid
                doms_sorted = sorted(per_domain)
                digests_out.append({
                    "digest": d,
                    "required": man.copies_required,
                    "independent_domains": doms_sorted,
                    "satisfied": len(doms_sorted) >= man.copies_required,
                    "witness_copies": [
                        {"domain": dom, "copy_id": per_domain[dom]}
                        for dom in doms_sorted
                    ],
                })
            out.append({"manifest": mid, "proofs": digests_out})
        return out

    def worst_failure_domain(self) -> dict:
        """枚举每个单域离线，选出最不利故障域。"""
        best: dict | None = None
        for dom in self.case.domains:
            margin, bad_groups = self.min_margin(dom)
            key = (margin, bad_groups, dom)
            if best is None or key < best["_key"]:
                tight = self._tight_entries(dom, margin)
                best = {
                    "domain": dom,
                    "min_margin": margin,
                    "violating_groups": bad_groups,
                    "tight_entries": tight,
                    "_key": key,
                }
        assert best is not None
        best.pop("_key", None)
        return best

    def _tight_entries(self, offline_domain: str, margin: int) -> list[dict]:
        readable = self.readable_by_digest(offline_domain)
        entries: list[dict] = []
        for (required, digests), (rep, count) in self._groups().items():
            for d in sorted(digests):
                doms = sorted({self.effective_domain(c)
                               for c in readable.get(d, ())})
                if len(doms) - required == margin:
                    entries.append({
                        "manifest": rep,
                        "affected_manifests": count,
                        "digest": d,
                        "required": required,
                        "readable_domains": doms,
                    })
        return entries


# ---------------------------------------------------------------------- 操作应用
def _apply_op(snap: Snapshot, op: Operation) -> None:
    if op.kind == "invalidate":
        if op.manifest_id not in snap.active:
            raise SimulationError(f"{op.op_id}: 清单 {op.manifest_id} 已失效")
        # 增量移出该清单所在分组，再从活动集合移除；其他清单引用不受影响。
        mid = op.manifest_id
        snap._move_group(mid, snap._group_key(mid), None)
        for d in snap.mdigests[mid]:
            snap._adjust_refs(d, -1)
        snap.active.remove(mid)
    elif op.kind == "rebind":
        if op.manifest_id not in snap.active:
            raise SimulationError(f"{op.op_id}: 不能重绑已失效清单 "
                                  f"{op.manifest_id}")
        mid = op.manifest_id
        lst = snap.mdigests[mid]
        if op.old_digest not in lst:
            raise SimulationError(f"{op.op_id}: 清单 {mid} "
                                  f"当前未引用 {op.old_digest}")
        old_key = snap._group_key(mid)
        snap.mdigests[mid] = [
            op.new_digest if d == op.old_digest else d for d in lst
        ]
        # 重绑只移除这份清单自己对旧摘要的引用，并登记对新摘要的引用。
        snap._adjust_refs(op.old_digest, -1)
        snap._adjust_refs(op.new_digest, 1)
        # 重绑改变该清单的摘要集合：从旧组移到新组，其余 5 万清单不受影响。
        snap._move_group(mid, old_key, snap._group_key(mid))
    elif op.kind == "migrate":
        cid = op.cabinet_id
        if cid in snap.retired:
            raise SimulationError(f"{op.op_id}: 柜 {cid} 已退役，不能迁移")
        if op.new_domain is not None:
            snap.cab_domain[cid] = op.new_domain
        if op.new_edges is not None:
            snap.cab_edges[cid] = tuple(op.new_edges)
        # 域/继承边变化会影响可达性与可读性，清空相关缓存（分组不受影响）。
        snap._reach_cache.clear()
        snap._readable_cache.clear()
    else:  # cleanup
        targets: list[str] = []
        if op.copy_id is not None:
            if op.copy_id not in snap.live:
                raise SimulationError(f"{op.op_id}: 副本 {op.copy_id} "
                                      f"已不存在，无法清理")
            targets.append(op.copy_id)
        if op.digest is not None:
            targets.extend(c for c in snap.live
                           if snap.copies[c].digest == op.digest)
        for cpid in set(targets):
            snap.live.remove(cpid)
        snap._readable_cache.clear()


def _referenced_digests(snap: Snapshot) -> set[str]:
    # 引用计数在失效/重绑时增量维护，O(被引用摘要数) 而非 O(清单数 × 引用数)。
    return set(snap._ref_counts)


def _migration_helps(snap: Snapshot, cabinet_ids: Iterable[str]) -> bool:
    """该批迁移的柜中，是否有副本仍在为某份保全清单提供有效证明。"""
    cids = set(cabinet_ids)
    used = _referenced_digests(snap)
    reach = snap.reachable(None)
    for cpid in snap.live:
        rec = snap.copies[cpid]
        if rec.holder in cids and rec.holder in reach and rec.digest in used:
            return True
    return False


@dataclass
class SimResult:
    feasible: bool
    reason: str | None
    total_cost: int
    migration_proof_batches: int
    sequence: list[tuple[int, str]]
    batch_reports: list[dict]
    final_snapshot: Snapshot | None


def simulate(case: Case, assignment: dict[str, int],
             enforced_wos: frozenset[str] | None = None,
             detail: bool = False, witness_limit: int | None = None
             ) -> SimResult:
    """按 assignment 全量前推 assignment：op_id -> 批次（0 表示跳过）。

    detail=False 时只做求解所需的可行性与目标计算，不生成见证明细。
    任意一步失败立即返回 infeasible，保证枚举时的早停成本最低。
    enforced_wos=None 强制全部截止工单；否则只强制给定工单，其余视为
    属于别的冲突组件而跳过检查（组件局部仿真用）。
    """
    if enforced_wos is None:
        enforced = {wo.wo_id for wo in case.work_orders}
    else:
        enforced = set(enforced_wos)
    relaxed_wos = frozenset(wo.wo_id for wo in case.work_orders
                            if wo.wo_id not in enforced)
    snap = Snapshot.initial(case)
    reports: list[dict] = []

    def fail(reason: str) -> SimResult:
        return SimResult(False, reason, 0, 0, [], reports, None)

    # 初始快照（第 0 批）也必须通过正常与单域离线审计。
    if snap.audit(None):
        return fail("initial_snapshot_violation")
    for dom in case.domains:
        if snap.audit(dom):
            return fail(f"initial_snapshot_violation_offline_{dom}")
    if detail:
        reports.append(_batch_report(0, [], snap, witness_limit))

    by_batch: dict[int, list[Operation]] = {t: [] for t in
                                            range(1, case.batch_count + 1)}
    total_cost = 0
    sequence: list[tuple[int, str]] = []
    for op in case.operations:
        t = assignment.get(op.op_id, 0)
        if t == 0:
            continue
        if not (op.batch_from <= t <= op.batch_to):
            return fail(f"operation_{op.op_id}_outside_window")
        by_batch[t].append(op)
        total_cost += op.cost
        sequence.append((t, op.op_id))
    sequence.sort()

    migration_batches = 0
    for t in range(1, case.batch_count + 1):
        snap = snap.clone()
        for dec in case.decommissions:  # after_batch=t-1 的退役在本批生效
            if dec.after_batch == t - 1:
                snap.retired.add(dec.cid)
        executed: list[dict] = []
        cleanups: list[Operation] = []
        migrated_cabs: list[str] = []
        for op in sorted(by_batch[t], key=lambda o: o.op_id):
            try:
                _apply_op(snap, op)
            except SimulationError as exc:
                return fail(str(exc))
            executed.append({"op_id": op.op_id, "kind": op.kind,
                             "cost": op.cost})
            if op.kind == "cleanup":
                cleanups.append(op)
            if op.kind == "migrate":
                migrated_cabs.append(op.cabinet_id)

        # 合法清理：批末仍被任一保全清单引用的摘要，其副本不允许被本批删掉。
        referenced = _referenced_digests(snap)
        for op in cleanups:
            digests: set[str] = set()
            if op.copy_id is not None:
                digests.add(case.copies[op.copy_id].digest)
            if op.digest is not None:
                digests.add(op.digest)
            for d in digests:
                if d in referenced:
                    return fail(f"cleanup_{op.op_id}_digest_{d}_still_referenced")

        # 交接审计：正常 + 每个域临时离线。
        if snap.audit(None):
            return fail(f"batch_{t}_snapshot_violation")
        for dom in case.domains:
            if snap.audit(dom):
                return fail(f"batch_{t}_violation_offline_{dom}")

        # 截止工单（松弛分析时跳过指定工单）。
        for wo in case.work_orders:
            if wo.deadline_batch != t or wo.wo_id in relaxed_wos:
                continue
            if not _work_order_satisfied(snap, wo):
                return fail(f"work_order_{wo.wo_id}_unmet_at_batch_{t}")

        helps = _migration_helps(snap, migrated_cabs) if migrated_cabs else False
        if helps:
            migration_batches += 1
        if detail:
            report = _batch_report(t, executed, snap, witness_limit)
            report["migration_proof_contributes"] = helps
            reports.append(report)

    return SimResult(True, None, total_cost, migration_batches,
                     sequence, reports, snap)


def _work_order_satisfied(snap: Snapshot, wo: WorkOrder) -> bool:
    if wo.kind == "invalidate":
        return wo.manifest_id not in snap.active
    # retire_digest：截止批末不得再有任何副本（可读与否都算）。
    return all(cpid not in snap.live
               for cpid, rec in snap.copies.items()
               if rec.digest == wo.digest)


def _batch_report(batch_no: int, executed: list[dict], snap: Snapshot,
                  witness_limit: int | None) -> dict:
    return {
        "batch": batch_no,
        "executed_operations": executed,
        "active_manifests": sorted(snap.active),
        "retired_cabinets": sorted(snap.retired),
        "worst_failure_domain": snap.worst_failure_domain(),
        "witnesses": snap.witnesses(witness_limit),
    }
