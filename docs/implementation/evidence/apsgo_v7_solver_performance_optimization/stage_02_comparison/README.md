# 阶段 2：节点复用的真实对照与性能复查

2026-09-08；本阶段实施前为 `6b1311bc85cfb057779124d933bbad033175b13b`。本机实测及质量回归完成，仅增加专项测量证据，不继续修改生产。最终提交树回归见提交正文；Windows 待验。

## 1. 对照身份与范围

| 项目 | 旧版 | 新版 |
|---|---|---|
| Git 提交 | `33159257e299536270c1bfdf81f225eb23813667` | `6b1311bc85cfb057779124d933bbad033175b13b` |
| 独立源码导出 | `/tmp/apsgo-v7-perf-stage1-baseline.nMXQze` | `/tmp/apsgo-v7-perf-stage2-source.fsFALg` |
| 生产源码集合 SHA-256 | `dc617aa5a5061e8eca9acf80713b7d9b586e93510136c1c8acf70d63d8c14579` | `7cd98513e1f1d0b1ea15b62b59a7fc25ae1644b40ef5576ac712f6d7c511c392` |

已核验生产差异仅 `src/apsgo_scheduler/core/virtual_material.py`。两个导出中的测量工具字节相同，SHA-256 为 `860d831a7123464e1cc21d508bc7a60958202d0a1562c4296eaaedb08b0826f2`；导出无 Git 元数据，提交身份由本表绑定，工具不冒认外层仓库的提交。

输入沿用[阶段 0](../stage_00_baseline/README.md)的 `stage_00_baseline_run_02/prepared_request.json`，SHA-256 为 `29ed173cbfec0d4b6130da87e8513f0a3a1b3b4c9c50f3b501b2551c2026b383`。现场绑定规则版本 6；总时限 900 秒、收尾 30 秒、候选 200000、种子 590531。不是当前 YAML 的 310/10，也不是历史正式基准的 180/10；不重新绑定数据库或调整规则。

原始数据及大型报告全部保存在忽略目录 `diagnostics/performance_optimization/`，不提交客户订单、完整计划或分析器二进制。新结果不覆盖旧归档。

## 2. 必须比较的内容

- 首轮：整个 `first_search`，包含初始/最终方案、全部节点字段与顺序、采纳轨迹、质量、计数、停止原因和边缓存公开统计。
- 各阶段：`first_search_timing.functions` 的函数名称、顺序和全部计数；只排除每项 `wall_seconds/cpu_seconds`。
- 完整求解：整个 `final_search`；整个 `result` 只移除 `run_manifest.stage_duration_seconds` 的数值，阶段键集合仍须相同。发布、资源事实、双审计、拆单计数与全部业务指纹保留比较。
- 当前公开结果的 `code_revision` 为 `unversioned`；实际源码用上述独立身份核验。因此不以 Git 不同为由豁免结果/发布/运行/审计指纹。

工具只替换 `solver.run_local_search`；拆后重放走 `controlled_split` 已导入的原函数。完整模式不会再次截断，也不会将重放冒充首轮；计时包含观察快照的公开调用不称为 HTTP 端到端时间。

## 3. 先行正确性检查

新输出：`stage_02_first_check/`。与最终阶段 0 参考的完整首轮及阶段计数逐字段一致，检查/完整评价/采纳仍为 64226/2263/34，停止为 `local_search_complete`。

首轮经过 66.431507 秒、CPU 66.351351 秒；旧单次参考 102.523736/102.462803 秒。整链 44.735851、真实节点 0.825658、补重 14.089690、调序 6.780184 秒。此单次结果不替代下面配对样本；首轮质量不是最终发布状态。

## 4. 五对无分析器测量

使用本目录 [`run_pairs.py`](run_pairs.py)，固定五对、新旧交替且串行独立进程，不启动其他重测试。以下是实际命令，重跑须换不存在的输出目录，临时源码缺失则从表中提交重新导出：

