# 阶段 7：统一串行全流程与完整对照

## 7.1 调用边界、独立审核与探针接线

实施前 `6b457b9`，macOS、Conda `aps_3.10.18`。本项没有新增生产算法：前序已接通公共层，本项核验公共服务、独立核心审核和应用审核的完整链路。公共服务小例把旧首轮布局/拆单准备、旧精修准备/对象覆盖/源枚举和旧索引构建替换为失败桩，仍产生公共候选并通过双审计；静态传递调用见证覆盖首轮、拆单及精修生产入口。拒绝不物化、资源/期锁/错误/容量/取消等由前序专项与本次累计共同验证，不把小例称为完整 GQGA4 质量验收。

`verify_unified_numeric_solver.py` 改挂两处公共消费别名；结构描述仅为旧提案摘要映射 7 个元数据，不展开链。正式编辑对象现在只在接受时产生，旧“每个拒绝也生成编辑对象”的物理计数不再适用；新增消费流，额度、接受、阶段及正式结果仍严格核对。不能为探针重建拒绝对象，也不能用物理流改变为由放宽公开业务结果比较。

`verify_numeric_serial_batch.py` 改挂公共消费，不再挂已退出生产的旧覆盖函数。每个实际完整消费候选用相同权威视图独立重算，所有数值结果数组逐项比较；采样为零明确失败。旧平铺批量预热移除，本项仍是有界公共逐条预准备，不声称原生批量/并行已完成。

### 真实单条与 8 条窗口

实际命令（仓库根目录）：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_numeric_serial_batch.py \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage71_batch_window_01 \
  --window-checks 20000
```

退出 0。两侧从正常前缀 134226 次、方案 `fdda717a044c91cb78e88587d1a8339682c6d47d29ea756ac9efcce11ebbc369` 进入，追加 20000 次，实际完整消费各 5560 次、接受各 18 次。逐项汇总、顺序轨迹、资源序号、最终完整公开结果除实测时间外一致，准备请求/交期报告字节相同，双审计通过；退出方案 `6205c1544159c9299dae61c79fbb920ef855c20478fee59e70f6a08c9cb65477`。批次 8 实际协调 1237 次、预计算 5601 次、消费 5560 次、作废 41 次；单条没有协调批次。两侧最大私有内存/冷热及额外重算不同，窗口时间只作诊断，不能作为提速对比。

### 必要累计验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/architecture tests/api tests/app tests/core
```

共享累计 **4078 项通过，215.43 秒，退出 0**。开发时新测试误用不存在的应用审核字段，聚焦检查拦截后改为实际 `audit_report.passed`；没有改生产或断言门槛。精确暂存树用同命令复验，实际树、目录、数量和耗时记入本项提交正文，不在本文件自引用当前提交。

六项保护与原请求摘要保持；共享残留仍为已登记 8 项缺失及汇总，退出 1，不记通过；精确导出残留须退出 0。下一项 7.2 为完整同标准 40 万次结果及独立正式耗时组；历史交期退步仍未关闭。

## 7.2 完整结果对照与串行耗时

### 完整业务对照（已完成，性能组另记）

实施前 `927f650`；生产代码仍为 6.2 接线版本。使用 7.1 已验证精确导出 `/tmp/apsgo-unified-step71-281mPy`，不是共享树中的未提交源码。运行项目解释器、`PYTHONDONTWRITEBYTECODE=1`：

```bash
python tools/verify_unified_numeric_solver.py \
  --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/unified_numeric_solver/stage72_full_trace_01

# 以下完整结果比较在 V7 根目录执行。
python tools/verify_numeric_kernel_migration.py \
  --reference-run diagnostics/unified_numeric_solver/stage0_baseline_01/run_01 \
  --compare-run diagnostics/unified_numeric_solver/stage72_full_trace_01/run_01 \
  --output-dir diagnostics/unified_numeric_solver/stage72_trace_comparison_01
```

两条命令均退出 0，完整公开返回只排除 `run_manifest.stage_duration_seconds` 后完全相同；准备请求和交期报告字节相同，业务首个差异为无。另读取两份 `trace.json`，严格比较 `construction`、`phases` 及 `streams` 的 `quota`、`refinement_proposals`、`accepted`，均完全相同，包含全流摘要及首尾样例：

