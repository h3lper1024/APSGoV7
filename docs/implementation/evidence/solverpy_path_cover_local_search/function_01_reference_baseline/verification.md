# 功能 1：冻结参考输入、输出与阶段对照基线

## 当前状态

数据与工具验证已完成，质量与性能门槛已由用户确认并冻结，随本项提交收口。五份中立输入、两种 Python 环境的参考复跑、V3 全量清单、阶段轨迹与两组各 20 次性能样本均已核验。下一项为功能 2“建立核心值类型、不可变领域模型与诊断契约”，本轮未实施。

- 实施前提交：`5f9008a`（建立新求解器自包含工程与架构门禁）。
- 分支：`codex/solverpy-path-cover-clean`；平台：macOS 27.0 arm64，zsh。
- Python：正式参考使用 Conda `apsgo_v6_3.10.18` / 3.10.18；历史复跑使用 `/usr/bin/python3` / 3.9.6。
- 当前仅新增基线数据、开发采集工具与测试，没有增加任何求解器业务代码。

## 输入与来源

五份输入的原始字节总计 9,104,637 字节。CSV 来自实施计划第 2.4 节的明确路径，四份 JSON 分别从该节的四个精确 Git 历史对象导出到新临时目录，核对哈希后才写入新基线目录，写入后再次核对。没有恢复任何旧源代码目录。

所有相对路径、原始 SHA-256、原始来源与字节数记录在 `tests/baselines/gqga4/reference_manifest.json`。脚本及四份现有静态产物也由该清单固定，外部脚本与输出均未修改。

首次暂存树导出发现本机 `core.autocrlf=input` 会把输入 CSV 的 CRLF 转为 LF，导致导出字节哈希变化：原始为 `d178cc19ac6ef2807962beb110333f8702c8e2eeec153f9b0f94236913060941`，错误暂存值为 `69c490c6671e9f2d782b9157948d0455ee08f64504e868bece48b5a2cccf37e7`。
新增 `.gitattributes` 仅对本基线目录禁用文本转换，再显式重新暂存输入；不重写 CSV。
该补充属于功能 1 必需的字节身份保护，已在实施计划白名单中说明；首次错误导出不作为通过证据。
同时仅对该 CSV 声明 `cr-at-eol`，保留通常的空白检查并将 CR 识别为行结束符。重新暂存后，工作树、索引与干净导出的五份输入哈希全部一致。

## 参考复跑与阶段证据

| 运行 | 候选检查数 | 停止原因 | 内部耗时 | 结果 |
|---|---:|---|---:|---|
| Python 3.9.6 历史复跑 | 94024 | search_complete | 28.188854 秒 | 三份结果产物逐字节相同；运行清单仅计时与输入路径不同 |
| Python 3.10.18 正式环境复跑 | 94024 | search_complete | 22.822220 秒 | 三份结果产物逐字节相同；运行清单仅计时、输入路径、Python 版本不同 |
| Python 3.10.18 阶段采集 | 94024 | search_complete | 28.758919 秒 | 17 条路径、31 条初始链、40 次搜索改善、1 次拆单、9 次后续改善，最终语义与静态产物相同 |

两次普通复跑的完整命令、环境、差异字段与结果文件哈希见 `reference_replays.json`；阶段采集产物对照见 `stage_artifact_comparison.json`。

阶段采集通过临时包装参考函数、读取返回时的图数据与接受动作时的局部变量完成，不改写参考脚本、不重写其算法。仅采集运行把搜索安全时间上限显式改为 600 秒，以排除记录开销造成的提前停止；候选预算仍为 100000。这一覆盖被单独记录，不能冒充 30 秒性能结果。正式性能采样运行未加这些包装的原始脚本，使用原始 30 秒搜索安全上限。

阶段轨迹原始金样：`tests/baselines/gqga4/reference_stage_expectations.json`，SHA-256 为 `90abd195942759fba3dc946a6d4a989e5ab0e5503667b0d0c28569efb539cd45`。

| 候选计数类型 | 实际数量 |
|---|---:|
| 整链结构调整，两处计数点合计 | 4806 |
| 单订单移动 | 77607 |
| 虚拟过渡材料填充 | 11610 |
| 受控拆单 | 1 |
| 合计 | 94024 |

参考最终质量为 `[1, 70.3, 0, 0.0, 22, 540.0, 22694.88]`，仍是 `BEST_EFFORT`：22 条链、0 条欠重链、1 个禁止违规。此事实不是产品正确性门禁通过，也不能放宽目标的“其他禁止违规为零”。

## V3 结果边界

`v3_reference_manifest.json` 保存指定目录全部 170 个相对路径与内容哈希，聚合 SHA-256 与计划一致：`1a28933a36d019380f17116dbf8411b5163bd30910bf633bec8496c8f8c13897`。

由最终指标、616 行排程明细和逐链规则报告交叉验证：37 条链、3 条欠重链、29333.91 吨真实材料、1080 吨虚拟材料。`rolling_final_repair/final_result.csv` 与同目录 `output_chains.csv` 字节相同。