```sh
/usr/bin/caffeinate -i env PYTHONDONTWRITEBYTECODE=1 \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_solver_performance_optimization/stage_02_comparison/run_pairs.py \
  --old-root /tmp/apsgo-v7-perf-stage1-baseline.nMXQze \
  --new-root /tmp/apsgo-v7-perf-stage2-source.fsFALg \
  --baseline-run /Users/miles/dev/dev-py/APSGOV7/diagnostics/performance_optimization/stage_00_baseline_run_02 \
  --output /Users/miles/dev/dev-py/APSGOV7/diagnostics/performance_optimization/stage_02_pairs_01
```

每次输入/源码/工具身份、完整首轮、日志、UTC 起止时间及退出码均保留。配对脚本只在诊断层调用现有测量入口，不重复实现求解器。

独立审查补正：首次执行的配对脚本只记录源码摘要、未作为失败条件；已在当前脚本补充旧版匹配冻结参考、新旧只差批准的生产文件、每样本匹配所属版本。原执行副本保留为 `stage_02_pairs_01/run_pairs_observed.py`（SHA-256 `2fa4806dcad82fb8ac7a4cd7ef3af449ba1dc130186a47b86774d6d3b9ec6f9c`）。此修正不改变子进程命令或计时区间；本批须另逐样本执行同等源码核验后才能收口，不能只使用原脚本的 `status=pass`。

实际五对全部执行完毕，退出 0；每对完整首轮、各阶段计数及输入都相同：

| 对次 / 执行先后 | 旧经过秒 | 新经过秒 | 旧 CPU 秒 | 新 CPU 秒 |
|---|---:|---:|---:|---:|
| 1 / 旧→新 | 103.869472 | 66.545963 | 103.752757 | 66.489153 |
| 2 / 新→旧 | 103.713202 | 66.526065 | 103.632688 | 66.477002 |
| 3 / 旧→新 | 103.022769 | 66.529175 | 102.954344 | 66.476562 |
| 4 / 新→旧 | 103.723001 | 66.301091 | 103.661367 | 66.253037 |
| 5 / 旧→新 | 103.504390 | 66.427911 | 103.420473 | 66.384639 |
| 中位数 | **103.713202** | **66.526065** | **103.632688** | **66.476562** |

首轮经过时间中位数减少 **35.855741%**。没有删除、替换或补挑样本；这五对不是历史 20 对性能验收，也不是 Windows 或完整 HTTP 时延。

批次完成后已按每样本保留的全部生产文件哈希逐项复核两份冻结导出，10 次全部一致，且均为 `first`、未开启分析器。`source_verification.json` 保存逐样本身份/测量文件哈希及源码清单；`summary.json` 保存全部原始时间、命令及 UTC 起止时间。配对脚本源码保护补正不改变本批计时结论，原副本保留可复查。

批次时窗为 UTC `12:03:47.887790～12:18:32.328047`（本地 `20:03～20:18`）；`pmset -g log` 对该时窗的 `Entering Sleep`、`Wake from`、`DarkWake from` 检索无匹配，退出 1 表示无此事件。临时防空闲休眠随命令退出，不改持久电源设置。

## 5. 函数分析与剩余热点

新版独立运行 `--scope first --profile`，结果在 `stage_02_new_profile/`，退出 0；完整首轮与阶段计数仍等于基线。分析器下首轮 152.682958 秒，明显高于无分析器 66.5 秒，**不能当普通性能样本**。

旧分析沿用设计已经核验的 `ec3e0a7` 分析记录；已用 Git 确认其生产代码与本次旧版 `3315925` 无差异，并核验同一输入、请求、首轮方案与轨迹。原工具、报告和分析二进制独立复制到 `stage_02_old_profile_reference/`；该旧分析不是本阶段重新运行，且旧/新工具版本不同，因此只用于已核验同工作量函数计数及各自热点说明，不用分析器耗时计算加速倍数。

| 函数 / 工作 | 旧调用数 | 新调用数 | 新累计秒 |
|---|---:|---:|---:|
| `bridge`，寻找虚拟桥 | 9283 | 9283 | 59.128520 |
| `materialize`，创建完整虚拟节点 | 1211135 | 455702 | 12.667640 |
| `semantic_fingerprint`，取得边语义身份 | 2779402 | 2779402 | 36.915925 |
| `contracts.fingerprint`，实际编码并计算摘要 | 1219135 | 463448 | 27.941109 |
| `core/evaluation.py::evaluate_plan`，完整方案评价 | 2263 | 2263 | 84.931304 |
| `evaluate_complete_chain`，完整链规则评价 | 53505 | 53505 | 55.624086 |

