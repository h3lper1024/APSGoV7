# 阶段 0：全流程基线与覆盖清单

## 1. 范围与身份

- 实施前 `2539ab0`；分支 `codex/delivery-objective-optimization`；macOS Darwin 27.0.0 arm64、Conda `aps_3.10.18`。生产源码未修改。
- `git diff 64eb929 39a50f8 -- src` 及 `git diff 39a50f8 2539ab0 -- src` 均为空；同标准基线没有偷换代码。
- 实际基线导出树 `28eb64a0e852923239abc29fc5400eb35877eba1`，目录 `/tmp/apsgo-unified-stage0-V92qIO`。仅额外加入诊断工具和其测试；没有带入四份用户文件。导出后测试补强不影响实际运行工具与生产源码，最终提交树另行复验。
- 原请求 SHA-256：`b94d979b3d5d6545a0b7bac9a01b49b89f4feda0582914a4ff07d4d35088ae6b`。仍为 531 单、27 原型、种子 590531、400000 次、9999 秒搜索＋10 秒收尾。
- 派生数值请求指纹 `f1a7a8a6f1025f6023ca4f747a8ba8b42462d57bec1a1373fd86b025a9b528e2`；规则指纹 `cdfecaab5382ffbec0d77fe951d19b48d07db4a8c2efe21bbe296963432b6edb`。仅按既有 `derive_numeric_request()` 改数值标准与评分投影声明；规则参数、订单和时间输入不变。
- 实际原始产物：[基线目录](../../../../diagnostics/unified_numeric_solver/stage0_baseline_01/)。`input_identity.json` 包含逐生产文件摘要、完整派生请求、工具摘要及环境；`run_01` 保留请求、完整返回、交期报告和测量；`trace.json` 保留阶段与有序摘要。

| 产物 | SHA-256 |
|---|---|
| 派生请求字节 | `a61d2a18eb0723aa77ba9cacc4064eed7af281f388d679b67b30b074d8e6da55` |
| 全流程轨迹 | `9f0796578b4bbb3b9c8c3c9b081aaf209a5295b7b0914c47dde1c16a6cc1c232` |
| 完整返回 | `483409e1905ec92d585c90766e9b0cf82d2ea31997abd37982ddb0639b628182` |
| 交期报告 | `7e2feba832de905b58318f40782cc39449f0758ef3d3b21ce5a2067cb4e152d4` |

## 2. 实际完整结果

| 项目 | 本次基线 |
|---|---|
| 连接图 / 路径 / 初始链 | 检查 140715 边，允许 32536 边；匹配 514 边、17 路径、30 初始链 |
| 首轮结束 | 110964 次候选，848 次完整评价，361 次接受 |
| 拆单及唯一重放结束 | 134226 次候选，1128 次完整评价，442 次接受；拆单两种各 1 次、重放 1 次 |
| 最终 | 400000 次候选，78775 次完整评价，486 次接受；22 链、360 吨虚拟材 |
| 九级目标 | `(0, 0, 0, 0, 541.2236111111111111111111111, 1181514.034911111111111111111, 11606, 360, 22)`，公开单位与原实现相同 |
| 停止 / 审核 | 候选额度用完；核心、应用审核均通过，公开结果可发布；不证明全局最优 |
| 诊断经过时间 / CPU | 189.3497125 / 188.410072 秒；含本工具观测成本，不作正式性能样本 |
| 进程峰值驻留内存 | 747782144 字节，含编译和探针 |
| 已知检查点对照 | 与原阶段 7.4 `stage7_native_400k_01/run_02` 完整公开结果除实测时间外一致；请求和交期报告字节一致 |

旧欠清空仍为 6 月 23 日 13:13、本月晚交仍为 24 单/1182.86 吨；独立历史交期退步问题没有解决。本阶段确认的是同标准当前实现可重放，不是历史质量恢复或一分钟达标。

## 3. 全流程调用、行为和退出清单