其中 570.3 吨链 `BR_00000001|flexible|stage1|chain_0034` 同时违反窄 IF 钢连续重量与链重下限，另两条 24 吨、288 吨链只欠重。因此 V3 也不能被描述成“仅存在欠重违规”。通用发布契约仍只允许欠重；本次 GQGA4 已确认的基准更严格，连欠重也必须为零。

## 性能采样与验证入口

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/capture_solverpy_reference.py --verify-only
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/compare_solver_stages.py --reference-only
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python tools/benchmark_solver_process.py reference --reference-manifest tests/baselines/gqga4/reference_manifest.json --warmup 1 --samples 20 --output tests/baselines/gqga4/reference_performance_samples.json
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/integration/test_reference_baseline_identity.py -q
```

采样从启动每个独立进程前计时，到进程退出止，包含输入读取、求解、校验与结果文件写出；哈希核对与结果对照在计时之外。预热一次不计入正式 20 个样本。中位数取排序中间两项均值，第 95 百分位取排序第 19 项；同时保存最大值。

每个样本必须完整达到同样的 94024 次检查、相同停止原因和结果，差异样本不能静默剔除。JSON 在每次结束后保存，若中断保留已完成样本；工具拒绝覆盖现有证据文件。

首组正式性能样本已完成：中位数 `23.601609812496463` 秒，第 95 百分位 `23.711902249982813` 秒，最大值 `23.716102000005776` 秒。预热 1 次与正式 20 次全部达到相同结果、94024 次检查及 `search_complete`；没有删除或替换失败样本。

共享工作树累计回归为 63 项通过（0.27 秒）；保留原始字节后的暂存代码树 `ade45fe1ee7d0d944d006dab29082b9c1ffcf46a` 干净导出同样 63 项通过（0.25 秒）。两处脚本身份检查、阶段对照、Ruff 与格式检查均通过；干净导出编译检查通过。全部命令退出码为 0。

按计划第 7.1 节，在干净导出中另做“预热 1 次 + 正式 20 次”的同组基准命令；仅输出目标改为本项证据目录中的 `clean_export_performance_samples.json`，避免覆盖首组原始样本。此组已完成，退出码为 0，21 次均结果一致。中位数 `23.670567562483484` 秒，第 95 百分位 `23.92138983399491` 秒，最大值 `23.932215125008952` 秒；相对首组的中位数漂移约 `+0.2922%`，第 95 百分位漂移约 `+0.8835%`。采样时正式漂移门槛尚未批准；现已在正式性能文件中保留两项绝对漂移各不超过 20% 的政策。

干净导出验证命令与共享工作树相同；累计检查命令组外层耗时分别为 7.185 秒与 8.718 秒，后者包含额外编译检查，均不含单独记录的基准运行。暂存代码树与当前生产包、工具、测试、输入和参考基线字节一致，之后只补充状态文档和本项证据，不更改计时工作负载。

代码复核增加两个失败保护：超时保留完整失败样本和已有输出，采样后继续核对输入身份；报告使用唯一临时文件原子写入，不覆盖预存同名临时文件或其软链接目标。两项模拟负例通过，不启动真实求解器。首组采样启动后加入这两项失败路径保护，成功路径的计时范围、外部命令和统计公式均未改变；干净导出复核使用补齐保护后的工具。

16 个稳定残留内容与原始 992 个易变路径仍保持；观测到一个新增 Ruff 缓存路径，已由保护脚本记录，未暂存或提交，没有删除旧文件。

## 用户确认的正式门槛

2026-09-03，用户要求以指定 `solver.py` 的实绩为目标，复跑后确认最多 22 条链、欠重链 0 条、禁止违规 0 项；随后明确将新实现完整运行时间门槛放到 3 分钟。正式文件为：

- `tests/baselines/gqga4/quality_gate.json`：22/0/0，531 个真实来源订单与 29333.91 吨完整覆盖及来源重量守恒；受控拆单授权、目标期与追溯、不得延后、虚拟输出重量占比不超过 5%、搜索与无缓存审计一致、结果契约自检继续保留。本次 GQGA4 不允许任何最终偏差，不修改通用发布契约仍允许欠重的语义。
- `tests/baselines/gqga4/performance_gate.json`：完整外层进程耗时中位数和第 95 百分位均不超过 180 秒；Python 3.10.18，各预热 1 次、各正式运行 20 次，交替配对；参考控制组两项统计相对冻结参考基线的绝对漂移均不超过 20%。最大单次耗时另行保存，不新增为失败门槛。

两份文件关联原始参考清单哈希，性能文件还关联首组原始样本哈希。没有改写 `reference_manifest.json`、原始样本、阶段轨迹、参考脚本或输出。原 `gate_proposal.json` 保留 37/3 与 30 秒的历史数值，但标记为已被正式文件取代，不再作为执行依据。

冻结发生在任何新求解器质量或性能结果产生之前。参考脚本原有的 1 项禁止违规仍保留为事实，不因门槛确认而改为成功；新实现能否同时达到所有门槛仍由最终完整验收证明。

### 用户指定命令的追加复跑

在 macOS arm64 使用 `/usr/bin/python3` 3.9.6、种子 590531、30 秒搜索参数、100000 次候选检查预算复跑原脚本。用户给出的四个旧 JSON 路径已不存在，改用 `tests/baselines/gqga4/inputs/` 中字节哈希相同的四份副本；CSV 仍使用用户指定原路径。

```sh
cd /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv
PYTHONDONTWRITEBYTECODE=1 /usr/bin/time -p /usr/bin/python3 outputs/solver.py \
  --input-orders work/archive/input_orders.csv \
  --optimization-problem /Users/miles/dev/dev-py/APSGOV6/tests/baselines/gqga4/inputs/optimization_problem.json \
  --resolved-rules /Users/miles/dev/dev-py/APSGOV6/tests/baselines/gqga4/inputs/resolved_rules.json \
  --rule-context /Users/miles/dev/dev-py/APSGOV6/tests/baselines/gqga4/inputs/rule_context.json \
  --solver-config /Users/miles/dev/dev-py/APSGOV6/tests/baselines/gqga4/inputs/solver_config.json \
  --output-dir /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/work/manual_test_590531_recheck.b1b0ya27 \
  --seed 590531 --time-budget-seconds 30 --candidate-check-budget 100000