| 有序业务流 | 数量 | 两侧相同 SHA-256 |
|---|---:|---|
| 扣额申请（含最终拒绝） | 400001 | `8ef1b01c084ad398ba8a176190ec5d8174fa5ba6a9f1acd357771cc9e9d36ccb` |
| 精修消费提案 | 258204 | `efe5973433186f4eb6b42796119ed739c5cbac1fba2d2454b5bd5c4d0dc15483` |
| 正式接受 | 486 | `9993a8a7cc4fb1b4f1a8fa218686ee9f1bd76984e8bb19cf427703014cf5167f` |

最终实际额度 400000、完整评价 78775、接受 486，两类拆分各 1、唯一重放 1；22 链、虚拟 360 吨、零禁止/欠重、双审计通过。九级公开键仍为 `(0,0,0,0,541.2236111111111111111111111,1181514.034911111111111111111,11606,360,22)`。最终数值方案 `8b1e4cc2b3bb7fcddf81433a73649c6d5f33cfc939602b88f475ce5cf1b5ec91`，任务 `e216cd1ae399671c34f7d2ecbb90c1ec6d78ecb0188260fe74df993f9f7fcc29`。

新轨迹 SHA-256 `50e7059c3b591cd37d063534c4e1538490fed8f50b72f7030a1e47c4358e8d30`；整体轨迹文件不要求等于旧文件，因为它还包含已显式迁移的物理探针：正式编辑由 82467 降至 486，仅随接受生成；原接受检查调用 78948 不等同完整评价，新完整评价流精确为 78775；新增公共消费流 273122。后置精修预计算 77751、消费 77647、作废 104，44 次正式物化对应 44 次接受，旧布局调用 0。上述均不改变业务额度和公开计数。

该首次带追踪运行经过/CPU 为 246.9947295409802 / 246.29460400000002 秒、峰值驻留 1826045952 字节。含首次编译与探针，不与旧单次时间直接断言性能；后续无重型探针的首次与三次热运行分组记录。当前完整正确性已通过，性能组尚未完成，不能称阶段 7 全部完成或一分钟达标；历史交期质量问题继续未关闭。

### 无重型探针性能组（已完成）

新侧在上述 7.1 导出、旧侧在 `/tmp/apsgo-unified-stage0-V92qIO` 顺序执行，不与累计测试、剖析或另一侧求解并发。macOS 实查 12 物理/12 逻辑处理器；计算仍为串行，不用核数折算提速。两侧均使用同一 Conda 解释器，命令为 `PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /usr/bin/time -l <解释器> tools/profile_numeric_solver.py`，共同参数：

```text
--prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json
--candidate-check-limit 400000 --repeat 4
```

新输出 `diagnostics/unified_numeric_solver/stage72_native_perf_01`；旧输出 `diagnostics/unified_numeric_solver/stage72_reference_perf_01`。第一轮含本进程首次编译、后三轮为同进程热运行；单轮计时覆盖 `solve_request()` 到双审计与返回构造，不含解释器/导入和诊断写盘。外层 `/usr/bin/time -l` 另记录整组启动到全部报告落盘；不能将单轮首测误称完整冷启动。HTTP 不在范围内，最终无缓存完整冷启动仍归采用后检查。

新侧四轮全部通过严格完整公开结果对照（`stage72_native_perf_comparison_01`），退出 0：

| 新侧样本 | 完整服务经过秒 | 进程 CPU 秒 |
|---|---:|---:|
| 首次，单列 | 242.131447 | 241.241636 |
| 热 1 | 157.752212 | 157.183426 |
| 热 2 | 158.468466 | 157.880985 |
| 热 3 | 158.343551 | 157.775554 |
| 热中位数 | 158.343551 | 157.775554 |

新侧外层整组 720.10 秒、用户 CPU 698.77 秒/系统 CPU 18.66 秒；进程峰值驻留 2667446272 字节，无交换。`time` 另报内存 footprint 5273784808 字节，不与驻留内存混用；四次连续运行的进程高水位也不称某单轮独占内存。

| 旧侧样本 | 完整服务经过秒 | 进程 CPU 秒 |
|---|---:|---:|
| 首次，单列 | 182.349474 | 181.678939 |
| 热 1 | 152.994143 | 152.453300 |
| 热 2 | 153.398841 | 152.848692 |
| 热 3 | 153.324088 | 152.794401 |
| 热中位数 | 153.324088 | 152.794401 |

