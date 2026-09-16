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

## 8.2.2 整候选有界原生计算

实施前 `64ea669`。普通结构候选统一调用 `native_candidate_complete_step()`：数值描述、切合链/移动交换/换序/回收、桥接修复、私有资源、期锁和完整评价都在原生数组上执行。阶段仍负责原枚举和消费；同一描述的两个修复变体保持依赖，拆单保留准备后扣额再评价的独立边界，底层修复/资源/评价原语不复制。

### 实际实现及保留边界

- 接线原生阶段控制并删除已替代的普通候选 Python 分支、局部资源编辑及切链包装。所有动作继续调用既有权威链/资源/评分函数，不重写公式。
- 一份代次上下文借用只读原链映射、节点/派生列、拆分组、规则及评价复用数据；每个候选工作区独有数值控制、配方、游标和输出缓冲。原生调用期间控制数组拥有有效计数，返回主线程后同步工作区有效长度，不建立第二份正式任务/方案。
- 控制与结果缓冲随工作区复用，扩容重试后重新分配；返回只读有效视图，旧候选在下一次重置时失效。结果缓冲也计入工作区保留字节诊断，不把新计数字段冒充旧输入缓冲数值不变。
- 评价前单独返回取消边界；整数溢出、无排产起点、非法链映射、期锁、容量重试和延迟错误保持。线程依然不能提交资源/状态，本项尚未使用多线程。

### 必要验证及首个差异

初次编译发现 Numba 对命名元组字段 `view` 的名称冲突，改为明确的 `chains` 字段；初步六种合链/反向案例随后通过。扩展回归 107 项通过后，架构检查拒绝裸数字错误码，改为具名数值错误码，未放宽检查；独立 60 项架构检查通过。这些是实现/检查问题，不是算法结果差异。

最终共享必要范围 **205 项通过，220.08 秒，退出 0**。范围为 `tests/core/test_numeric_candidate_kernel.py`、`test_numeric_candidate_publication.py`、`test_numeric_refinement_common.py`、`test_numeric_batch.py`、`test_numeric_state.py`、`test_numeric_view_evaluation.py`，以及 `tests/app/test_unified_numeric_flow.py` 和 `tests/architecture`；使用项目 Conda、`PYTHONDONTWRITEBYTECODE=1`、`pytest -q -x -p no:cacheprovider`。完整原生入口的实际无对象编译签名已验证；把 Python 修复/评价包装换成失败桩后仍能完成双材候选，所有输出字段与权威评价逐项相等，缓冲复用及旧视图失效通过。精确树同范围、目录和耗时见本项 Git 提交正文。

测试成功后在**同一进程**运行以下工具参数，避免重复支付首次编译成本；这是正确性诊断，不是冷启动或正式热性能组：

```sh
tools/verify_unified_numeric_search_prefix.py --source-root /Users/miles/dev/dev-py/APSGOV7 --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --through-split --refinement-checks 20000 --output-dir diagnostics/unified_numeric_solver/stage822_window_01
cmp diagnostics/unified_numeric_solver/stage61_reference_01/search_prefix.json diagnostics/unified_numeric_solver/stage822_window_01/search_prefix.json
```

退出均为 0，JSON 字节一致，SHA-256 仍为 `da2f5a753580bfcab57c258cf31a940e2d11aa7d417e89d0823d662b1d1e5f1d`。154226 次检查/6688 完整评价/460 接受；两类拆单各 1、唯一重放 1、入口/出口身份和窗口九级均不变。运行逐源码摘要在 `identity.json`。其既有 `mode` 名称含 `cold`，**本次实际已由上述测试预热**；附带 31.365581 秒墙钟/31.237831 秒 CPU 只能作为诊断，不与上一单元首次计时作提速比值，不称一分钟全量通过。

保护的用户文件、配置/库、原输入与历史交期问题不变。下一项新 8.2.3，使用同一个原生入口对独立候选进行线程筛查，不能用最后评分阶段的单独并行代替整候选。

## 8.2.3 整候选同内核线程筛查

实施前 `2e8e331`，保持 macOS arm64 / Conda `aps_3.10.18`。默认仍采用逐条协调的公共完整候选入口，新增内部实验选择不属于服务配置，不修改 YAML、SQLite 或公开接口。

