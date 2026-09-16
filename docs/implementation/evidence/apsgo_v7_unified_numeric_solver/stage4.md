# 阶段 4：有序连接图、路径覆盖和初始链

## 4.1 有序连接图与路径覆盖

实施前 `1463fd8`，冻结生产对照仍为阶段 0 的 `/tmp/apsgo-unified-stage0-V92qIO`，没有切换或覆盖共享工作区。

### 实际改动

- 构图直接写有容量和有效长度的一维邻居数组及偏移，不先积累 Python 邻接列表再拼接。容量增长重算未写入的同一条边，逻辑边计数不重复。
- `random.Random(seed)` 直接打散数值数组，源顺序、每条已完成邻接段的打散及随机调用数量保持；编译稳定排序保留原主键、后继键及同分随机顺序。排序比较直接比较物理值，距离排序使用无符号临时差值避免两端 signed-int64 差值溢出，不改变权威物理列或评分。
- 使用共享权威边原语；保留先判断允许边，再执行原方向过滤的次序及三项图计数。深层边扫描每段最多 256 次，段间由原预算检查取消/时限；不扣求解候选额度。
- 原广度优先分层及深度优先增广次序不变，队列、节点栈、邻接游标、入栈边和访问标记均为数值数组；每段最多 256 次遍历步骤，保持续算位置。最终按原起点次序输出路径数组，不建 Python 路径列表或访问集合。
- 部分构图/遍历中断只返回未签发的部分结果，不能作为完整初始解；初始链构造仍沿旧实现，本项未做 4.2。

### 实际对照

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_construction.py \
  --source-root /tmp/apsgo-unified-stage0-V92qIO \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage41_reference_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_construction.py \
  --source-root /Users/miles/dev/dev-py/APSGOV7 \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage41_native_01

cmp diagnostics/unified_numeric_solver/stage41_reference_01/construction.json \
    diagnostics/unified_numeric_solver/stage41_native_01/construction.json
```

均退出 0，完整图/匹配/路径及任务/规则/目标/构造身份逐字节相同，JSON SHA-256 均为 `6f5780806a090724f79c81cd5e9131beee90854a70ec5b0ebf46b33233d320ce`。真实 531 单检查 140715 条边、允许 32536 条、输出 17 路径；构造候选计数为 0。此处只到路径覆盖，没有进入初始方案、局部搜索或最终审核。

冻结/新实现冷诊断经过时间为 4.354742 / 4.527393 秒，包含本进程编译；CPU 时间 4.343797 / 4.518079 秒。它们是并行启动的两个独立诊断，不作为正式提速或性能样本，未执行完整性能组。

### 必要验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_native_construction.py tests/core/test_numeric_construction.py \
  tests/core/test_numeric_search.py tests/core/test_numeric_refinement.py tests/architecture
```

共享 132 项通过、53.62 秒、退出 0；精确暂存树同范围结果及实际提交见 Git 正文。先期测试把零节点图作为完整合法覆盖，既有覆盖契约要求非空，已收紧小例枚举为 1～4 个节点，不修改生产契约。全部小型有向无环图按原队列/迭代器栈对照，稳定键和随机同分序、容量重试计数、实际无对象编译及中途取消均通过；新模块依赖仅精确登记现有构造模块的 Numba。

下一项 4.2 初始链构造。全流程 40 万次、正式速度/并行、历史交期退步均未在本项验收；正式配置/SQLite、四份用户文件和旧证据不变。

## 4.2 初始链数值构造

实施前 `74e2d12`。原追加后禁止轮廓不恶化、原链重上限、原单原子超重错误和原期内稳定分组保持；没有新增约束。路径有效边检查、逐节点追加/截链和链期计算已编译，公共 `bind_base_chain()` 借用路径切片，`reorder_chains()` 只换元数据，最终 `flatten_view()` 才一次平铺正式方案。每次扫描最多 256 单后检查取消，不暴露部分初始方案；不扣候选额度。链规则仍使用唯一 `evaluate_chain_kernel()`，不绕回 Python 行转换包装。

真实运行沿用上节完整命令，将两个输出目录分别改为 `diagnostics/unified_numeric_solver/stage42_reference_01`、`stage42_native_01` 并增加 `--through-initial`；两个命令均退出 0。`cmp` 比较 `construction.json` 退出 0，SHA-256 均为 `c844301c51b2bbdaee64a8dd9d9a65d064227142b9b39825209b02a94b252deb`。原 531 单、17 路径、30 初始链，全部数组/顺序/身份和评分一致、候选额度 0。仅运行到初始构造，未进入局部搜索或最终双审计。

冻结/新冷诊断经过时间为 24.880539 / 32.292729 秒，CPU 为 24.809566 / 32.205588 秒，包含编译且与专项测试并发，**不是性能样本，不据此宣称提速或退化**。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_native_construction.py tests/core/test_numeric_construction.py \
  tests/core/test_numeric_chain_ops.py tests/core/test_numeric_search.py \
  tests/core/test_numeric_refinement.py tests/architecture
```

共享必要 151 项通过、64.48 秒、退出 0；精确暂存树同范围结果及提交见 Git 正文。包含 8 组构造对照、步长 1/2/256 续算、取消/超界/原子超重、公共切片和未变数据保护。先期新增小例误把显示吨数当整数内部重量，改为读取权威重量刻度；架构保护对既有错误位置 `status[3]` 仅在两个构造函数精准登记，不放开其他基准数常量。上述差异均已复验关闭，未改变业务规则或算法政策。

阶段 4 完成，下一项 5.1 首轮四类局部搜索；后续搜索、拆分、精修尚未接线，完整性能/并行/独立交期问题保持未验收。