旧侧外层整组 644.65 秒、用户 CPU 625.93 秒/系统 CPU 16.31 秒，峰值驻留 769359872 字节、无交换。旧侧四轮也均通过同一严格完整比较，证据 `stage72_reference_perf_comparison_01`。两侧生产源码摘要分别为旧 `3141efb32ae916d963953388f4fde943777632cf049b9cef660fc97b54deed23`、新 `aba248b1e2593eccf68e3028fa5abea1b292f2dfedb1d1dc6eaa27c32ad702ae`；测量工具字节相同。完整结果/逻辑计数不变，新热中位数增加 **3.2738%**，没有实现提速，双方都未达到 60 秒；三次样本只作工程采用依据，不声称统计显著或历史性能门禁达标。

### 预热后固定窗口剖析

仅为现有 `verify_unified_numeric_search_prefix.py` 增加 `--profile-refinement`，要求正数精修窗口，不改变生产代码。先调用其 `main()` 完成正常前缀＋2 万次窗口预热，再在同一解释器调用 `main()`、使用相同入口和窗口并传该开关，只剖析精修，不把编译混进热循环。

- 共同参数：`--source-root /tmp/apsgo-unified-step71-281mPy --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --through-split --refinement-checks 20000`。
- 预热/剖析输出：`diagnostics/unified_numeric_solver/stage72_hotspot_warmup_01`、`stage72_hotspot_profile_01`。二者 `search_prefix.json` 均与冻结 `stage61_reference_01` 字节相同，命令及两个 `cmp` 均退出 0。
- 保存 `refinement.pstats`、`refinement_profile.txt`；约 2988 万次函数调用。以下为带剖析器的归因，不是正式经过时间，各累计值互有包含，不能相加：

| 环节 | 调用次数 | 累计秒 | 说明 |
|---|---:|---:|---|
| 候选准备协调 | 39648 | 14.390 | 包含下列准备/评价等子调用 |
| 公共一次准备 | 36747 | 8.827 | 两种变体资格、校验、结构及修复 |
| 拼接修复 | 17691 | 6.056 | 主要成本在桥接及其 Python 调用边界 |
| 私有桥接入口 | 34960 | 5.226 | 其中原生扫描 197617 次、2.209 秒 |
| 完整视图评价 | 5601 | 1.914 | 其中原生完整内核 1.431 秒 |
| 私有工作区分配 | 36747 | 1.516 | 不是只分配一个有界批次后复用 |
| 描述边界校验 | 36747 | 1.232 | 同一代次重复调用数组校验与归属保护 |
| 生成提案的全部协调 | 19583 | 0.387 | 当前不是主要经过时间热点 |

下一批次步骤须从工作区复用、公共不变参数/描述校验边界和修复调用整合入手，不能只并行最后约 1.4 秒的评价内核，也不能取消必要的输入或候选保护。

### 原生生成器提前终止的生命周期问题

本机 NumPy 2.2.6 / Numba 0.65.1 下，在项目实际 `scan.intra()` 上使用 `make_state(5)`、每轮新 `build_scan()`，对 `interval_ranks` 保留弱引用。预热后各执行 1000 次“耗尽”或“取首项后丢弃”，删除生成器并执行 `gc.collect()`；以 `NUMBA_NRT_STATS=1` 和 `numba.core.runtime.rtsys.get_allocation_stats()` 读取原生分配/释放差：

| 处理方式 | 仍存活的扫描数组组 | 净原生分配 | 净原生内存管理块 |
|---|---:|---:|---:|
| 完整耗尽 | 0 | 0 | 0 |
| 首项后丢弃 | 1000 | 15000 | 15000 |

再用独立最小 `@njit` 数组生成器复现：类型为 `_dynfunc._Generator`，没有 `close()`，首项后删除仍保留输入数组。不是把“NumPy 格式必然耗内存”作为解释，也不将整个进程峰值都归因于这一项；首次编译等成本另存。

受控释放机制小例已验证：为原生生成器传入私有布尔终止标记，每个 `yield` 后立即检查；Python 生成器边界的 `finally` 设置标记，仅恢复一次使其直接返回，不计算下一项。1000 次提前关闭后，存活数组/净原生分配/净内存管理块均为 0。**此处仅小例验证，生产六类生成器尚未修复。**计划新增 7.3 先落实全部生成器的确定性释放并复测，之后才进入新 8.1；不以强制遍历剩余空间规避泄漏，不更改候选顺序/额度/接受。

工具新增限制与请求派生必要测试共享 **2 项通过，0.62 秒，退出 0**，实际命令为项目解释器 `-m pytest -q -p no:cacheprovider tests/app/test_unified_numeric_baseline.py::test_refinement_profiler_requires_an_explicit_bounded_window tests/app/test_numeric_solver_measurement.py`。7.1 累计 4078 项的生产代码未改，不再次运行累计回归；本项精确树复验同 2 项，身份与时间记入提交正文。保护文件与历史证据保持，阶段 7 尚待生命周期修复收口。

