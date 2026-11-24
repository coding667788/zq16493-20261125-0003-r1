"""archivecut：数字保全删除计划精确求解器。

模块划分：
- model：输入 JSON 的装载、校验与内存模型
- engine：快照语义、继承链可读性、批次审计与保留见证
- solver：冲突连通组件枚举、精确最优求解与截止工单松弛分析
- cli：plan / verify 两个子命令的 UTF-8 JSON 命令行入口
"""

__all__ = ["model", "engine", "solver", "cli"]
__version__ = "1.0.0"
