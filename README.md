# archivecut — 数字保全删除计划精确求解器

面向馆里数字保全（digital preservation）工作的 **archivecut** 规划工具。
在一份带费用与批次窗口的操作集合上，求出满足全部保全约束与截止工单的、
字典序最优的“执行 / 跳过”计划；无解时给出“任撤一个截止工单”的松弛分析。

- 纯 Python 3.12 标准库，**完全离线**，Windows 普通 CPU 可跑。
- 命令行输入输出均为 **UTF-8 JSON**。
- 精确求解，**枚举只沿冲突连通组件展开**，绝不整体展开 17 个操作的笛卡尔积。
- 常规清单规模按 ≥ 5 万条设计与测试（见 `tests/test_scale.py`）。

---

## 1. 保全模型与语义

### 1.1 合法保全版本

每个合法保全版本由一份**必要清单（manifest）**和它要求的最少若干份
**独立可读副本**组成。清单内引用的内容摘要（content digest）与封套摘要
（envelope digest）构成**一组不可拆的证明**：只要清单仍保全，它引用的每个
摘要都必须达到该清单要求的独立副本数。一个封套可以同时保护多份清单。

### 1.2 副本、继承链与故障域

- 副本物理存放在某个**保管柜（cabinet）**中；柜之间有有向继承边，
  **继承图允许成环**。副本“可读”当且仅当从某个根柜出发，存在一条只经过
  在线柜的有向路径到达其持有柜（可能经多条不同继承链找到同一副本）。
- **保管柜不是独立故障域，域（domain）才是。** 独立性按“当前可读副本覆盖了
  多少个不同的域”判定。副本可用 `pinned_domain` 显式固定域，否则随持有柜
  的域。
- **重复物理摘要不等于共享故障域**：两条 digest 相同的副本仍是独立副本记录，
  其故障域只由各自的（固定）域决定。
- 每次交接都按一次**独立读到的快照**审计：在正常情形，以及**任意一个域
  临时离线**的情形下，每份仍保全的清单引用的每个摘要，其可读副本覆盖的
  不同域数都不得少于该清单要求的副本数。

### 1.3 批次、删除计划与截止工单

- 时间分为 `batch_count` 个批次；每个操作有 `[batch_from, batch_to]` 窗口，
  要么在窗口内某个批次执行一次，要么跳过。
- 删除计划必须在**给定的每一批操作之后**，以及**任意一个域临时离线时**，
  都保持每份仍保全清单有指定数目的独立副本。
- **截止工单（work order）**规定截止批次：
  - `invalidate`：截止批末，指定清单必须已失效。
  - `retire_digest`：截止批末，指定摘要必须已无任何副本（可读与否都算）。
- 可删除清单的内容若仍被别的保全清单引用，**只能移除这份清单自己的引用**；
  清理（cleanup）一个仍被任一保全清单引用的摘要属非法，整批回退判失败。

### 1.4 四种操作

| kind | 含义 | 关键字段 |
| --- | --- | --- |
| `invalidate` | 清单失效，只移除它自己的引用 | `manifest_id` |
| `rebind` | 把清单的某个引用重绑到**预登记**副本的摘要 | `manifest_id`, `old_digest`, `new_digest` |
| `migrate` | 柜迁移：改域、改继承边（可只改其一） | `cabinet_id`, `new_domain?`, `new_edges?` |
| `cleanup` | 合法清理：按副本 ID 或按摘要删除副本 | `copy_id?` / `digest?` |

另有固定外部事件 `decommissions`：柜在 `after_batch` 之后退役（退役在
下一批操作生效前发生），退役柜及其上副本不再可读。退役不是可松弛工单。

### 1.5 优化目标（依次比较）

1. **总费用** `total_cost`（最小）。
2. **迁移有效证明的批次数** `migration_proof_batches`（最大）：某批的迁移柜上
   仍有副本在为某份保全清单提供有效证明时，该批计 1。
3. **按 (批次, 操作 ID) 展开的序列**（字典序最小）。

---

## 2. 输入格式（UTF-8 JSON）

顶层对象字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `case_id` | string | 用例标识 |
| `required_copies` | int | 缺省副本要求（清单可逐条覆盖），≥1 |
| `batch_count` | int | 批次数，≥1 |
| `domains` | string[] | 全部故障域，唯一 |
| `root_cabinets` | string[] | 根柜 ID（缺省为全部柜） |
| `cabinets` | object[] | 柜：`id`, `domain`, `edges[]`（继承边，允许成环） |
| `copies` | object[] | 副本：`id`, `digest`, `holder`, 可选 `pinned_domain` |
| `envelopes` | object[] | 封套：`id`, `digest` |
| `manifests` | object[] | 清单：`id`, `content_digests[]`, `envelope_digests[]`, 可选 `copies_required` |
| `work_orders` | object[] | 截止工单：`id`, `kind`, `deadline_batch`, `manifest_id` 或 `digest` |
| `decommissions` | object[] | 退役事件：`cabinet_id`, `after_batch` |
| `operations` | object[] | 操作，**最多 17 个**：公共字段 `id`, `kind`, `batch_from`, `batch_to`, `cost` |

操作的类型专属字段见 1.4。所有 ID 唯一性、引用完整性、批次窗口、
摘要预登记等都在装载阶段严格校验，非法输入返回 `status=input_error`、退出码 2。

最小示例见 `fixtures/` 目录。

---

## 3. 命令行用法

