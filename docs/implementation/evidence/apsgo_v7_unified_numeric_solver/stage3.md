# 阶段 3：公共评价、统一候选与发布

## 3.1 公共视图与权威评价

实施前 `1519292`。阶段 2 已完成公共资源，本项使既有规则及完整评价直接读取这些私有数值列；不接新候选计算/消费/发布流程。

### 实际改动

- `column_value()`、`matrix_value()` 从普通数组或基础/私有双段数组读取同一字段；Numba 按实际类型编译，无对象模式、无运行时规则回调。`private_task_columns()` 只借用基础列及已初始化尾部，不拼接整任务、不转订单/节点对象。
- `evaluate_view_kernel()` 为唯一完整评价公式入口：直接按公共链映射读取原链和变化链。原 `evaluate_kernel()` 仅把正式平铺方案包装为零变化视图，不另写规则/交期/评分公式。批量、正式评价和旧搜索调用继续沿原入口进入该内核。
- 链规则、跨虚拟端点、资源统计、交期毫秒累计及九级评分仍沿原顺序；原链检查/方案检查/节点指标明细次序保持。输出逐节点完工数值数组属于必要评价结果，不是将输入链复制成完整候选。
- 新 `evaluate_numeric_view()` 校验基础身份、视图代次/全部数组归属、链号/期及有效区，直接返回数值汇总或同源明细。未变链缓存绑定当前权威任务/规则/目标/方案评价；内核继续比较链身份、期和节点行，不能仅凭链号复用。
- 主候选控制尚未迁移；现有 `_layout()`、旧覆盖准备和资源整体扩展仍有原调用方，不能以本次公共评价完成宣称这些旧热路径已退出。来源守恒/拆片授权和原阶段拒绝政策仍由既有候选及发布承担，下一项统一候选必须迁移而不是省略它们。

### 验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_kernel_migration.py

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_view_evaluation.py

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_*.py tests/architecture
```

冻结同标准参考专项 10 项通过、51.38 秒；首轮视图专项 7 项通过、71.24 秒（包含首次不同数组类型的编译）。随后补全数组归属/初始化区保护及借用引用见证，最终集中和同范围精确导出的实际数量/耗时/退出码见本项提交正文，不把前述时间当运行性能。

见证包括：正式零变化与两条变化链；未变链复用计数 0/2 条重算；单/双虚拟尾部；同期间和未来归还拆片的完工与逐项明细；过期视图/外来基础数组/错误前次评价/未初始化行/未知期拒绝；禁用正式方案构造、任务扩展和旧拼全方案入口时仍可评价；基础和私有列真实共享内存、实际无对象编译。冻结参考与完整明细保持，不改旧预期值。

未执行新的 40 万次完整排程或性能实验，没有开始并行。阶段 3.2 单次候选计算、3.3 消费发布尚未完成；阶段 3 不标整体完成。正式配置/SQLite、四份用户文件和历史诊断不变，旧交期退步仍为独立未关闭问题。
