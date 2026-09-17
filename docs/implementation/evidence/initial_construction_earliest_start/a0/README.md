# A0：初始构造基线与最小见证

## 1. 结论与范围

2026-09-17，按用户“先执行第一步”开展实施计划 A0。**基线身份、输入清单和合成见证已交付；生产数值构造尚未执行，A0 退出门槛未全部通过，不进入 A1/A2。**

固定提交：`08a4d24faf897afdcd6c20a796a6f85f145cbc1a`；分支 `codex/earliest-process-start-constraint`。源码树为 `d6c2952075634e78cf4c4a621a45f99f577ff184`。身份均来自 GitHub 当前提交树，不把仓库历史 Mac 路径当作当前执行主机。

本次不修改生产算法、已有测试断言、规则/回写政策、配置、数据库、CI、前端或服务；不实现链阈值辅助、调序、分段或借单检查。依据为[实施计划 A0](../../../apsgo_v7_initial_construction_earliest_start_implementation_plan.md)与[设计第 3.1 节](../../../../design/apsgo_v7_initial_construction_earliest_start_design.md)。

## 2. 可复用交付物

| 文件 | 用途 |
|---|---|
| [a0_cases.json](../../../../../tests/baselines/initial_construction_earliest_start/a0_cases.json) | 六组合成输入和手算期望；明确不是生产观测结果 |
| [capture_initial_earliest_start_baseline.py](../../../../../tools/capture_initial_earliest_start_baseline.py) | 独立见证校验，以及固定源码树上的原构造采集入口；不运行局部搜索 |
| [独立见证测试](../../../../../tests/core/test_initial_earliest_start_baseline_witnesses.py) | 时间相加、覆盖/期间、损坏输入、不可覆盖输出和运行阻塞保护 |
| [baseline_manifest.json](baseline_manifest.json) | 源码/依赖/历史输入身份、环境差异、未完成项和退出门槛 |
| [local_validation.json](local_validation.json) | 本轮实际测试结果与数值入口阻塞摘要 |

Git blob SHA 与业务请求/规则/字典指纹是不同概念。真实运行尚未产生的指纹、图和质量均保留 `null`，不从 Git SHA 推造。合成输入仅用 A/B/C/D 标识，不追加真实订单原文。

## 3. 六个固定见证

所有合成订单均为 100 吨，用时固定一小时，开始时间 `2026-06-01T08:00:00+08:00`。重量和用时是测试输入，不代表订货量切换，也不声称由炉区速度推算。大部分案例用两组互不重叠的温区隔开工艺路径，以便人工核对图；完整工艺判断仍等待原实现复核。

| 用例 | 已固定的手算结论 | 能说明什么 |
|---|---|---|
| 同期间两链 | 原 `[A,B]→[C,D]` 中 B 提前 2 小时；手工交换整链后为零 | 初始顺序本身能够造成提前；不是新算法已经完成 |
| 链内阻塞 | `[A,B,C]` 中 B 提前 2 小时 | 链首就绪不等于整链就绪；阶段 A 不承诺解决内部阻塞 |
| 全未就绪 | 四个节点各提前 1 小时 | 不跳时钟、不丢单；零违规并非无条件可得 |
| 混合期间 | 最终顺序 `[C,D]→[A,B]`；D 提前 2 小时 | 应按最终期间分组累计时间，跨期不重置；未来来源仍检查 |
| 停用且缺少下界 | 原结构不变，不计算本规则违规 | 不补造下界；停用兼容性需在真实代码继续复核 |
| 到轮到时恰好就绪 | 原顺序均零提前 | 不能错误要求所有后继在链开始时已经就绪 |

期望由人工预先固定，独立校验器只做逐节点时间相加、覆盖和期间检查，不实现 A1 的 `R(L)` 辅助，也不是另一套生产规则引擎。图、覆盖和链序的 JSON 字段标注 `hand_calculated_not_observed`；不能据此声称已跑过原构造。

当前对“提前来源”的结论限于上述合成顺序。真实剩余订单究竟在初始阶段还是后续借单中引入，本轮没有逐阶段运行证据，不下根因结论。

## 4. 实际执行与环境阻塞

本轮工作目录是 Linux/bash 中的隔离新文件草稿，不是完整仓库导出，也无法看到用户本机未提交差异。

