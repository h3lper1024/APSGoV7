# 步骤 3：连续段移动与交换

实施前 `5e5eeb8`，Conda `apsgo_v6_3.10.18`，macOS / Darwin arm64 / zsh。开工工作区干净，当前提交独立导出原残留检查 `clean_export` 通过。

仅增加 `width_optimization.py` 中惰性的连续段范围枚举；移动长度至少 2，交换两侧非整链且至少一侧多于 1。沿用共享候选预算、步骤 2 的两链同时形成、规范化所属期、必要接口桥和唯一接受器。新增顺序只是描述枚举，不新增规则、评分、状态或桥策略。

## 验证命令与边界

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_width_optimization_blocks.py tests/core/search/test_width_optimization_nodes.py tests/core/search/test_width_optimization_baseline.py tests/core/search/test_width_optimization_guard.py tests/core/search/test_width_optimization_scan.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
```

先行节点及扫描回归 46 项通过（0.67 秒）。本项需由实际连续段枚举器证明单节点无法完成的交换、旧虚拟随段保留、原序不反转、内部同分拒绝及形状/顺序/计数契约；不能以手工完整候选被接受替代。

新增连续段专项 10 项通过（0.62 秒），连续段加节点专项 34 项通过（0.68 秒），首轮无失败。实际块专属样本：单节点族 30 次检查、零接受；连续段族 42 次检查、1 次完整评价/接受，将宽差 700→300 mm、两链仍各 700 吨并自然结束。已覆盖 1 对 2 与 2 对 1、尾段移动、包含 1/2 个旧虚拟节点的原样移动、纯虚拟段拒绝、115 个惰性描述形状/顺序及内部同分预筛。上述是启用指定真实规则的小样本，不是完整 GQGA4 运行。

首轮主聚焦 132 项通过（0.85 秒）；累计检查发现原架构门禁把字面数字 3 统一标为基准数据，命中了“两段总长度至少 3”的实现。未改门禁或断言，枚举改为两个非空段并显式排除已有单节点族的“一对一交换”，输出集合和顺序不变；架构加连续段复验 75 项通过（1.04 秒）。独立枚举核验覆盖两链长度 1～6 的 36 种组合，与直接完整范围集合逐项比较，无遗漏、重复或隐藏计数。

首次导出 `1f6caf6d1adc603c5e1c1b173ecf23ff24253003` 验证不合格：并行运行的格式检查先创建了导出目录内的 `.ruff_cache` 元数据，使原残留检查退出 1，累计测试尚未开始。此副本不作为最终验证，不删除其内容以伪造通过；重新导出修正后的暂存树，先完成残留检查，格式检查显式使用 `--no-cache` 并在保护验证后执行。原冻结清单和保护工具完全不改。

共享树和最终暂存树执行相同聚焦及累计回归；原残留检查只在干净导出运行。Ruff 复用本机已有安装；实际退出码、数量、耗时、最终树由本项提交正文保留。本步不接公共求解流程，也不声称实际 GQGA4 收益或性能验收通过。
