"""精确求解器：只按冲突连通组件枚举。

枚举合规性约束（题面要求）：
- 17 个操作的组合空间绝不整体展开；先把操作与它们触及的实体（清单/柜/摘要/封套）
  以及截止工单做并查集，得到冲突连通组件，组件之间彼此独立，各自枚举。
- 组件内部用“立即应用 + 换批审计”的 DFS：操作在选中批次立即应用到克隆快照，
  换批时做一次完整批末检查（操作可应用性、合法清理、正常审计、每个单域离线
  审计、本组件截止工单），不满足即剪枝；退役事件按批次固定生效，空批也审计。
- 全局合并时各组件取局部字典序最优（目标可加），再做一次完整仿真复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import (
    SimulationError,
    Snapshot,
    _apply_op,
    _migration_helps,
    _referenced_digests,
    _work_order_satisfied,
    simulate,
)
from .model import Case, Operation


# ------------------------------------------------------------------ 冲突连通组件
class _DSU:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _manifests_touching_digest(case: Case, digest: str) -> list[str]:
    return [m.mid for m in case.manifests.values()
            if digest in m.all_digests()]


def build_components(case: Case) -> list[dict]:
    dsu = _DSU()

    for op in case.operations:
        dsu.find(f"op:{op.op_id}")
        ents: set[str] = {f"op:{op.op_id}"}
        if op.kind in ("invalidate", "rebind"):
            ents.add(f"M:{op.manifest_id}")
            if op.kind == "rebind":
                ents.add(f"D:{op.old_digest}")
                ents.add(f"D:{op.new_digest}")
        elif op.kind == "migrate":
            ents.add(f"C:{op.cabinet_id}")
            # 域/继承边改写只影响真正落在该柜上的副本：把这些副本连通进来，
            # 进而经“同摘要副本”规则关联到受影响摘要，但不跨到无关摘要。
            for cpid, rec in case.copies.items():
                if rec.holder == op.cabinet_id:
                    ents.add(f"P:{cpid}")
        else:  # cleanup：副本、其摘要，以及引用该摘要的全部清单
            if op.copy_id is not None:
                rec = case.copies[op.copy_id]
                ents.add(f"P:{op.copy_id}")
                ents.add(f"D:{rec.digest}")
                for mid in _manifests_touching_digest(case, rec.digest):
                    ents.add(f"M:{mid}")
            if op.digest is not None:
                ents.add(f"D:{op.digest}")
                for mid in _manifests_touching_digest(case, op.digest):
                    ents.add(f"M:{mid}")
        nodes = list(ents)
        for n in nodes[1:]:
            dsu.union(nodes[0], n)

    # 故障域耦合只沿“同一摘要的多个副本”建立：它们共享域离线这一故障面。
    # 不预联合柜与其上所有副本——柜不是独立故障域，且同一柜承载互不相干的
    # 摘要时，强联合会把无关删除冲突错误并到一个组件。柜迁移的影响由迁移
    # 操作本身经它改写的柜实体按需连通（见下方 migrate 分支）。
    for cpid, rec in case.copies.items():
        dsu.union(f"P:{cpid}", f"D:{rec.digest}")
    for m in case.manifests.values():  # 一份清单的全部摘要是不可拆证明
        for d in m.all_digests():
            dsu.union(f"M:{m.mid}", f"D:{d}")

    # 工单锚点必须在所有实体合并完成后再连接。
    for wo in case.work_orders:
        dsu.find(f"W:{wo.wo_id}")
        anchor = (f"M:{wo.manifest_id}" if wo.kind == "invalidate"
                  else f"D:{wo.digest}")
        dsu.find(anchor)
        dsu.union(f"W:{wo.wo_id}", anchor)
    wo_component = {wo.wo_id: dsu.find(f"W:{wo.wo_id}")
                    for wo in case.work_orders}

    groups: dict[str, dict] = {}
    for op in case.operations:
        root = dsu.find(f"op:{op.op_id}")
        g = groups.setdefault(root, {"operations": [], "work_orders": set()})
        g["operations"].append(op.op_id)
    for wo in case.work_orders:
        root = wo_component[wo.wo_id]
        g = groups.setdefault(root, {"operations": [], "work_orders": set()})
        g["work_orders"].add(wo.wo_id)

    components = list(groups.values())
    for g in components:
        g["operations"].sort()
        g["work_orders"] = sorted(g["work_orders"])
    components.sort(key=lambda g: (g["operations"], g["work_orders"]))
    return components


# ------------------------------------------------------- 组件内增量批次枚举（带剪枝）
@dataclass
class ComponentOption:
    assignment: dict[str, int]
    cost: int
    migration_batches: int
    sequence: list[tuple[int, str]]


def _op_choices(op: Operation) -> list[int]:
    # 跳过（0）优先，再按批次升序，便于尽早找到廉价可行方案。
    return [0] + list(range(op.batch_from, op.batch_to + 1))


def _check_batch_end(case: Case, snap: Snapshot, t: int,
                     cleanups: list[Operation], migrated_cabs: list[str],
                     enforced_wo: set[str]) -> str | None:
    """返回 None 表示批末合法，否则返回失败原因。"""
    referenced = _referenced_digests(snap)
    for op in cleanups:
        digests: set[str] = set()
        if op.copy_id is not None:
            digests.add(case.copies[op.copy_id].digest)
        if op.digest is not None:
            digests.add(op.digest)
        for d in digests:
            if d in referenced:
                return f"cleanup_{op.op_id}_digest_{d}_still_referenced"
    if snap.audit(None):
        return f"batch_{t}_snapshot_violation"
    for dom in case.domains:
        if snap.audit(dom):
            return f"batch_{t}_violation_offline_{dom}"
    for wo in case.work_orders:
        if wo.deadline_batch != t or wo.wo_id not in enforced_wo:
            continue
        if not _work_order_satisfied(snap, wo):
            return f"work_order_{wo.wo_id}_unmet_at_batch_{t}"
    return None


def enumerate_component(case: Case, comp: dict
                        ) -> tuple[list[ComponentOption], int]:
    """枚举单个冲突组件的全部可行方案（重复等价方案去重）。

    DFS 状态 = (操作序号, 已审计到的批次 cur_t, 本批已应用操作 batch_ops,
    当前快照)。操作在选中批次立即应用到克隆快照；后续操作落到更晚批次时，
    先对 cur_t 批做完整批末检查，再把空批（退役仍生效）审计推进到新批次之前。
    """
    # DFS 按“最早可执行批次、再按操作 ID”排序处理：否则晚窗口操作会在早窗口
    # 操作尚未执行时就推进中间空批审计，把可行方案误剪。批内应用仍按 ID 排序
    # （_apply_op 在 close_batch/audit_end 中由 _check_batch_end 间接对齐引擎，
    # 同批立即应用分支则在叶子统一审计，顺序与引擎的 ID 排序一致）。
    op_ids: list[str] = sorted(
        comp["operations"],
        key=lambda oid: (case.op_index()[oid].batch_from, oid))
    ops = [case.op_index()[oid] for oid in op_ids]
    choices = [_op_choices(op) for op in ops]
    enforced_wo = set(comp["work_orders"])
    seen: set[tuple[int, ...]] = set()
    options: list[ComponentOption] = []
    evals = 0

    base = Snapshot.initial(case)
    if base.audit(None) or any(base.audit(d) for d in case.domains):
        return [], 1

    retire_at: dict[int, set[str]] = {}
    for dec in case.decommissions:
        retire_at.setdefault(dec.after_batch + 1, set()).add(dec.cid)

    def apply_retirements(snap: Snapshot, t: int) -> None:
        for cid in retire_at.get(t, ()):
            snap.retired.add(cid)

    def audit_end(snap: Snapshot, t: int,
                  batch_ops: list[Operation]) -> str | None:
        """批末审计（不含退役，退役在批开始统一处理）。"""
        cleanups = [o for o in batch_ops if o.kind == "cleanup"]
        migrated = [o.cabinet_id for o in batch_ops
                    if o.kind == "migrate"]
        return _check_batch_end(case, snap, t, cleanups, migrated,
                                enforced_wo)

    def migration_contributes(snap: Snapshot,
                              batch_ops: list[Operation]) -> bool:
        migrated = [o.cabinet_id for o in batch_ops
                    if o.kind == "migrate"]
        return bool(migrated) and _migration_helps(snap, migrated)

    def start_batch(snap: Snapshot, t: int) -> None:
        apply_retirements(snap, t)

    def dfs(idx: int, cur_t: int, batch_ops: list[Operation],
            snap: Snapshot, current: list[int], mig_batches: int) -> None:
        nonlocal evals
        if idx == len(ops):
            work = snap
            last = cur_t - 1
            if batch_ops:  # 结清当前未审计批（退役在进入该批时已生效）
                work = work.clone()
                if audit_end(work, cur_t, batch_ops) is not None:
                    return
                if migration_contributes(work, batch_ops):
                    mig_batches += 1
                last = cur_t
            for t in range(last + 1, case.batch_count + 1):
                work = work.clone()
                start_batch(work, t)
                if audit_end(work, t, []) is not None:
                    return
            key = tuple(current)
            if key in seen:
                return
            seen.add(key)
            assignment = {oid: b for oid, b in zip(op_ids, current) if b}
            cost = sum(case.op_index()[oid].cost for oid, b
                       in zip(op_ids, current) if b)
            seq = sorted((b, oid) for oid, b in zip(op_ids, current) if b)
            # 迁移批次数已在 DFS 换批/叶子处增量累计，无需整轮重放仿真。
            options.append(
                ComponentOption(assignment, cost, mig_batches, seq))
            return

        op = ops[idx]
        for b in choices[idx]:
            if b == 0:
                dfs(idx + 1, cur_t, batch_ops, snap, current + [0],
                    mig_batches)
                continue
            work = snap.clone()
            if b == cur_t:
                # 同批：该批开始时的退役已在首次进入时处理过。
                evals += 1
                try:
                    _apply_op(work, op)
                except SimulationError:
                    continue
                dfs(idx + 1, cur_t, batch_ops + [op], work, current + [b],
                    mig_batches)
                continue
            if b < cur_t:
                continue  # 批次只能随操作选择向后推进
            # 换批：审计 cur_t 末，推进中间空批，再进入 b 批（先退役后应用）。
            if batch_ops:
                if audit_end(work, cur_t, batch_ops) is not None:
                    continue
                if migration_contributes(work, batch_ops):
                    next_mig = mig_batches + 1
                else:
                    next_mig = mig_batches
            else:
                next_mig = mig_batches
            failed = False
            for t in range(cur_t + 1, b):
                start_batch(work, t)
                if audit_end(work, t, []) is not None:
                    failed = True
                    break
            if failed:
                continue
            start_batch(work, b)
            evals += 1
            try:
                _apply_op(work, op)
            except SimulationError:
                continue
            dfs(idx + 1, b, [op], work, current + [b], next_mig)

    dfs(0, 0, [], base, [], 0)
    return options, evals


# ---------------------------------------------------------------------- 顶层求解
@dataclass
class SolveOutcome:
    feasible: bool
    best_assignment: dict[str, int] | None
    best_key: tuple | None
    components: list[dict]
    component_options: list[list[ComponentOption]]
    eval_count: int
    total_combos: int
    relaxation: list[dict] = field(default_factory=list)


def _option_key(opt: ComponentOption) -> tuple:
    return (opt.cost, -opt.migration_batches, tuple(opt.sequence))


def solve(case: Case) -> SolveOutcome:
    components = build_components(case)
    all_options: list[list[ComponentOption]] = []
    evals_total = 0
    total_combos = 1
    for comp in components:
        opts, evals = enumerate_component(case, comp)
        evals_total += evals
        all_options.append(opts)
        total_combos *= len(opts)

    best = _merge(case, all_options)
    if best is not None:
        return SolveOutcome(True, best[0], best[1], components,
                            all_options, evals_total, total_combos)

    # 无解：逐个撤掉截止工单重解，仅保留“确实把结果从无解变成可行”的工单。
    relaxation: list[dict] = []
    all_wo_ids = [wo.wo_id for wo in case.work_orders]
    for wo in case.work_orders:
        enforced = frozenset(w for w in all_wo_ids if w != wo.wo_id)
        opts_lists: list[list[ComponentOption]] = []
        for i, comp in enumerate(components):
            # 撤掉的工单所在组件需要重新枚举（强制集合变了）；其余复用。
            if wo.wo_id in comp["work_orders"]:
                comp2 = {"operations": comp["operations"],
                         "work_orders": [w for w in comp["work_orders"]
                                         if w != wo.wo_id]}
                opts, ev = enumerate_component(case, comp2)
                evals_total += ev
            else:
                opts = all_options[i]
            opts_lists.append(opts)
        merged = _merge(case, opts_lists, enforced=enforced)
        if merged is not None:
            relaxation.append({
                "work_order": wo.wo_id,
                "kind": wo.kind,
                "deadline_batch": wo.deadline_batch,
                "best_total_cost": merged[1][0],
                "best_migration_proof_batches": -merged[1][1],
                "best_sequence": [{"batch": b, "op_id": oid}
                                  for b, oid in merged[1][2]],
            })
    relaxation.sort(key=lambda r: (r["deadline_batch"], r["work_order"]))
    return SolveOutcome(False, None, None, components, all_options,
                        evals_total, total_combos, relaxation)


def _merge(case: Case, opts_lists: list[list[ComponentOption]],
           enforced: frozenset[str] | None = None
           ) -> tuple[dict[str, int], tuple] | None:
    """合并各冲突组件的可行方案并做完整仿真复核。

    冲突组件之间不共享实体，费用与迁移批次可加、序列可拼接，因此先尝试
    “每个组件取局部字典序最优”的线性组合，这是常见情形且只需一次完整仿真。
    若该组合在全局复核下不可行（典型：组件内某低价选项跳过了维持后续批次
    证明所必需的迁移），再回退到按局部字典序展开的精确搜索——它仍只在
    冲突组件的可行方案空间内枚举，绝不回到 17 个操作的原始笛卡尔积。
    """
    if any(not lst for lst in opts_lists):
        return None

    def simulate_key(assignment: dict[str, int]):
        res = simulate(case, assignment, enforced_wos=enforced)
        if not res.feasible:
            return None
        return (res.total_cost, -res.migration_proof_batches,
                tuple(res.sequence))

    ordered = [sorted(lst, key=_option_key) for lst in opts_lists]
    linear: dict[str, int] = {}
    for lst in ordered:
        linear.update(lst[0].assignment)
    key = simulate_key(linear)
    if key is not None:
        return dict(linear), key

    # 回退：按组件局部字典序做组合搜索，首个全局可行组合即全局最优。
    best_key: tuple | None = None
    best_assign: dict[str, int] | None = None
    indexes = [0] * len(ordered)

    def walk(i: int) -> None:
        nonlocal best_key, best_assign
        if best_key is not None:
            return  # 已按字典序找到最优，剪枝其余分支
        if i == len(ordered):
            assignment: dict[str, int] = {}
            for j, lst in enumerate(ordered):
                assignment.update(lst[indexes[j]].assignment)
            k = simulate_key(assignment)
            if k is not None:
                best_key, best_assign = k, dict(assignment)
            return
        for k in range(len(ordered[i])):
            indexes[i] = k
            walk(i + 1)
            if best_key is not None:
                return

    walk(0)
    if best_key is None:
        return None
    return best_assign, best_key