下列路径均位于 `src/apsgo_scheduler/core/`；“退出”指后续替代的内部准备路径，不是删除整个阶段。

| 环节 | 当前实际入口 / 公共操作 | 当前检查、扣额和次序 | 资源、随机性及后续退出点 |
|---|---|---|---|
| 连接图 | `_numeric_construction.build_numeric_construction_graph` → `numeric_edge_allowed` | 原排序、方向过滤、有序邻接；不扣候选额度，边检查另计 | 唯一随机源 `random.Random(seed)` 原打散；4.1 替代 Python 邻接列表，不换随机源 |
| 路径覆盖 | `numeric_minimum_path_cover` | 原广度分层/深度增广；不扣候选额度 | 无动态资源、无随机；4.1 替代队列、迭代器栈，不换匹配算法 |
| 初始链 | `construct_numeric_initial_plan` → 链禁止轮廓/完整评价 | 保持最大链重与禁止轮廓不恶化的原截链政策，不要求初始零欠重；不扣候选额度 | 无动态资源；4.2 替代逐链 Python 追加，不增新约束 |
| 首轮整链 | `_numeric_search.improve_numeric_whole_chain` | 欠重优先供体、原反转/位置顺序，每位置先扣额；直接/单材/双材，完整评价严格首改善 | 正式序号仅接受推进；5.1 替代 `_layout`、`_resource_whole_chain_candidate` 的全方案准备 |
| 首轮真实节点 | `improve_numeric_real_node_relocation` | 原供体/欠重目标/节点/位置；供体容量预筛，位置扣额后测边 | 无新增资源；5.1 替代 `_layout` 和 `apply_numeric_candidate` 全方案物化 |
| 首轮虚拟填充 | `improve_numeric_virtual_weight_fill` | 原目标/位置/原型序；每原型先扣额，再检查边、容量、完整评分 | 私有虚拟材，接受推进序号；5.1 退出拒绝路径完整任务扩展 |
| 首轮整链调序 | `improve_numeric_chain_order` | 原交期次序、同期间位置、预览与完整核验；保留原扣额点 | 无资源，无随机；5.1 共用链映射及候选入口 |
| 两类拆单 | `improve_numeric_controlled_split` → `evaluate_numeric_split`、`_prepare_numeric_split` | 按链/父单序和原资格判定；完整拆分准备成功后扣额，期锁检查，原虚拟比例前置拒绝；不能改全禁止提前拦截 | 同期拆分/未来借归均保留；私有片段、分隔材及分步扩展；5.2 退出业务对象和整任务扩展 |
| 唯一重放 | 拆单接受后 `_run_numeric_local_search` | 有拆单且允许继续时只重放一次上述四类；与前缀共用同一预算，不清零 | 5.2 使用同一新首轮入口，不能新增轮次 |
| 后置精修 | `_numeric_refinement.improve_numeric_refinement` → `_scan_family`、`_try_recipe` | 结构归属直接生成；每原单 4、重点/常规 64；首变体和条件触发的第二变体分别原位扣额；全规则禁止检查、首改善 | 无新增随机；6.1～6.2 退出 `PreparedRefinementCandidate`、`_prepare_repaired_parts`，保留正式原子发布 |
| 连接材回收 | 精修常规回收族 → `_prepare_reclaim` | 实际消费 259 条回收描述；保护分隔材、重连/期锁和全评价政策保持。本次无回收接受，不伪称有真实接受见证 | 小例负责有接受见证；6.2 共用链/修复/资源，不另写评分 |
| 发布 / 审核 | `NumericSearchState.commit`；`_numeric_audit` 与应用审核 | 原代次/严格改善/完整身份；接受后正式资源；最终从原输入和接受事实重建，不信搜索缓存 | 3.3/7.1 适配后仍同一提交者与独立审核；不改公开对象 |

首轮现有 append/prepend 与端点 insertion 的枚举及扣额原样冻结；本轮不能顺手删除这些历史枚举而改变逻辑额度。“结构唯一归属”延续后置精修已有拥有者机制，不借数据结构改造重写候选集合。

