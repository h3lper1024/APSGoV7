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
