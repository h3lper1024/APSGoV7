# 统一数值阶段 8：公共批次与并行试点

## 8.1 从描述开始的公共串行批次

实施前 `e8959ee`，macOS Darwin 27.0.0 arm64，Conda `aps_3.10.18`。不应用 Ponytail；不改正式配置/库、算法、九级顺序、输入和资源编号。

### 实际修改

- `_numeric_batch.py` 从 13 列数值描述进入公共候选计算，声明清理后/原样变体顺序；两个变体共用一次描述校验，各自保留独立私有输出。
- 新公共批次工作区限定于一个已接受方案代次；同一代次跨轮转复用，最多保留本批描述数两倍的尝试槽。批次完全消费、取消、异常或接受后先使借用视图失效，再复用数组。没有全局任务池或拒绝候选的正式物化。
- 32 MiB 是多候选的估算包络，包含私有缓冲及评价结果/临时区估算；空间不足时缩小批次，最小为单描述。不用该批量参数截断一个原候选，也不宣称它能约束编译内存或一个候选不可约的内存。必要扩容仍从同一描述重试，不扣额。
- 规则、目标顺序、基础字段和未变链评价复用参数在同代次集中验证；每次候选仍检查视图有效性、链身份/期、原序消费和接受条件。错误归属与捕获移至公共候选层，首轮和精修复用；公共批次不反向依赖阶段控制。扩容也使用同一工作区方法。
- 精修仍按原通道、4/64 轮转、原扣额和首改善执行；工作区复用不变成结果复用或重复候选过滤。旧对象批量打包只剩冻结比较测试所用的不可达旧定义，后续 9.1 清理。
- **本项仍为公共候选入口的有界批量串行调用，不是整个候选已编译为单次原生调用，更没有启用并行。**桥接步骤与完整候选原生调度的进一步整合属于下一项同内核并行准备，不能只并行评价尾端。

### 必要回归

复用/隔离/旧视图失效、代次校验一次、容量缩批、未消费错误延迟、取消及首改善作废通过。既有调度专用测试的简化状态替换为真实小型数值状态；小容量注入改挂当前共用分配函数，不让已退役钩子造成假测试。

初次集中检查的三处失败来自旧测试状态没有新工作区需要的基础任务字段，已修正；第二次新增错误延迟测试误用默认 0 次额度，校正为 10 次后单项通过（36.72 秒）。不是修改业务断言或基线来放行。其余必要集中 216 项通过（该次总计 149.07 秒）；视图复核和修正单项共 9 项通过（74.22 秒）。合计 225 项必要范围，精确导出同范围合并执行，结果记入提交正文。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/core/test_numeric_refinement_common.py tests/core/test_numeric_batch.py tests/core/test_numeric_refinement.py tests/core/test_numeric_candidate_publication.py tests/core/test_numeric_candidate_kernel.py tests/core/test_numeric_kernel_migration.py tests/core/test_numeric_state.py tests/core/test_numeric_view_evaluation.py tests/app/test_unified_numeric_flow.py tests/architecture -q
```

### 真实有序候选对照

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_numeric_serial_batch.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --window-checks 20000 --output-dir diagnostics/unified_numeric_solver/stage81_batch_window_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_numeric_kernel_migration.py --reference-run diagnostics/unified_numeric_solver/stage71_batch_window_01/batch_8 --compare-run diagnostics/unified_numeric_solver/stage81_batch_window_01/batch_8 --output-dir diagnostics/unified_numeric_solver/stage81_reference_comparison_01
```

两命令退出 0。从原首轮、两类拆单及唯一重放正常进入 134226 次位置，再各消费 20000 次；两侧 **5560 个实际完整消费候选的全部数值输出、顺序、接受、资源编号和完整公开结果一致**，双审计通过。与原 7.1 窗口公开结果也一致，仅排除实测阶段时间。消费轨迹文件字节相同，SHA-256 `592ebb620d3f0dd07db5e0b6a18bd045f29fb8332a244998fc79760923685718`。

入口方案 `fdda717a044c91cb78e88587d1a8339682c6d47d29ea756ac9efcce11ebbc369`，出口 `6205c1544159c9299dae61c79fbb920ef855c20478fee59e70f6a08c9cb65477`。18 次接受、22 链/360 吨虚拟、零禁止/欠重；窗口九级 `[0,0,0,0,1951028,426211618665,11611,36000,22]`，只是人工有界窗口，不作完整 40 万次最终排程。

| 物理工作 | 单条 | 8 条批次 |
|---|---:|---:|
| 实际工作区分配次数（含扩容） | 31 | 223 |
| 累计申请的私有缓冲字节 | 352129 | 2533057 |
| 完整预计算 / 实际消费 / 作废 | 按原序即时计算 / 5560 / 0 | 5601 / 5560 / 41 |
| 实际协调批次数 | 0 | 1237 |