```

退出码为 0；外层 `real` 为 28.09 秒，脚本内部记录 27.987295 秒；94024 次检查，`search_complete`，22 链、0 欠重、1 项禁止违规、540 吨虚拟材料。状态仍为 `BEST_EFFORT`，不能以退出码 0 判断产品合格。

结果明细、链汇总、校验报告三份文件与固定静态产物逐字节一致，首个语义差异为空；运行清单仅计时和输入路径不同，其 SHA-256 为 `d42f575f5576e8d11a1298157f645d7cf286e84c8a36cbff18970e5946af5cdd`。原 `work/manual_test_590531` 已有结果未覆盖，脚本、原输出、输入和冻结副本的运行前后哈希全部不变。这次单次 Python 3.9 测量仅作用户命令复核，不替换正式 Python 3.10 的 20 次性能基线。

### 门槛冻结后的回归边界

本轮仅增加两份正式门槛、门槛身份测试及状态说明，不改动参考脚本、采样工具或求解工作负载，因此沿用已完成的两组各 20 次原始性能采样，不重复生成或覆盖样本。新增测试分别回读两组全部预热和正式样本，重新计算统计值、核对输入身份与每次结果；共享树和暂存树干净导出执行相同检查。

累计回归初次验证为 66 项通过（0.32 秒）；最终提交检查记录如下。

| 检查 | 共享工作树 | 暂存树干净导出 |
|---|---|---|
| 基线聚焦测试 | 20 项通过，0.23 秒 | 20 项通过，0.24 秒 |
| 架构与基线累计测试 | 66 项通过，0.31 秒 | 66 项通过，0.27 秒 |
| 五份输入、脚本与静态输出身份 | 通过 | 通过 |
| 原始阶段轨迹与静态结果对照 | 一致，首个差异为空 | 一致，首个差异为空 |
| Ruff 0.12.0 静态与格式检查 | 通过，11 个 Python 文件 | 通过，11 个 Python 文件 |
| 残留保护 | 16 个稳定内容、992 个原始易变路径保持，新增缓存未暂存 | 没有带入旧残留 |
| 编译检查 | 不在共享树执行 | `compileall` 通过 |

上述通过命令的退出码全部为 0。首次尝试 `conda run -n apsgo_v6_3.10.18 python -m ruff` 因该环境未安装 Ruff 而退出 1；随后使用工程已有的 `/Users/miles/anaconda3/bin/ruff` 0.12.0，没有新增依赖。该工具发现新测试文件需格式化，仅格式化该文件后再次通过检查，不涉及采样工具或求解逻辑。

验证的暂存树为 `b6ace1b005d61018e6fd2a068f7b040e1ea0aa8c`，导出到 `/var/folders/gr/wzbstdss0ps8t5ss2nbdwkn00000gn/T/apsgo-gate-freeze-5bw3nn2m`。之后只补齐本项说明与状态，不修改被测代码、正式门槛或基线；提交前再从最终暂存树导出执行累计回归与身份核对。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/capture_solverpy_reference.py --verify-only
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/compare_solver_stages.py --reference-only
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
# 仅在干净导出执行
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```

正式质量文件 SHA-256：`5c6e83148771f0ebc3c71bb970908c63fc53ecb0e9078ec2658bbc1cd39ddee9`；正式性能文件 SHA-256：`e6a472ea83288b67e2902fd47c11cf312d174a3cd9803f91f71b8dbbed8209cb`。新文件和修改文件均为 UTF-8 无 BOM，暂存白名单共 23 个文件。提交前执行 `git diff --cached --check`；原始脚本、参考产物、目标设计和旧残留不在本项修改中。

下一项：功能 2“建立核心值类型、不可变领域模型与诊断契约”。本次只完成参考基线与门槛冻结，不宣称新求解器质量或性能已通过。
