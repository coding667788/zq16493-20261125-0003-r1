"""生成大规模合成夹具：常规清单至少 5 万条，操作仍不超过 17 个。

用法：
    python tools/make_large_fixture.py --manifests 50000 --output fixtures_large/case_50k.json

设计要点：
- 5 万个清单绝大多数是“静态背景”，不被任何操作触及，用于验证按
  (要求副本数, 摘要集合) 分组的审计在大规模下仍精确且可行。
- 背景摘要池的副本分布在 4 个故障域，保证任一单域离线时每个摘要仍有
  3 个独立域，满足要求 2；少量清单要求 3 份以覆盖分组维度。
- 16 个失效操作各自作用于一份“私有摘要 + 独立域副本”的清单，互不共享
  实体，因此在冲突并查集里拆成 16 个单操作组件；再加 1 个独立清理组件，
  共 17 个操作。枚举规模因此是各小组件的线性乘积，而非 2^16 整体展开。
"""

from __future__ import annotations

import argparse
import json
import os


def build(manifest_count: int) -> dict:
    domains = ["D1", "D2", "D3", "D4"]
    cabinets = [
        {"id": f"C{i + 1}", "domain": domains[i],
         "edges": [f"C{(i + 1) % 4 + 1}"]}
        for i in range(4)
    ]
    content_pool = [f"CT{i}" for i in range(12)]
    envelope_pool = [f"ENV{i}" for i in range(4)]

    copies: list[dict] = []
    # 背景摘要：每个在 4 个域各放一个副本；重复物理摘要仍是独立副本记录。
    for digest in content_pool + envelope_pool:
        for i in range(4):
            copies.append({"id": f"{digest}-p{i + 1}", "digest": digest,
                           "holder": f"C{i + 1}"})

    # 16 份“可失效”清单各自的私有摘要，分布在 4 个域，与背景池完全隔离。
    target_count = 16
    private_digests = [f"PRIV{i:02d}" for i in range(target_count)]
    for digest in private_digests:
        for i in range(4):
            copies.append({"id": f"{digest}-p{i + 1}", "digest": digest,
                           "holder": f"C{i + 1}"})
    # 仅被第 0 份目标清单引用的封套，失效后可在批 2 合法清理。
    retire_env = "ENVRETIRE"
    for i in range(4):
        copies.append({"id": f"{retire_env}-p{i + 1}", "digest": retire_env,
                       "holder": f"C{i + 1}"})

    manifests: list[dict] = []
    target_manifest_ids = [f"MT{i:02d}" for i in range(target_count)]
    for i in range(target_count):
        manifests.append({
            "id": target_manifest_ids[i],
            "content_digests": [private_digests[i]],
            "envelope_digests": [retire_env] if i == 0 else [],
            "copies_required": 2,
        })

    # 背景清单从目标清单之后开始编号，避免与目标清单 ID 冲突。
    for i in range(manifest_count):
        c1 = content_pool[i % len(content_pool)]
        c2 = content_pool[(i + 1) % len(content_pool)]
        env = envelope_pool[i % len(envelope_pool)]
        required = 3 if i % 97 == 0 else 2
        manifests.append({
            "id": f"MB{i:06d}",
            "content_digests": [c1, c2],
            "envelope_digests": [env],
            "copies_required": required,
        })

    operations = []
    for i, mid in enumerate(target_manifest_ids):
        operations.append({
            "id": f"op-kill-{i:02d}", "kind": "invalidate",
            "manifest_id": mid, "batch_from": 1, "batch_to": 2, "cost": 1,
        })
    operations.append({
        "id": "op-purge-env0", "kind": "cleanup",
        "digest": retire_env, "batch_from": 2, "batch_to": 2, "cost": 1,
    })

    work_orders = [
        {"id": f"WO-KILL-{i:02d}", "kind": "invalidate",
         "manifest_id": mid, "deadline_batch": 2}
        for i, mid in enumerate(target_manifest_ids)
    ]

    return {
        "case_id": f"synthetic_{manifest_count}",
        "required_copies": 2,
        "batch_count": 2,
        "domains": domains,
        "root_cabinets": [c["id"] for c in cabinets],
        "cabinets": cabinets,
        "copies": copies,
        "envelopes": [{"id": f"E{i}", "digest": d}
                      for i, d in enumerate(envelope_pool + [retire_env])],
        "manifests": manifests,
        "work_orders": work_orders,
        "decommissions": [],
        "operations": operations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifests", type=int, default=50000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.manifests < 50000:
        parser.error("--manifests 至少 50000 才能满足规模约定")
    payload = build(args.manifests)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(f"wrote {args.output}: {len(payload['manifests'])} manifests, "
          f"{len(payload['operations'])} operations, "
          f"{len(payload['copies'])} copies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