相比 7.2 相同真实窗口剖析记录的 36747 次工作区分配，本项 8 条路径降至 223 次；这是工作量观察，**不是端到端耗时比值**。采样工具直接围绕当前分配入口计数，不保留所有工作区而改变其生命周期；最大保留私有输入缓冲 181744 字节，输出/编译内存不混入该字段。单条 32.639248 秒、8 条 16.252195 秒均包含逐项重算和诊断，且前者先运行有编译成本；不得据此宣称两倍提速。

运行生产源码摘要 `1ef04c60eed49df1810569484843b5aab5eefb1e54064183ca609455818a0f2b`。之后仅清理无人调用的旧工作区分配包装及无用导入、补类说明，生产执行函数不再变化；最终精确树同范围验证见提交正文。下一项新 8.2 仍需将整个候选的链编辑/桥接/资源/评价接入同一原生计算流程后再实际测试并行。

## 8.2.1 共用有界原生拼接修复

实施前 `57a4543`，环境和保护范围沿用本计划；非旧 NumPy 计划的同号步骤。本单元只是整候选原生计算的前置单元，没有开启并行、不代表新 8.2 全部完成。

### 实际修改与边界

- `repair_parts_step()` 直接读取原链数组片段、依原顺序拼接和修复，使用数值进度/桥接游标续算；每次至多处理一个拼接片段或 64 项桥接搜索。主线程在步骤间保留取消检查，不用不可取消的全原型双层循环替换。
- 原生桥接扫描完成后直接调用唯一选中资源写入路径；原独立桥接包装也调用同一路径。直接、单材、双材优先序、严格同分选择、温区投影、资源全字段和扩展事件都保留，不物化正式任务。
- 共用 `_repair_parts()` 接线后，首轮整链、真实节点移动、拆单移除父单后的拼接和精修结构调整均走该原语；并非只为后置精修复制一套桥接逻辑。
- 保留容量不足原尝试重试、部分私有缓冲不发布、资源序号 int64 溢出的原错误边界。独立直接写入还在读取非法端点状态时明确退出，避免继续使用失败投影；有效输入的写入公式不变。

### 必要回归

首批旧有 48 项通过（90.98 秒），随后新增小步续算、全字段/事件及原错误定位见证，集中必要范围 **197 项通过，134.17 秒，退出 0**：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/core/test_numeric_candidate_kernel.py tests/core/test_numeric_private_resources.py tests/core/test_numeric_private_split.py tests/core/test_numeric_candidate_publication.py tests/core/test_numeric_refinement_common.py tests/core/test_numeric_batch.py tests/app/test_unified_numeric_flow.py tests/architecture
```

同一真实双材案例用 1/2/64 项扫描片验证原生续算，选中序号、全部节点/派生列和扩展事件相等；真实 Numba 无对象编译签名已见证。既有非自适应温区、资源容量、取消、拆单、原序消费和实际主流程禁止旧准备入口的检查一起通过。精确暂存树导出复验同一范围，树、目录、耗时见本项 Git 提交正文。

本单元开始及提交前残留检查仍只报告已登记的 8 个旧稳定路径缺失及 1 个归类汇总，不修复外部历史残留、不宣称共享检查成功；精确导出另验。四份用户文件、正式 YAML/SQLite 和原请求 SHA-256 均与阶段 0 保护值一致。

### 真实前缀与窗口

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_search_prefix.py --source-root /Users/miles/dev/dev-py/APSGOV7 --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --through-split --refinement-checks 20000 --output-dir diagnostics/unified_numeric_solver/stage821_window_01
cmp diagnostics/unified_numeric_solver/stage61_reference_01/search_prefix.json diagnostics/unified_numeric_solver/stage821_window_01/search_prefix.json
```

退出均为 0。前缀及窗口 JSON 字节一致，SHA-256 `da2f5a753580bfcab57c258cf31a940e2d11aa7d417e89d0823d662b1d1e5f1d`；共 154226 检查/6688 完整评价/460 接受，窗口新增 5560 完整评价/18 接受，出口身份和九级与上节窗口一致。两类拆单各 1、唯一重放 1；没有把窗口结果冒充全量最终排程。

本次为首次编译并带轨迹的正确性诊断，开始阶段与必要测试有短时重叠；附带 115.381378 秒墙钟/114.878869 秒 CPU 不作正式性能比较。源码逐文件摘要在该目录 `identity.json`；无扩大忽略项、无改原期望。下一项整候选原生计算仍待实施，之后才进入实际线程筛查。