## 4. 轨迹边界与小样例

新增 `tools/verify_unified_numeric_solver.py` 只在诊断运行装配可恢复的观测包装，不进入生产依赖。每条摘要流覆盖全部记录但仅保存前后各 8 条样本，整体轨迹约 166025 字节；不保存 40 万份候选方案。

| 摘要流 | 记录数 | 含义 |
|---|---:|---|
| `quota` | 400001 | 全部正额度申请，含最后 1 次额度不足；实际消费仍 400000，不与申请数混同 |
| `edits` | 82467 | 通过前置准备后生成的完整编辑描述；不能称全部额度都形成了编辑对象 |
| `refinement_proposals` | 258204 | 实际原序消费的精修结构描述，包含修复失败者；第二变体另有额度记录 |
| `evaluated_attempts` | 78948 | 进入候选接受检查的调用，包括期锁拒绝；不等于 78775 次完整评价 |
| `accepted` | 486 | 全部接受的动作、影响范围、质量、任务/方案指纹和资源序号 |

图邻接、覆盖路径一次性保留数组内容；阶段前后记录质量、身份和计数。回收等物理预计算统计单列 `refinement_diagnostics`，其中 103 个已评价后缀被丢弃，不计逻辑额度。探针不主动调用预算检查，不改变取消或随机调用。

复用 `test_numeric_search` 的直接/单材/拒绝/填充、两类拆片及唯一重放，`test_numeric_state` 的守恒和空表面，`test_numeric_rules` 的真实/虚拟边与规则，`test_numeric_batch` 的容量/取消/第二变体/接受后错误作废，`test_numeric_kernel_migration` 的跨多个虚拟端点和原生规则差分。新工具测试另补有序摘要有界性、安装恢复、观测前后结果一致及强制双材桥接。上述小例与真实全流程分开记录，不能用有界窗口替代完整验收。

## 5. 实际命令与验证

仓库根为 `/Users/miles/dev/dev-py/APSGOV7`；Python 为 `/Users/miles/anaconda3/envs/aps_3.10.18/bin/python`。完整运行命令在上述干净导出根执行：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_solver.py \
  --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/unified_numeric_solver/stage0_baseline_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_numeric_kernel_migration.py \
  --reference-run diagnostics/numpy_numeric_search_and_calculation/stage7_native_400k_01/run_02 \
  --compare-run diagnostics/unified_numeric_solver/stage0_baseline_01/run_01 \
  --output-dir diagnostics/unified_numeric_solver/stage0_checkpoint_comparison_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/app/test_unified_numeric_baseline.py tests/app/test_numeric_solver_measurement.py \
  tests/core/test_numeric_search.py tests/core/test_numeric_batch.py tests/core/test_numeric_state.py \
  tests/core/test_numeric_rules.py tests/core/test_numeric_kernel_migration.py tests/architecture
```

完整基线与公开结果比较均退出 0、首个业务差异为无。共享及最终精确树的必要测试实际数量、耗时和提交记录见本项提交正文。准备期间的小例失败已定位为测试夹具：默认 12 秒时限被首次编译耗尽、漏带交期计时、两端本可直连，分别补同条件宽松时限、复用计时夹具、构造不相交温区；生产规则及断言标准未放宽。

`measurement.json` 覆盖 `solve_request()` 输入准备至双审计和返回对象；`timing_boundary.json` 另含读取/派生、交期报告、诊断写盘。当前不含解释器启动、模块导入及 HTTP；首次完整冷启动和无探针热性能由阶段 7/最终采用验证补齐，本次不据此认定提速。

工作区残留检查仍为已登记 8 个缺失路径及汇总，退出 1，不记通过；没有新残留差异。七项保护摘要保持计划值，生产源码无差异。下一项为 **1.1 共用描述、方案视图和私有工作区**；本证据不表示新公共计算层已实现。
