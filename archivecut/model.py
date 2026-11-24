"""输入模型装载与严格校验。

输入是一份 UTF-8 JSON，顶层字段见 README。本模块只做结构/语义前置校验，
不做任何求解；所有 ID 在装载阶段检查唯一性与引用完整性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any


class InputError(ValueError):
    """输入 CASE 不符合约定格式时抛出。"""


@dataclass(frozen=True)
class Domain:
    name: str


@dataclass
class Cabinet:
    cid: str
    domain: str
    # 继承边：cid -> 能直接读到的下层柜列表，允许成环
    edges: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CopyRec:
    """物理副本。重复物理摘要（digest 相同）也是独立副本记录，

    其故障域只由所在/固定域决定，不因为摘要相同而共享故障域。
    """

    copy_id: str
    digest: str
    holder: str          # 当前持有柜
    pinned_domain: str | None = None  # 显式固定域；缺省随持有柜的域


@dataclass
class EnvelopeRec:
    env_id: str
    digest: str


@dataclass
class ManifestRec:
    mid: str
    content_digests: list[str]
    envelope_digests: list[str]
    copies_required: int

    def all_digests(self) -> list[str]:
        return list(self.content_digests) + list(self.envelope_digests)


@dataclass(frozen=True)
class WorkOrder:
    """截止工单。

    kind == "invalidate"：截止批次 end_of_batch 时清单必须已失效。
    kind == "retire_digest"：截止批次 end_of_batch 时该摘要必须没有任何副本，
                            且截止批次之后不允许再出现（没有操作能新建副本）。
    """

    wo_id: str
    kind: str
    deadline_batch: int
    manifest_id: str | None = None
    digest: str | None = None


@dataclass(frozen=True)
class Decommission:
    """固定外部事件（不是可松弛工单）：柜在某批次之后退役。"""

    cid: str
    after_batch: int


@dataclass
class Operation:
    op_id: str
    kind: str           # invalidate / rebind / migrate / cleanup
    batch_from: int
    batch_to: int
    cost: int
    manifest_id: str | None = None
    old_digest: str | None = None
    new_digest: str | None = None
    cabinet_id: str | None = None
    new_domain: str | None = None
    new_edges: list[str] | None = None
    copy_id: str | None = None
    digest: str | None = None


@dataclass
class Case:
    case_id: str
    required_copies: int
    batch_count: int
    root_cabinets: list[str]
    domains: list[str]
    cabinets: dict[str, Cabinet]
    copies: dict[str, CopyRec]
    envelopes: dict[str, EnvelopeRec]
    manifests: dict[str, ManifestRec]
    work_orders: list[WorkOrder]
    decommissions: list[Decommission]
    operations: list[Operation]
    known_digests: set[str] = field(default_factory=set)

    def op_index(self) -> dict[str, Operation]:
        return {op.op_id: op for op in self.operations}


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise InputError(msg)


def _as_nonempty_str(obj: Any, where: str) -> str:
    _require(isinstance(obj, str) and obj != "", f"{where} 必须是非空字符串")
    return obj


def _as_int(obj: Any, where: str) -> int:
    _require(isinstance(obj, int) and not isinstance(obj, bool),
             f"{where} 必须是整数")
    return obj


def _as_str_list(obj: Any, where: str) -> list[str]:
    _require(isinstance(obj, list), f"{where} 必须是数组")
    out: list[str] = []
    for i, item in enumerate(obj):
        out.append(_as_nonempty_str(item, f"{where}[{i}]"))
    return out


def load_case(path: str) -> Case:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise InputError(f"找不到输入文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"JSON 解析失败: {exc}") from exc
    return parse_case(raw)


def parse_case(raw: Any) -> Case:
    _require(isinstance(raw, dict), "顶层必须是 JSON 对象")
    case_id = _as_nonempty_str(raw.get("case_id"), "case_id")

    required_copies = _as_int(raw.get("required_copies", 2), "required_copies")
    _require(required_copies >= 1, "required_copies 至少为 1")
    batch_count = _as_int(raw.get("batch_count"), "batch_count")
    _require(batch_count >= 1, "batch_count 至少为 1")

    domains_raw = raw.get("domains", [])
    domains = _as_str_list(domains_raw, "domains")
    _require(len(set(domains)) == len(domains), "domains 存在重复")
    domain_set = set(domains)

    cabinets_raw = raw.get("cabinets", [])
    _require(isinstance(cabinets_raw, list), "cabinets 必须是数组")
    cabinets: dict[str, Cabinet] = {}
    for i, item in enumerate(cabinets_raw):
        where = f"cabinets[{i}]"
        _require(isinstance(item, dict), f"{where} 必须是对象")
        cid = _as_nonempty_str(item.get("id"), f"{where}.id")
        _require(cid not in cabinets, f"柜 ID 重复: {cid}")
        dom = _as_nonempty_str(item.get("domain"), f"{where}.domain")
        _require(dom in domain_set, f"{where} 的域 {dom} 未在 domains 登记")
        edges = _as_str_list(item.get("edges", []), f"{where}.edges")
        cabinets[cid] = Cabinet(cid=cid, domain=dom, edges=edges)
    _require(cabinets, "至少需要一个保管柜")
    for cab in cabinets.values():
        for e in cab.edges:
            _require(e in cabinets, f"柜 {cab.cid} 的继承边指向未知柜 {e}")

    roots = _as_str_list(raw.get("root_cabinets", list(cabinets)),
                         "root_cabinets")
    _require(roots, "root_cabinets 不能为空")
    for r in roots:
        _require(r in cabinets, f"根柜 {r} 未在 cabinets 登记")

    copies_raw = raw.get("copies", [])
    _require(isinstance(copies_raw, list), "copies 必须是数组")
    copies: dict[str, CopyRec] = {}
    known_digests: set[str] = set()
    for i, item in enumerate(copies_raw):
        where = f"copies[{i}]"
        _require(isinstance(item, dict), f"{where} 必须是对象")
        cpid = _as_nonempty_str(item.get("id"), f"{where}.id")
        _require(cpid not in copies, f"副本 ID 重复: {cpid}")
        digest = _as_nonempty_str(item.get("digest"), f"{where}.digest")
        holder = _as_nonempty_str(item.get("holder"), f"{where}.holder")
        _require(holder in cabinets, f"{where} 的持有柜 {holder} 未知")
        pinned = item.get("pinned_domain")
        if pinned is not None:
            pinned = _as_nonempty_str(pinned, f"{where}.pinned_domain")
            _require(pinned in domain_set,
                     f"{where} 的固定域 {pinned} 未在 domains 登记")
        copies[cpid] = CopyRec(copy_id=cpid, digest=digest, holder=holder,
                               pinned_domain=pinned)
        known_digests.add(digest)

    envelopes_raw = raw.get("envelopes", [])
    _require(isinstance(envelopes_raw, list), "envelopes 必须是数组")
    envelopes: dict[str, EnvelopeRec] = {}
    for i, item in enumerate(envelopes_raw):
        where = f"envelopes[{i}]"
        _require(isinstance(item, dict), f"{where} 必须是对象")
        eid = _as_nonempty_str(item.get("id"), f"{where}.id")
        _require(eid not in envelopes, f"封套 ID 重复: {eid}")
        digest = _as_nonempty_str(item.get("digest"), f"{where}.digest")
        envelopes[eid] = EnvelopeRec(env_id=eid, digest=digest)
        known_digests.add(digest)

    manifests_raw = raw.get("manifests", [])
    _require(isinstance(manifests_raw, list), "manifests 必须是数组")
    manifests: dict[str, ManifestRec] = {}
    for i, item in enumerate(manifests_raw):
        where = f"manifests[{i}]"
        _require(isinstance(item, dict), f"{where} 必须是对象")
        mid = _as_nonempty_str(item.get("id"), f"{where}.id")
        _require(mid not in manifests, f"清单 ID 重复: {mid}")
        contents = _as_str_list(item.get("content_digests", []),
                                f"{where}.content_digests")
        envs = _as_str_list(item.get("envelope_digests", []),
                            f"{where}.envelope_digests")
        need = _as_int(item.get("copies_required", required_copies),
                       f"{where}.copies_required")
        _require(need >= 1, f"{where}.copies_required 至少为 1")
        for d in contents + envs:
            _require(d in known_digests,
                     f"{where} 引用的摘要 {d} 没有任何预登记副本/封套")
        manifests[mid] = ManifestRec(mid=mid, content_digests=contents,
                                     envelope_digests=envs, copies_required=need)

    wos_raw = raw.get("work_orders", [])
    _require(isinstance(wos_raw, list), "work_orders 必须是数组")
    work_orders: list[WorkOrder] = []
    wo_ids: set[str] = set()
    for i, item in enumerate(wos_raw):
        where = f"work_orders[{i}]"
        _require(isinstance(item, dict), f"{where} 必须是对象")
        wid = _as_nonempty_str(item.get("id"), f"{where}.id")
        _require(wid not in wo_ids, f"工单 ID 重复: {wid}")
        wo_ids.add(wid)
        kind = _as_nonempty_str(item.get("kind"), f"{where}.kind")
        _require(kind in ("invalidate", "retire_digest"),
                 f"{where}.kind 非法: {kind}")
        deadline = _as_int(item.get("deadline_batch"),
                           f"{where}.deadline_batch")
        _require(1 <= deadline <= batch_count,
                 f"{where}.deadline_batch 超出 1..{batch_count}")
        mid = item.get("manifest_id")
        digest = item.get("digest")
        if kind == "invalidate":
            mid = _as_nonempty_str(mid, f"{where}.manifest_id")
            _require(mid in manifests, f"{where} 的清单 {mid} 未知")
        else:
            digest = _as_nonempty_str(digest, f"{where}.digest")
            _require(digest in known_digests,
                     f"{where} 的摘要 {digest} 没有预登记副本")
        work_orders.append(WorkOrder(wo_id=wid, kind=kind,
                                     deadline_batch=deadline,
                                     manifest_id=mid, digest=digest))

    dec_raw = raw.get("decommissions", [])
    _require(isinstance(dec_raw, list), "decommissions 必须是数组")
    decommissions: list[Decommission] = []
    for i, item in enumerate(dec_raw):
        where = f"decommissions[{i}]"
        _require(isinstance(item, dict), f"{where} 必须是对象")
        cid = _as_nonempty_str(item.get("cabinet_id"), f"{where}.cabinet_id")
        _require(cid in cabinets, f"{where} 的柜 {cid} 未知")
        after = _as_int(item.get("after_batch"), f"{where}.after_batch")
        _require(0 <= after <= batch_count,
                 f"{where}.after_batch 超出 0..{batch_count}")
        decommissions.append(Decommission(cid=cid, after_batch=after))

    ops_raw = raw.get("operations", [])
    _require(isinstance(ops_raw, list), "operations 必须是数组")
    _require(len(ops_raw) <= 17, "operations 最多 17 个")
    operations: list[Operation] = []
    op_ids: set[str] = set()
    for i, item in enumerate(ops_raw):
        where = f"operations[{i}]"
        _require(isinstance(item, dict), f"{where} 必须是对象")
        oid = _as_nonempty_str(item.get("id"), f"{where}.id")
        _require(oid not in op_ids, f"操作 ID 重复: {oid}")
        op_ids.add(oid)
        kind = _as_nonempty_str(item.get("kind"), f"{where}.kind")
        _require(kind in ("invalidate", "rebind", "migrate", "cleanup"),
                 f"{where}.kind 非法: {kind}")
        bf = _as_int(item.get("batch_from"), f"{where}.batch_from")
        bt = _as_int(item.get("batch_to"), f"{where}.batch_to")
        _require(1 <= bf <= bt <= batch_count,
                 f"{where} 批次窗口必须满足 1 <= from <= to <= {batch_count}")
        cost = _as_int(item.get("cost"), f"{where}.cost")
        _require(cost >= 0, f"{where}.cost 不能为负")
        op = Operation(op_id=oid, kind=kind, batch_from=bf, batch_to=bt,
                       cost=cost)
        if kind == "invalidate":
            op.manifest_id = _as_nonempty_str(item.get("manifest_id"),
                                              f"{where}.manifest_id")
            _require(op.manifest_id in manifests,
                     f"{where} 的清单 {op.manifest_id} 未知")
        elif kind == "rebind":
            op.manifest_id = _as_nonempty_str(item.get("manifest_id"),
                                              f"{where}.manifest_id")
            _require(op.manifest_id in manifests,
                     f"{where} 的清单 {op.manifest_id} 未知")
            op.old_digest = _as_nonempty_str(item.get("old_digest"),
                                             f"{where}.old_digest")
            op.new_digest = _as_nonempty_str(item.get("new_digest"),
                                             f"{where}.new_digest")
            m = manifests[op.manifest_id]
            _require(op.old_digest in m.all_digests(),
                     f"{where}: 清单 {m.mid} 未引用 old_digest "
                     f"{op.old_digest}")
            _require(op.new_digest in known_digests,
                     f"{where}: new_digest {op.new_digest} 必须已预登记副本")
        elif kind == "migrate":
            op.cabinet_id = _as_nonempty_str(item.get("cabinet_id"),
                                             f"{where}.cabinet_id")
            _require(op.cabinet_id in cabinets,
                     f"{where} 的柜 {op.cabinet_id} 未知")
            nd = item.get("new_domain")
            if nd is not None:
                op.new_domain = _as_nonempty_str(nd, f"{where}.new_domain")
                _require(op.new_domain in domain_set,
                         f"{where} 的新域 {op.new_domain} 未登记")
            if "new_edges" in item and item["new_edges"] is not None:
                op.new_edges = _as_str_list(item["new_edges"],
                                            f"{where}.new_edges")
                for e in op.new_edges:
                    _require(e in cabinets,
                             f"{where} 新继承边指向未知柜 {e}")
        else:  # cleanup
            cpid = item.get("copy_id")
            digest = item.get("digest")
            _require(cpid is not None or digest is not None,
                     f"{where}: cleanup 必须指定 copy_id 或 digest")
            if cpid is not None:
                op.copy_id = _as_nonempty_str(cpid, f"{where}.copy_id")
                _require(op.copy_id in copies,
                         f"{where} 的副本 {op.copy_id} 未知")
            if digest is not None:
                op.digest = _as_nonempty_str(digest, f"{where}.digest")
                _require(op.digest in known_digests,
                         f"{where} 的摘要 {op.digest} 未预登记")
        operations.append(op)

    # 退役事件里同一柜的多条记录保留全部，引擎按批次逐个生效。
    return Case(
        case_id=case_id,
        required_copies=required_copies,
        batch_count=batch_count,
        root_cabinets=roots,
        domains=domains,
        cabinets=cabinets,
        copies=copies,
        envelopes=envelopes,
        manifests=manifests,
        work_orders=work_orders,
        decommissions=decommissions,
        operations=operations,
        known_digests=known_digests,
    )
