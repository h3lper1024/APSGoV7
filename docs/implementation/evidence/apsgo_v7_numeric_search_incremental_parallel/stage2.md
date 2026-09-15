# 第二阶段：缓存、精确增量与按需评价

## 2.1 资源与连续段复用

- 开始提交：`9ad3289`；macOS / Conda `aps_3.10.18`，不改配置、SQLite、依赖或原产物。
- 接入既有接受方案缓存：未变链复用资源分类，按候选新链序重组借用/虚拟/分区明细。重量使用原 `sum_weights` 的精确求和，可安全按链合并；不使用 28 位减旧加新。
- 高表面/连续虚拟数量、窄钢/同规格连续重量复用实际节点对象绑定的分类和完整连续段重量。变化后的分组和违规位置仍按新序构造；连接、逆宽基准和跨虚拟端点继续原检查。不是把所有状态规则改为常数时间合并。
- 缓存仅保留接受快照和当前候选事实，候选返回前切断历史引用；拒绝不提升。未知规则继续原完整评价，独立审计无缓存。
- 共享必要检查：`python -m pytest -q -p no:cacheprovider tests/core/test_incremental_evaluation.py tests/core/test_plan_evaluation.py tests/core/rules/test_high_surface_run_count.py tests/core/rules/test_same_spec_continuous_real_weight.py tests/core/rules/test_continuous_narrow_steel_weight.py tests/core/rules/test_consecutive_virtual_material.py`，退出 0，228 项 / 0.27 秒。均使用 `PYTHONDONTWRITEBYTECODE=1` 和指定 Conda 解释器。
- 暂存树导出同范围检查随提交正文记录。累计回归、交期边界与完整 40 万影子/性能重复留 2.3～2.4；当前不宣称整体提速。

## 2.2～2.4

尚未完成，持续实施；第三阶段未进入。