| 项目 | 本轮实际 | 仓库参考 |
|---|---|---|
| Python | 3.13.5 | environment.yml：3.10.18 |
| NumPy | 2.3.5 | pyproject.toml：2.2.6 |
| Numba / llvmlite | 0.65.1 / 0.47.0 | 0.65.1 / 0.47.0 |
| pytest | 9.0.2 | environment.yml：8.4.2；pyproject.toml：>=8.4,<9 |
| Uvicorn | 0.48.0 | environment.yml：0.40.0；pyproject.toml：>=0.40,<0.41 |

实际命令与结果：

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -c /dev/null -p no:cacheprovider -q \
  tests/core/test_initial_earliest_start_baseline_witnesses.py \
  --junitxml=/mnt/data/apsgo-a0/a0_witness_junit.xml
```

**24 passed，退出 0；仅独立见证和采集工具保护测试。**独立 Python 语法解析通过。`-c /dev/null` 用于隔离新测试文件，不能称为按项目 pytest 配置完成集成验证；0.11 秒不是排程性能数据。

`git clone` 实际失败，退出 128：`Could not resolve host: github.com`。GitHub 连接器读取成功，但没有完整源码进入容器。另尝试数值采集入口，因草稿不是 Git checkout，在源码身份检查处退出 2，`numeric_executed=false`；未进入 NumPy 构造。依赖差异也已登记，没有擅自改仓库环境文件。

## 5. 原实现采集的复现入口（尚待执行）

在依赖匹配、生产源码树仍为上面固定身份的完整干净 checkout 中执行；工具依赖仓库已有 `tests` 夹具，属于开发工具，不是部署命令。输出路径必须不存在：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python \
  tools/capture_initial_earliest_start_baseline.py \
  --mode numeric --output /tmp/apsgo-a0-numeric-baseline.json
```

该模式调用 `build_numeric_construction_graph()`、`numeric_minimum_path_cover()`、`construct_numeric_initial_plan()`、`evaluate_numeric_plan()`，记录实际图/覆盖/链序、开始时刻、最早违规数和严重度、完整九项质量、计数和请求/规则/任务身份。手工交换链仅作为反事实校验，不由优化器生成，也不代表 A2 实现。

合成构造验证预算独立声明为 600 秒总时限、30 秒收尾、0 次局部搜索候选。它只用于这六个小例，覆盖可能的首次编译开销，不写服务 YAML，不冒充当前现场预算，不调用后续搜索或整月验收。工具拒绝覆盖已有输出，拒绝不同源码树/脏源码，不用新算法输出改写旧期望。

原构造运行和既有必要测试均未在本轮执行。补齐这些证据后才能关闭 A0；实际不一致必须分析，不得修改历史期望来消除差异。A1/A2/A3/A4 均保持原门槛。

## 6. 历史真实输入和已知差异

已通过 GitHub 元数据确认[历史目录](../../earliest_process_start_constraint/real_20260601/README.md)中存在完整 `enabled_request.json`、`disabled_request.json`、HTTP 输入、`lower_bounds.json` 和 `comparison.json`；对应 blob 身份见清单。不是“没有真实数据”，而是尚未核验其是否符合当前重量口径及本次实验要求，也未进行新的运行。

历史 531 单、规则 19、40 万候选等数据只属于该历史实验，不作为当前活动库或现场参数。本轮不复制订单原文、不读取现场 SQLite、不改历史证据；A4 的输入指纹和质量验收保留待确认/待执行。

已有回写策略差异继续单列：当前 `final_audit.py` 的阻断计数固定为零，而 `test_business_violation_writeback.py` 仍断言启用且提前时应阻断。这是静态契约冲突，本轮未运行该测试，不编造失败数量，不改断言来制造全绿。

## 7. 提交与停止点

本次提交只包含新增的合成见证、采集/测试代码、三份证据文件及实施计划更新；保留原 `src/`、配置、数据库和工作流树。提交消息包含 `[skip ci]`，用于避免既有 push 工作流默认执行全量 pytest，不修改 CI 配置或取消他人的作业。只关联 `Refs #1`，不关闭 Issue。

停止点：**A0 部分完成；待匹配环境中的原构造数值采集和必要原测试。不得以 24 项独立测试通过替代这一门槛。**