### 实现与必要验证

独立描述外层使用实际 `prange` 原生线程循环；每个描述的清理后/原样变体仍按原依赖顺序执行。每步有界续算，线程只能改自己的链、节点、资源事件及输出；主线程在返回后处理取消、扩容、延迟错误和原序结果包装，消费与正式发布不变。串行批量和并行批量共享同一个 `_advance_native_group()`，后者调用既有整候选原生入口，不复制业务公式。

共享基础输入通过只有一项的原生列表借用同一只读数组，候选帧为互不重叠的原生列表；Python 边界保留同帧引用，避免每次检查将整个原生帧重新装箱。该间接传递是实际编译所需：最初直接传递嵌套命名元组时，Numba 并行包装器将其误判为数组参数并报 `scalar type TaskColumns given for non scalar argument`；修正后串行与双线程 4 个小例通过。没有退回 Python 对象计算。

扩充后 12 个新边界案例加现有必要业务共 78 项通过；原组合运行共 123 项通过后，仅架构旧否定断言仍禁止 `_numeric_batch` 使用 Numba，退出 1 / 269.24 秒。同步精确依赖正反断言后，架构全部 97 项通过 / 1.34 秒。没有放开其他文件、Pandas、SciPy 或测试依赖。其间一次重复启动的新例复跑在编译阶段主动停止，避免重复工作，不算通过。精确暂存树重新执行完整同 175 项，结果和目录记入 Git 提交正文。

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -x -p no:cacheprovider tests/core/test_numeric_native_batch.py tests/core/test_numeric_candidate_kernel.py tests/core/test_numeric_batch.py tests/core/test_numeric_refinement_common.py tests/architecture
```

覆盖成功/失败单双材、两个真实修复变体、没有清理变体、私有隔离、容量重试、取消、后续非法描述延迟和第一修复溢出阻断第二修复；实际无对象编译签名与全部数值输出核对通过。

### 真实热点筛查

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_native_candidate_parallel.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --output-dir diagnostics/unified_numeric_solver/stage823_hotspot_01
```

退出 0。正常首轮、拆单及重放后捕获 256 个实际生成描述，横跨 3 个正式代次、33 批；不是从中间快照恢复。动作分别为链内移动 104、节点移动/交换各 40、块移动 33、块交换 32、回收 7；249 描述保留清理/原样变体，7 描述单变体，失败尝试也计入。每次实际完成评价 80 次，其余原样保留准备失败/无变体等结果。缺失的切链/调序等边界由既有小例覆盖，不宣称这 256 项穷尽全流程。

每模式预热一次，5 次热运行按模式轮换顺序；所有有效链映射、节点/派生列、资源事件、状态及完整评分输出摘要一致。计时包含描述校验、工作区准备、修复/资源/评价、输出包装及释放；不包含基础上下文/池创建、摘要核对、正常前缀或最终审核，**不是端到端性能**。

| 模式 | 5 次墙钟中位数（秒） | CPU 中位数（秒） |
|---|---:|---:|
| 公共入口逐条串行 | 0.200562 | 0.200229 |
| 整候选原生批量串行 | 0.146220 | 0.145894 |
| 并行入口 1 线程 | 0.205066 | 0.308417 |
| 2 线程 | 0.255566 | 0.469859 |
| 4 线程 | 0.304550 | 0.780105 |
| 8 线程 | 0.364021 | 1.324022 |

环境实际 12 逻辑核、Numba 最大线程 12；工具完成后恢复线程设置。进程累计峰值 1573650432 字节含编译及全部模式，不当作某模式单独峰值。完整输入、源码、分组和样本见 `samples.json` / `hotspot.json`；生产摘要 `5104209e4580410a9e45fb7e16641cf66ddc2b63208c1c2d44b429926ce75df1`。

本实现的线程协调总成本超过这些有界候选的收益；这是本次样本和实现的结论，不推断所有并行都无用。**不扩大任何线程组到真实窗口或 40 万次。**串行批量组织局部降低约 27.1%，下一项单独验证实际窗口及完整求解；尚不切换默认、不宣称整体提速。

## 8.3 实际窗口与完整采用决策

