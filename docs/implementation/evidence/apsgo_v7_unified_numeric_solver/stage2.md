# 阶段 2：连接修复与私有资源

## 前置修复：非自适应温区下的无单材解

实施前 `473596c`。阶段 2.1 小例发现原 `choose_virtual_bridge()` 非静态分支只在找到可用单材后才赋值 `best`；没有单材时进入双材分支前抛出 `UnboundLocalError`。已在阶段 0 独立冻结源码 `/tmp/apsgo-unified-stage0-V92qIO` 复现，退出 1，不是新数值实现引入。

见证为真实端点宽度 1000/400、温区 700～710/800～810，虚拟原型宽度 800/600，关闭虚拟温度自适应。原宽度约束下一个原型不能连接，两个原型可以；不是修改温度或宽度配置来放行。

用户已明确确认“允许修复，并继续推进”。仅在该分支扫描前初始化 `best = None`；候选枚举、严格平滑度择优、参数和规则保持。最多一材时返回无解，最多两材时选择原型 0、1；虚拟材温区仍按原端点派生为 700～810。当前冻结 GQGA4 使用自适应温区，不进入本故障分支。

必要验证（共享树和独立精确树同范围）：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_bridge_nonadaptive.py tests/core/test_numeric_search.py
```

实际数量、耗时、退出码和精确树写入本项 Git 提交正文，不在文档中自引用当前提交。只暂存这一行生产修复、独立回归和三份记录；正在开发的 2.1 公共实现不包含在本修复提交中。未执行完整求解、不宣称阶段 2 完成或提速；原配置/SQLite、用户文件及历史产物不变。

## 2.1 共用边判定与私有桥接

前置错误修复为 `795a107`；公共资源实现从 `473596c` 开始。本步骤只接通共用原型边公式，私有资源尚未切换进主求解。

- `edge_allowed_values()` 是既有五类边规则的唯一布尔公式；正式行和私有行都按相同物理/类别/有效掩码读取，原 NumPy 原型快速掩码改调该内核。不新增规则或删掉链级跨虚拟检查。
- 私有桥接先直接、再有序单材、最后有序双材；双材中间边在原平滑指标中计算两次，同分仅保留首个原型组合。温区仍由两个真实或私有锚点派生，不回查订单对象。
- 原型扫描每次有限工作量，用数值游标续算；`MORE_WORK` 仅表示内部尚未扫描完，不是新的公开状态。外层取消不分配节点、不消费候选额度；容量不足可从同基准重算。
- 只将最终选中的原型写入私有全字段/派生列，记录原扩展事件边界。无正式节点对象、任务整体扩展或指纹计算；目前尚未实现拆片或接受发布。
- 实际编译最初发现角色字段 `int8` 与错误分支 `int64` 返回类型不能统一，已在数值读取处明确转为 `int64`；不改变权威列存储类型或业务值。非自适应旧错误按用户授权单独修复，而非修改期望绕过。

### 冻结真实原型对照

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_resources.py \
  --source-root /tmp/apsgo-unified-stage0-V92qIO \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage21_reference_02

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_resources.py \
  --source-root /Users/miles/dev/dev-py/APSGOV7 --private \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage21_private_02

cmp diagnostics/unified_numeric_solver/stage21_reference_02/samples.json \
    diagnostics/unified_numeric_solver/stage21_private_02/samples.json
```

三项退出 0。531 单逐单取原序后第 1/7 个端点，分别尝试最多 1/2 个虚拟材，共 2124 组：直接 372、无解 982、单材 676、双材 94。全部节点字段和规则派生列、选择结果一致，两个样本文件 SHA-256 均为 `1a00b574dc3f431b8e5109b6eca2129bd740d6b1bfdcd7175e5717afaa350d1c`。这是固定边样本对照，不是穷尽所有订单对或完整排程。

原工具首次运行在写测量摘要时因浮点 JSON 类型失败，样本已写出；私有首次运行因临时方案遗漏原订单被来源守恒拒绝。已修正工具为 Decimal 测量及全原单基准方案，失败目录 `stage21_reference_01`/`stage21_private_01` 保留，不记通过。未修改真实输入或生产守恒检查。此对照含首次编译、字段导出等，3.04/8.06 秒不是性能收益判定。

### 必要集中验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_private_resources.py tests/core/test_numeric_bridge_nonadaptive.py \
  tests/core/test_numeric_rules.py tests/core/test_numeric_kernel_migration.py \
  tests/core/test_numeric_state.py tests/core/test_numeric_workspace.py tests/core/test_numeric_units.py \
  tests/core/test_numeric_chain_ops.py tests/core/test_numeric_search.py tests/architecture
```

最终共享/精确树数量与耗时见提交正文。见证包含实际无对象编译、缺失锚点/容量/取消零有效区修改、稳定同分、单/双材、温度自适应启停、私有锚点、序号溢出、正式对象构造/扩展失败桩以及精确依赖负例。未运行 40 万次、未试并行、阶段 2 尚未整体完成；下一项 2.2。
