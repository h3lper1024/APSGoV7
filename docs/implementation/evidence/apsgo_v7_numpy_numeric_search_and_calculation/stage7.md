# 阶段 7：完整数值评价内核

## 7.1 契约与迁移参考（实施前 a03fdf5）

当前授权持续实施 7.1～7.4，完成串行接线后停止，不进入阶段 8。Ponytail 已关闭。本机为 macOS arm64，全部运行使用 Conda `aps_3.10.18`。

迁移参考冻结在 `tests/core/numeric_migration_reference_rules.py` 和 `numeric_migration_reference_evaluation.py`：直接保留实施前整数计算函数，借用生产边界类型，不是旧 Decimal 口径，也不允许生产导入。24 个固定小方案的全部规则明细、资源、时钟及九级评分一致。

真实窗口通过 `tools/verify_numeric_kernel_migration.py` 正常执行冻结请求前置流程取得，不新增恢复接口、不重置预算。`diagnostics/numpy_numeric_search_and_calculation/stage7_reference_window_01/window.json` 已保存 256 个实际后置精修候选：记录候选序号、方案代次/身份、任务/规则身份、结构描述、资源序号和质量。工具每次重新执行原前缀，因此搜索内部游标也由原流程产生，而不是从不完整快照猜测恢复。

### 数值契约与覆盖表

规则覆盖沿用阶段 0/2 的全部 20 类编译支持，不限 GQGA4 正式启用组合。内核只接收只读数值数组和原生标量；规则配置对象、字符串身份、回调、对象数组留在边界。

| 规则/事实 | 输入列/参数 | 汇总输出 | 明细输出/验证 |
|---|---|---|---|
| 5 类边规则 | 宽厚、缺值、温区、角色、软硬/热轧编码；区间开闭/首命中与容差 | 禁止数/严重度/规则命中数 | 规则、原因、链及相邻位置；阶段 2 全规则边界 + 迁移差分 |
| 高表面/窄钢/同规格/虚拟连续 | 派生匹配列、分组、重量/数量阈值 | 最大段及禁止汇总 | 每段起止、严重度，空表面断段 |
| 链重/逆宽计数/连续逆宽/跨虚拟端点 | 链节点偏移、重量、宽度/缺值、角色、阈值 | 欠重数/逐链两位缺口、禁止汇总 | 按原规则顺序与主体位置保留 |
| 延后期/虚拟比例/填充/链间宽差 | 来源/排产期、链重、真实/虚拟重、首尾宽 | 禁止及指标汇总 | 比例分子分母、逐个延后节点、跨期边界 |
| 构造优先级 | 派生优先级数值列 | 节点指标合计，不进评分 | 同规则指标编号 |
| 交期/资源 | 原单映射、节点工时、原单交期/旧欠、重量 | 原单完成、旧欠清空秒、吨秒负担、真实/虚拟/借用重 | 原单末片完成、毫秒时钟；两类拆单/桥接沿用资源测试 |
| 受控拆分资格 | 角色、窄钢、片重/数量、模式和目标期 | 不额外进入方案评分 | 保留既有授权事务；完整候选评价覆盖授权后片段 |

固定返回状态区分成功、结构无效、数值错误、取消和明细容量不足；不得把错误当普通拒绝。质量输出固定九项，按既有声明重排。链缓存保存原始数值汇总、事实和规则命中数；仅可信未变链可复用。明细缓冲列包含规则、原因/指标、链/位置、严重度/数值及处置；容量不足由外层扩容重算，不能截断。

### 验证

- `PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/core/test_numeric_kernel_migration.py`：1 项通过，内含 24 个完整方案。
- 同环境执行 `tools/verify_numeric_kernel_migration.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --output-dir diagnostics/numpy_numeric_search_and_calculation/stage7_reference_window_01`：退出 0，256 个真实完整候选逐项一致。
- 本项未改生产求解器，不宣称内核或性能完成。共享残留仍为已登记的 8 项缺失；正式配置/数据库和 4 份外部改动文件保持不动。
