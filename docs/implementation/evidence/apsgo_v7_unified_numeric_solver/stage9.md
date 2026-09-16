# 阶段 9：统一数值实现清理与最终验收

实施前提交 `b197fd3`，环境 macOS 27 arm64、Conda `aps_3.10.18`。本项承接阶段 8 的采用结论：后置精修默认采用同内核原生批量串行，不启用线程并行；规则、算法、九级评分、候选顺序/额度、资源编号、两类拆单和唯一重放均不调整。

## 1. 清理范围与保护

| 原生产模块 | 移走的旧定义 | 保留的实际能力 |
|---|---:|---|
| `_numeric_search.py` | 20 个 | 统一首轮描述、候选计算、消费、正式发布及拆单/重放 |
| `_numeric_refinement.py` | 30 个 | 公共数值描述/原生批量计算、4/64 轮转、原序接受/回收 |
| `_numeric_resources.py` | 12 个 | 公共私有桥接、拆片/分隔材、正式接受资源物化 |
| `_numeric_batch.py` | 4 个 | 有界代次缓冲和同一完整候选入口 |
| `_numeric_kernel.py` | 旧批次包装类型及入口 | 权威规则公式、公共视图完整评价及明细内核 |

这些旧定义从正式生产包删除，放入 `tests/core/numeric_reference_{search,refinement,resources,batch}.py`，继续支撑新旧行为对照；不是可回退的生产求解模式。旧资源的 128 项扩展缓存随参考一起移出生产。独立历史公开数值语义入口、外层规则与审核接口保持，不以清理为由删除公开能力。

清理前后保留的所有函数/类经语法树比较，函数体差异为零；迁出的 66 个定义也保持原函数体，唯一源码结构差异是测试参考 `_prototype_edge_mask()` 的内部相对导入改为正式模块绝对导入。引用关系按生产/测试边界改写，不更改公式。保留新增的非自适应双虚拟材回归，并增加对现用私有桥接的同案例验证。

全流程见证工具的旧挂钩只在冻结旧源码存在时安装；当前挂钩仍为公共消费。旧对象候选采样命令已明确拒绝并指向统一数值工具，不允许零采样假通过；完整返回比较功能不变。资源差分工具当前使用 `--private`，旧模式必须指向冻结旧源码，提前明确报错，不能混合新旧任务对象。

新增保护要求全部迁出定义不得重新出现在生产模块；已有传递调用检查、失败零发布、原序变体/错误/取消保护保留。旧参考不是单独冻结整个旧求解器，完整独立基线仍为阶段 0 导出和诊断产物。

## 2. 必要验证

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -x -p no:cacheprovider tests/architecture tests/api tests/app tests/core
```

共享树累计完成：4171 项通过，2 项旧参考挂钩定位错误；修正后两项定向补验通过 / 24.47 秒。它们不等于同一次累计全绿；精确暂存树还须按同一完整范围重新运行，实际结果记录于本项提交正文。

首次累计运行退出 1：985 项通过后，一处全流程见证仍只统计 `compute_candidate_attempt()`，正式批量路径直接调用同一原生内核，因此计数为零；不是求解或审核失败。测试改挂两种方式共用的 `finish_native_candidate()`，仍要求真实候选执行及独立审核通过，不降低断言、不修改生产算法。修正后按同一累计范围重跑（去掉 `-x`，收齐可能的其余失败）。

第二次累计退出 1 / 402.94 秒，上述全流程见证通过；余下两处分别是参考拆片期锁和拒绝候选零正式物化测试，仍向生产模块安装已迁出的参考函数挂钩，报 `AttributeError`。改为同参考模块中的真实被调用位置，断言不变。补验命令如下；两次初始失败记录保留，不登记为全量通过。

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/core/test_numeric_refinement.py::test_refinement_rejects_split_piece_outside_authorized_target_period tests/core/test_numeric_refinement.py::test_rejected_refinement_overlay_does_not_materialize_formal_plan
```

## 3. 最终完整结果和冷启动

共享累计及定向修正完成后，在全新进程、独立临时 `NUMBA_CACHE_DIR=/tmp/apsgo-unified-final-cold-cache-Y6ajZa` 中执行 400000 次完整请求，不加探针、不删除用户缓存。公共服务计时包含输入到双审核及返回对象；外层进程时间另外包含 Python 启动、导入、请求读取与产物写盘，不含 HTTP。

```sh
/usr/bin/caffeinate -i /usr/bin/time -l /usr/bin/env PYTHONDONTWRITEBYTECODE=1 NUMBA_CACHE_DIR=/tmp/apsgo-unified-final-cold-cache-Y6ajZa /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/profile_numeric_solver.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --candidate-check-limit 400000 --repeat 1 --output-dir diagnostics/unified_numeric_solver/stage9_final_cold_01
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_numeric_kernel_migration.py --reference-run diagnostics/unified_numeric_solver/stage0_baseline_01/run_01 --compare-run diagnostics/unified_numeric_solver/stage9_final_cold_01/run_01 --output-dir diagnostics/unified_numeric_solver/stage9_final_compare_01
```

两条命令退出 0。最终公开返回与阶段 0 完整相同，只排除实测阶段时间；请求和交期报告字节相同。正常完成 400000 次检查、78775 次完整评价、486 次接受后额度结束，`success`、可发布、来源守恒、核心/应用双审核均通过。没有调整质量门槛，也没有用首次全部合规提前终止代替完整预算。

| 最终冷启动指标 | 实测 |
|---|---:|
| 公共服务墙钟 | 331.755426 秒 |
| 公共服务 CPU 时间 | 330.684479 秒 |
| 外层新进程墙钟 | 334.76 秒 |
| 外层用户 / 系统 CPU | 326.13 / 7.48 秒 |
| 最大驻留内存 | 1690304512 字节 |
| 是否达到 60 秒 | 否 |

生产源码摘要 `0eaa1e80e9e7d795f4681eeed44361d04ecde5a01014ac168be44e9a641b018b`；派生请求 `f1a7a8a6f1025f6023ca4f747a8ba8b42462d57bec1a1373fd86b025a9b528e2`，规则 `cdfecaab5382ffbec0d77fe951d19b48d07db4a8c2efe21bbe296963432b6edb`。完整九级结果仍为 `(0, 0, 0, 0, 541.2236111111111111111111111, 1181514.034911111111111111111, 11606, 360, 22)`：零禁止/欠重、22 条链、360 吨虚拟。旧欠清空仍为 6 月 23 日 13:13，本月晚交 24 单/1182.86 吨；本项没有改善或恶化同标准基线的交期。

本项不再跑线程性能组；删除的定义没有生产调用，现用函数体没有变化，因此阶段 8 的同条件热性能组继续有效，不因清理机械重跑两侧三次。最终冷启动数值与该组热运行分开报告。

## 4. 未关闭边界

- 一分钟目标尚未实现；阶段 8 采用组热服务中位数 147.223581 秒。
- 历史旧欠交及本月晚交质量退步仍在独立文档追踪，不因数值化同标准等价而关闭。
- 未进行 Windows 打包、现场性能、HTTP 负载、部署、数据库启用或 C# 适配。
- 服务 YAML、SQLite、输入请求及四份用户本地文档按冻结摘要保护；共享残留仍仅原八项缺失及汇总，退出 1，不记为通过。精确导出独立核验。