节点创建减少 **62.373972%**，实际编码减少 **61.985506%**。获取语义身份仍调用同样多次，但复用不可变对象后既有对象级记忆能更多地直接命中；公开边缓存命中/未命中/条目仍完全相同。

新分析中完整方案评价约占首轮 **55.63%**，成为最大单项累计热点；链评价已包含在其中，不能重复相加。桥接、语义摘要、实际编码也有嵌套关系。下一个优化候选应先讨论未变链评价复用，不能据此直接新增缓存、NumPy、Numba、CSR 或多进程；本轮不扩展生产修改。

## 6. 完整求解与正式质量回归

完整运行均使用上述相同的 `profile_solver_search.py`、绑定输入及 `--scope full`，不加分析器或时限覆盖。两份源码依次运行，第二组独立复跑，输出分别为：

| 输出目录后缀（均以 `stage_02_full_` 开头） | 公共调用经过秒 | 公共调用 CPU 秒 |
|---|---:|---:|
| `fixed_work_old` | 203.647481 | 203.475292 |
| `fixed_work_new` | 131.456331 | 131.363734 |
| `fixed_time_old` | 205.291528 | 205.100165 |
| `fixed_time_new` | 131.503411 | 131.356093 |

第一组用于固定工作量等价性；第二组按归档原 900 秒上限独立复跑。**两组均由 200000 次额度截断，未触发墙钟上限**，因此无需临时延长预算。这证明相同实际工作量结果一致，不宣称本机已验证“两个版本在墙钟截断点形成不同结果”的场景，更不称自然收敛或最优。

四次完整结果除时间值以外逐字段完全一致：

- `success`，23 条链，质量 `(0,0,0,0,11726,660,23)`；禁止与欠重均为零，虚拟 660 吨。
- 检查 200000、完整评价 3697、采纳 54；首轮之后新增 20 次采纳。图、路径覆盖、边缓存等所有公开计数也相同。
- 同期间拆单 1 次、未来借入归还拆单 1 次；两次授权与分片资源事实一致，双审计均通过。
- 独立从发布节点按 `source_order_id` 汇总：531 个原订单逐单精确守恒，输入/输出真实重量均为 29333.91 吨。
- 完整稳定数据 SHA-256：`e8dcd14c1844b5dec309476e6e8d2b3c78393d92864f96b5c3b05c325b352e06`。首个差异为无；此摘要包括第 2 节要求的全部稳定内容，阶段时间只保留键。
- 结果指纹 `04f63862d807acdc0f5870100ab16b2293fff5f456a4c72a0cb0299ed242a14b`，发布指纹 `117cd6e162f8c07d58af4fe96ed07e39be140a0a3efff4b820d09a2fa8e351ee`，全程轨迹指纹 `1a15b6e4fd636b85bbc23f1794f9d2049a3d30d5e52497583dc6f31063ecdd22`，四次均相同。

第一组公共调用观察耗时减少 35.449076%，包括拆单、重放、宽差与审计；这是有观察开销的单机两组重复结果，不是 HTTP 端到端或 20 对性能门。阶段时间也说明修改作用于多个桥接调用处：第一组首轮约 102.92→66.37 秒、拆单/重放 35.70→22.92 秒、宽差 61.46→38.34 秒。完整求解与这些子阶段不能重复求和。

`stage_02_complete_verification.json` 保存每次输入/源码身份核验、逐单守恒、结果摘要和原文件哈希。独立第二次审查已对第一组及十个首轮样本复核通过；主执行另对第二组及四次整体做相同完整比较，全部通过。

历史正式 GQGA4 质量门另使用既有 `quality_precheck.py`、固定提交 `6b1311bc85cfb057779124d933bbad033175b13b` 及新输出 `stage_02_formal_gqga4/`，实际退出 0、`passed=true`、失败为空：

