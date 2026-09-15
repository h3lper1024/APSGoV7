# 阶段 0：基线与对照入口

- 实施前提交：`7964d9d`；当前分支 `codex/delivery-objective-optimization`，工作树开工时干净。
- 本轮授权：阶段 0～1；第一阶段集中验证后停止，不进入缓存/按需评价或并行阶段。
- 环境：Darwin arm64，Conda `aps_3.10.18`，Python 3.10.18，NumPy 2.2.6 / Numba 0.65.1 / llvmlite 0.47.0。
- 计划第 2.2 节七个保护文件的 SHA-256 全部回读一致；没有改变输入、配置、SQLite 或历史产物。

## 已执行检查

1. 用 `tools/profile_solver_search.py --scope check` 回读组合 40 万次请求，退出 0。请求与规则身份由现有加载器重新计算；新材料位于 `diagnostics/numeric_search_incremental_parallel/stage0_request_check_01/`。
2. `tools/verify_numeric_search.py` 对原 `combined_01` 和 `combined_repeat_02` 回读、重新完整评价最终方案，重新计算全部原单精确完成时刻及片段位置；对照首轮、精修入口、最终方案/完整评价/接受轨迹、候选与完整评价计数、虚拟生成上界、拆分次数及双审计，退出 0，首个差异为空。紧凑结果见 [stage0_signatures.json](stage0_signatures.json)。
3. 单候选核验复用 `tools/replay_backlog_search_witnesses.py:load_case`、原 `tests/core/search/test_width_optimization_nodes.py`、`test_width_optimization_blocks.py`、`test_chain_order_integration.py` 和受控拆单/桥接回收测试夹具；阶段 1.3 在这些既有入口上对照描述还原前后，不另建求解入口或复制算法。

完整对照命令（项目根运行；前置 `PYTHONDONTWRITEBYTECODE=1` 并使用上述 Conda Python）：

```bash
python tools/verify_numeric_search.py \
  --reference diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01 \
  --candidate diagnostics/critical_delivery_search_and_bridge_reclamation/combined_repeat_02 \
  --output docs/implementation/evidence/apsgo_v7_numeric_search_incremental_parallel/stage0_signatures.json
```

输出拒绝覆盖；重验请使用新的输出路径。工具比较到首个有名称的差异，不仅比较最终九个数。上述脚本是只读诊断，不进入生产包。

## 计数与测量边界

- 逻辑候选仍在原 `consume_candidate_check/permit` 位置扣费；数组预计算不扣额外逻辑额度。
- 第一阶段仍走全部原完整评价，完整评价计数应保持 172312；后续第二阶段才允许降低它。
- 计算视图准备次数/耗时、目录峰值、描述还原次数独立统计，不塞入旧候选或完整评价计数。
- 快速判定、回落、投机评价目前尚未实施，不用零值伪装已接线统计。
- 未优化现场完整对照已从 `7964d9d` 干净导出启动，路径 `diagnostics/numeric_search_incremental_parallel/stage0_control_full_01/`；完成结果在阶段 1.3 登记。它和历史两次样本分开，不预填成功或耗时。运行期间不同时启动另一求解/重型测试。

本阶段没有算法加速结论。提交前做薄工具断言、文件差异检查及精确导出残留检查；业务集中检查留到 1.3。
