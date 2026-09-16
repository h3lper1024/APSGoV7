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

## 2.2 私有拆片、分隔材和资源事件

实施前 `6f64c98`；2.1 双树各 196 项已通过并提交。本项仍是公共资源原语，不替换主求解拆单流程。

- 模板全字段及四组规则派生列由同一个编译写入原语复制；虚拟材和拆片只覆盖实际改变的字段。
- 拆片按原最大片重顺序分配，末片低于下限或分隔数量超限不生成资源；工时按累计片重比例逐段四舍五入，再相减得到各片整数毫秒。原片重/总工时、父单/资源/来源期、目标期、片序和拆分序号保持。
- 极端输入的中间 `工时 × 累计片重` 可超 int64，而最终商仍合法；本实现用整数商余分解和有界二进位除法，不能错误拒绝原 Python 宽整数允许的结果，不使用浮点或调整精度。305 组边界/固定种子大整数比对及极大值完整片段分配通过。
- 分隔材按原型原序，以原权威链评价的禁止数、严重度、平滑度逐级择优；每次仅投影左片/原型/右片三行，不复制任务全部列，不创建临时节点对象和指纹。原规则公式不另写一份。
- 私有拆分组与扩展事件同时记录；容量不足/取消不推进有效长度，正式编号、任务身份和接受仍留给阶段 3。授权判断仍由原拆单规则及阶段控制负责，本原语不新增动作/规则或直接接受候选。

### 真实拆片对照

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_resources.py \
  --source-root /tmp/apsgo-unified-stage0-V92qIO --splits \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage22_reference_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_resources.py \
  --source-root /Users/miles/dev/dev-py/APSGOV7 --private --splits \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage22_private_01

cmp diagnostics/unified_numeric_solver/stage22_reference_01/splits.json \
    diagnostics/unified_numeric_solver/stage22_private_01/splits.json
```

均退出 0。按每原单从最早期到其来源期检查原资格，实际得到同期间 2、未来归还 3 共 5 个资源案例；全部拆片/分隔材/派生列/目标期/扩展事件字节一致，样本 SHA-256 为 `d5a7d9c0658460829cda4f4d156a75553c1aa18c53ed09fae88932f93f20b196`。此检查不执行搜索/接受，不能称完整求解有 5 次拆单。

### 集中验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_private_split.py tests/core/test_numeric_private_resources.py \
  tests/core/test_numeric_bridge_nonadaptive.py tests/core/test_numeric_rules.py \
  tests/core/test_numeric_kernel_migration.py tests/core/test_numeric_state.py \
  tests/core/test_numeric_workspace.py tests/core/test_numeric_units.py tests/core/test_numeric_chain_ops.py \
  tests/core/test_numeric_search.py tests/architecture
```

实际共享/精确树数量、耗时、退出码见提交正文。首次三片测试夹具使用 1200 吨原单但沿用了测试侧 1000 吨链上限，在输入标准化被正确拒绝；已将该夹具的链上限设为 2000 吨，生产配置和输入校验未改。专项两资源文件 18 项通过、22.88 秒；覆盖同期间/未来归还、两/三片、私有锚点、容量/取消/重复父单拒绝、工时守恒及正式对象/扩展失败桩。

集中检查发现旧架构门把分隔材评价的三行数组长度当成禁止基线数字，203 项通过、1 项失败；容量前置收紧后的中间复测同样仅此失败，未记为通过。现只精确登记 `scan_private_separator()` 内三行投影的数组形状，温度列使用已有字段常量，增加负例继续拒绝其他函数/其他分配/`N=3`。未全模块放宽基线数字检查。拆片容量检查前不分配片段数组，失败桩核验该边界。

**阶段 2 完成；下一项 3.1 公共视图与权威评价内核。**尚未切换主求解、未跑新的完整排程/性能或并行。六项保护摘要和旧残留状态保持，历史交期质量问题未关闭。