实施前 `7108523`。同一生产源码摘要 `5104209e4580410a9e45fb7e16641cf66ddc2b63208c1c2d44b429926ce75df1`、同一请求和完整内核，仅替换内部批次协调方式；不更改生成、扣额、消费、接受或最终审核。每方式先运行一次，再按交替顺序各取 3 个热样本。Mac arm64、12 逻辑核、Conda `aps_3.10.18`，没有启动其他重型测试参与热样本竞争。

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_native_candidate_adoption.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --output-dir diagnostics/unified_numeric_solver/stage83_windows_03
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_native_candidate_adoption.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --window-checks 0 --output-dir diagnostics/unified_numeric_solver/stage83_full_01
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_numeric_kernel_migration.py --reference-run diagnostics/unified_numeric_solver/stage0_baseline_01/run_01 --compare-run diagnostics/unified_numeric_solver/stage83_full_01/native_serial_hot_03 --output-dir diagnostics/unified_numeric_solver/stage83_baseline_compare_01
```

三条命令均退出 0；各组所有公开结果（只排除实测阶段耗时）、原订单报告及派生请求一致，核心/应用双审核全部通过。窗口按正常前缀进入，从 134226 到 154226；人工窗口额度在最终审核前恢复为请求中的 400000，不修改审核条件。最后完整结果同时与阶段 0 冻结基线比较通过。

| 范围 / 方式 | 3 次完整公共服务时间（秒） | 服务中位数 | 精修中位数 |
|---|---|---:|---:|
| 2 万窗口 / 逐条协调 | 23.600032、23.072915、23.244311 | 23.244311 | 15.100803 |
| 2 万窗口 / 原生批量串行 | 19.014734、19.668727、18.882363 | 19.014734 | 10.795245 |
| 40 万完整 / 逐条协调 | 202.395192、210.670452、204.762459 | 204.762459 | 196.546317 |
| 40 万完整 / 原生批量串行 | 145.398425、147.662980、147.223581 | 147.223581 | 138.695969 |

实际窗口服务中位数降低 18.2%，完整服务降低 28.1%；各次批量耗时均小于各次逐条耗时。完整 CPU 中位数逐条 203.290090 秒、批量 146.050338 秒；同进程累计峰值 1949089792 字节含两模式编译及运行，不能拆成两种方式独立峰值。本次组不替代 7.2 的旧源码历史实绩，不把不同时间/实现的旧秒数直接计算为本次提升。

完整首次逐条 362.922689 秒、随后首次批量 163.691210 秒均单列，不算热样本；后者已复用前者公共内核编译，**不是批量方式独立冷启动**。第一轮期间作过一次 1 秒进程栈采样，观察到编译栈；另有轻量静态整理，均不计入热样本。最终采用版仍须阶段 9 独立新进程、隔离缓存的完整冷测。

采用结论：后置精修的代次工作区显式选择同内核原生批量串行；公共逐条入口仍用于首轮/拆单及参考对照，内部并行仅保留实验能力，不开放服务配置。正式精修已由新增测试见证实际批量调用，并与逐条路径比较完整状态及逻辑扣额。没有采用线程并行，没有达到一分钟目标；40 万候选、78775 完整评价、486 次接受、22 链、360 吨虚拟、零禁止/欠重、两类拆分各一次及唯一重放保持。历史交期退步仍未关闭。

工具早期 `stage83_windows_01` 没有恢复临时额度，最终应用审核正确报 `runtime_policy_mismatch`，退出 1；`stage83_windows_02` 编译阶段主动停止后修正工具，均不算有效性能样本。原产物保留，审核没有放宽。工具的正常/异常恢复和模式强制覆盖共 4 项测试，以及正式选择与原有批次/取消/资源隔离/架构的必要范围，在共享树与精确树验证后记录于本项提交正文。

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -x -p no:cacheprovider tests/core/test_numeric_native_batch.py tests/core/test_numeric_refinement_common.py tests/core/test_numeric_batch.py tests/app/test_native_candidate_adoption_tool.py tests/architecture
```

原服务 YAML、SQLite、历史诊断、用户四份本地文档均保留。共享残留检查仍仅历史八项缺失及汇总，退出 1；精确导出单独检查，不能将共享检查记为通过。