## 7.3 原生生成器提前终止释放

实施前 `e1af523`。六类生成器通过统一生命周期包装器传入私有终止标记，每个暂停点恢复后先立即退出；没有公开原生 `close()` 依赖，不耗尽余下空间。`intra`、`node_moves`、`node_exchanges`、`blocks`、`chain_candidates`、`reclaim` 的原生枚举和原有轮转保持。异常描述后原本直接退出的分支继续直接退出。`blocks` 的整数区间终点与布尔终止标记使用不同变量。

### 小例与必要回归

- 全部六类及五种整类协调器覆盖完整耗尽、显式关闭、丢弃、取消、异常退出；实际接受后作废与额度结束后所有扫描数组释放。
- 独立原生见证证明提前关闭后不会执行下一段扫描工作；错误描述和深扫续算退出均能释放参数数组。
- 复用上节实际 `scan.intra()` 的 1000 组试验，`NUMBA_NRT_STATS=1`，预热后分别比较 `rtsys.get_allocation_stats()` 的分配减释放；正常耗尽及首项后丢弃的存活数组、净原生分配、净内存管理块均为 **0**。不是只观察 Python 对象数。
- 原专项 96 项通过，80.85 秒；集中必要组 250 项通过，136.00 秒，补充的接受作废/错误续算 2 项通过，76.25 秒。集中组命令如下，导出树合并执行同 252 项，时间及树身份记入本项提交正文。
- 一次命令误填不存在的 `test_unified_numeric_candidate.py`，退出 4、没有执行测试；已更正为真实公共接线与发布测试，不作为测试通过。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/core/test_numeric_refinement_scan.py tests/core/test_numeric_refinement.py tests/core/test_numeric_refinement_common.py tests/core/test_numeric_batch.py tests/core/test_numeric_candidate_publication.py tests/architecture -q
```

### 真实同序窗口

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_search_prefix.py --source-root /Users/miles/dev/dev-py/APSGOV7 --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --through-split --refinement-checks 20000 --output-dir diagnostics/unified_numeric_solver/stage73_window_01
```

退出 0。`search_prefix.json` 与 `stage61_reference_01` 字节相等，SHA-256 `da2f5a753580bfcab57c258cf31a940e2d11aa7d417e89d0823d662b1d1e5f1d`；原前缀 134226 后新增 20000 次检查，5560 次完整评价、18 次接受及全部有序摘要保持。窗口诊断包含首次编译，并与必要回归部分并发，不作为性能样本。

### 完整同标准对照

本项另运行一次 400000 次完整求解，仅验证生命周期修复后的完整结果；不重新执行 7.2 的八次性能组。

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/profile_numeric_solver.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --candidate-check-limit 400000 --repeat 1 --output-dir diagnostics/unified_numeric_solver/stage73_full_01
```

退出 0，`stage73_comparison_01/comparison.json` 完整对照通过；只忽略实测阶段时间，准备请求/交期报告字节相等。400000 次检查、78775 次完整评价、486 次接受、两类拆分各 1 次、22 链/360 吨虚拟、零禁止/欠重及双审计保持。九级公开结果仍为 `(0, 0, 0, 0, 541.2236111111111111111111111, 1181514.034911111111111111111, 11606, 360, 22)`。

本次生产源码摘要 `ca24bc8d6f5a5f62ecf7878f3943a43e106fbe959d044d26c7de91b2df16c56a`。单次首次运行经过 248.236088 秒、CPU 247.561426 秒、进程峰值驻留 1754677248 字节；包含首次编译且开始时与末尾必要测试部分并发，只作正确性运行的附带诊断，不替代 7.2 热中位数、不宣称内存总量或整体耗时已改善。已证明的是上节特定提前弃用泄漏归零。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_numeric_kernel_migration.py --reference-run diagnostics/unified_numeric_solver/stage0_baseline_01/run_01 --compare-run diagnostics/unified_numeric_solver/stage73_full_01/run_01 --output-dir diagnostics/unified_numeric_solver/stage73_comparison_01
```

阶段 7 正确性与问题修复收口，下一项新 8.1 为有界工作区复用、公共批次和重复准备治理，不是并行已实现。正式配置/数据库及四份用户文件摘要保持；共享残留检查仍为已记录的 8 项缺失及汇总、退出 1，未将其记为通过，干净导出另验。历史交期质量问题不随释放修复关闭。