推荐用隔离模式运行（忽略环境变量与当前目录隐式导入，避免被同名模块干扰）：

```bash
# 求解
python -I archivecut_cli.py plan fixtures/case_migration_proof.json -o result.json

# 复算成功路径与最优性
python -I archivecut_cli.py verify fixtures/case_migration_proof.json result.json
```

也可用模块方式（非隔离）：`python -m archivecut plan CASE`。

### plan 输出要点

- `status`：`feasible` / `infeasible`（无解退出码 1，输入错误退出码 2）。
- `objectives`：三项目标值与展开序列。
- `operation_decisions`：每个操作的 `execute`/`skip` 与执行批次。
- `batch_reports`：**每批末尾**（含第 0 批初始快照）的
  `worst_failure_domain`（**最不利故障域**及其最小裕度、最紧条目）与
  `witnesses`（逐摘要的独立域见证副本）。可用 `--witness-limit N` 限制规模。
- `enumeration`：冲突连通组件、各组件可行赋值数、审计评估次数。
- 无解时给出 `relaxation_remove_one_work_order.effective`，
  **只列实际把结果从无解变为可行的工单**及其松弛后最优解。

### verify 输出要点

复算给定 RESULT 的成功路径（逐批审计、工单、合法清理），重新独立求解以核对
最优性，输出 `verified`、`is_optimal`、目标是否一致；对无解 RESULT 则复算
松弛有效工单集合是否一致。

---

## 4. 题面两个例子的落点

### 例 1：只核对最终状态会误判同价动作

`fixtures/case_demo_transient.json`：两项同价迁移动作（批 1 或批 2 把柜 C3
并入域 D2）的**最终状态完全相同、总费用相同**，但在前一批次，早迁移会让摘要
B 的独立可读域从 4 个降到 3 个（离线裕度变小），晚迁移在该批仍保留 4 个域。
求解器对**每个批次末尾 + 每个单域离线**都审计，因此不会被最终状态蒙蔽；
`tests/test_solver.py::TransientMarginExampleTest` 复算两条路径的同价性、
批 1 的独立域差异与最终状态一致性。

### 例 2：两张工单同时删除释放空封套，但第三张保全单仍引用的内容不能删

`fixtures/case_shared_envelope.json`：M1、M2 同批失效后，它们共享的封套
`ENV` 成为无引用的空封套可在后续批次释放；而摘要 `Z` 仍被 M3 引用，
任何清理 `Z` 的动作都因 `still_referenced` 被判非法，只有先 `rebind`
解除 M3 自己的引用后才能清理。对应测试：`SharedEnvelopeExampleTest`。

### 无解与“撤一个工单”

`fixtures/case_infeasible_relax.json`：`retire_digest` 的截止批次早于清单
可能失效的最早批次，整体无解；撤掉该 retire 工单即可行，而撤掉另一张
invalidate 工单仍无解——松弛结果因此**只列 retire 工单**。

### 迁移有效证明批次

`fixtures/case_migration_proof.json`：根柜分阶段退役，不迁移会在后续批次的
单域离线审计下失败；早迁移与晚迁移同价，由迁移有效证明批次与展开序列裁决。

---

## 5. 求解方法（为什么在 17 操作 / 5 万清单下可行）

1. **冲突连通组件**：用并查集把每个操作与其触及的实体（清单、摘要、副本、
   柜）、每张截止工单与其锚点连通；同一份清单的全部摘要作为不可拆证明连通，
   同一摘要的多个副本经域离线故障面连通。组件之间不共享实体，彼此独立。
2. **组件内枚举**：DFS 按操作的最早可执行批次排序，操作在选中批次立即应用到
   克隆快照，换批时做一次完整批末检查（合法清理、正常审计、逐域离线审计、
   本组件工单），失败即剪枝；退役事件按批次固定生效，空批也审计。
3. **目标增量统计**：清单分组、摘要引用计数、迁移有效批次都在 DFS 中
   增量维护，避免每个叶子对 5 万清单全量重放。
4. **组件合并**：先取各组件局部字典序最优做一次完整仿真复核；若该组合全局
   不可行（典型为跳过必要迁移），再在组件可行方案空间内回退精确搜索。
5. **松弛分析**：整体无解时，逐个撤掉一张截止工单，仅对其所在组件重新枚举，
   只保留确实把结果变为可行的工单。

题面要求“精确求解只允许按冲突连通组件枚举”，本实现没有任何 17 操作的
整体枚举，也不设固定时间阈值；规模约定通过分组审计与增量计数满足。

---

## 6. 测试

```bash
python -I -m unittest discover -s tests -p "test_*.py"
```

- `test_engine.py`：继承环、可达性、单域离线、固定域、合法清理、重绑、迁移。
- `test_solver.py`：组件拆分/合并、批次窗口、三级目标、题面两例、松弛分析、见证。
- `test_cli.py`：plan/verify 端到端、退出码、UTF-8、篡改检测。
- `test_scale.py`：5 万清单 + 17 操作的精确求解、分组审计覆盖、组件边界。

生成自定义大夹具：

```bash
python -I tools/make_large_fixture.py --manifests 50000 \
    --output .runtime-tmp/case_50k.json
```

## 目录结构

```
archivecut/          包：model / engine / solver / cli
archivecut_cli.py    隔离模式薄入口
fixtures/            四个合成夹具（含题面两例、迁移、无解松弛）
tools/               大规模夹具生成器
tests/               unittest 套件
```