- 规则指纹 `d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0`，问题指纹 `cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea`；与现场版本 6 请求明确区分。
- 原 180 秒总时限、10 秒收尾、200000 次额度，不读/改现场 YAML。实际公共服务 120.205827 秒，200000 次检查、3603 次完整评价、58 次采纳、2 次同期间拆单。
- 质量 `(0,0,0,0,10912,600,22)`，零禁止、零欠重、来源覆盖/逐单守恒、拆单授权、无缓存审计及发布契约审计全部通过；虚拟比例约 2.0044%，不设链数上限。
- 此次是单次正式质量回归，**不关闭或重写历史 20 对性能门**。原输入、原门槛、旧输出均未改。

实际运行命令：

```sh
/usr/bin/caffeinate -i env PYTHONDONTWRITEBYTECODE=1 \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py \
  --code-repository /Users/miles/dev/dev-py/APSGOV7 \
  --code-revision 6b1311bc85cfb057779124d933bbad033175b13b \
  --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/performance_optimization/stage_02_formal_gqga4
```

统计与身份摘要见 [`results_summary.json`](results_summary.json)。其中保留十个首轮样本、四次完整运行、两份函数分析、正式质量结果及原文件哈希，不含原始订单或完整方案。原文件保存在上文忽略目录；如需重测，必须另起目录。

## 6.1 提交检查与限制

- 本阶段只改本说明、配对脚本、统计摘要、专项实施计划与 `AGENTS.md`；源码、依赖、规则、正式 YAML 和 SQLite 未变。
- `run_pairs.py` 的稳定阶段比较自检证明只忽略时间、不忽略候选计数；错误旧源码、把同一源码当新旧版本两种负例均在启动测量前拒绝，输出目录不创建（子命令按预期退出 1，整体检查退出 0）。Ruff、JSON 精确回读及 `git diff --check` 均须通过。
- 首次汇总分析器计时遇到精确 JSON 编码器拒绝 `float`，在生成任何摘要文件之前退出 1；已仅将统计计时转换为 `Decimal` 后重新生成成功，不改生产数值或原测量文件。
- 开工 `6b1311b` 干净导出残留检查通过；共享累计及最终精确暂存树同范围验证的实际数量、时间和退出码见本阶段提交正文。
- 本机证明了重复创建减少、完整等价性及实际提速；没有 Windows EXE 运行证据，不据此宣称所有环境性能门通过。下一步按第 7 节补 Windows 实包，不自动进入新算法、评价缓存或依赖实验。

## 7. Windows 阶段仍需实机

macOS 结果不能替代 Windows。现有 EXE 只有 `--config`，没有冻结请求重放命令；不得编造 `--prepared-request` 的 EXE 参数。

Windows 可先用 Conda `aps_3.10.18`，在两个独立干净源码目录执行：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
# 下列路径是测试目录示例；old/new 分别对应本记录中的完整提交。
conda run -n aps_3.10.18 python C:\apsgo-perf\APSGOV7-new\tools\profile_solver_search.py --prepared-request C:\apsgo-perf\frozen\prepared_request.json --output-dir C:\apsgo-perf\results\new-full-01 --scope full
```

旧源码用同一输入、对应旧工具路径和独立旧输出目录；配对计时不加 `--profile`。工具不需要数据库，保留输入中 900/30 策略；先校验输入哈希，返回 0 仍须读业务结果和双审计。

实际 EXE 应在各自干净 Windows x64 工作树运行 `release\build_exe.bat -CheckOnly`、`release\build_exe.bat`，用 `release\verify_release.py package --package-root release\dist\APSGoV7` 核验原始包，保留完整 `release_manifest.json`，再复制到隔离测试目录运行。已有构建自动冒烟是规则 GET/POST/GET 和重启，**不是 531 单求解验收**。不清理旧包、不覆盖现场配置、数据库或服务。

HTTP 完整同输入复现还有一个前提：上传归档没有 SQLite 快照。EXE 接收原始 `request.json` 后绑定测试数据库中的活动规则、原型、字典及 YAML。要复现版本 6，须有身份相符的独立数据库快照；不能换成发布种子库或修改期望版本号后声称同输入。没有此快照时，冻结请求 Python 验证可先做，EXE 同输入验收仍待补证。
