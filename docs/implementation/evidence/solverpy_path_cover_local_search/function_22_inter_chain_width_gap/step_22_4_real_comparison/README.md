# 功能 22.4：链间宽差目标真实对照与图表

[打开新七级交互报告](visualization/report.html) · [全部 21 处链间边界](run_new_seven/chain_boundary_detail.csv) · [新结果质量报告](run_new_seven/quality_report.json)

## 结论与范围

新旧两次实际求解均通过本次固定质量门：零禁止违规、零欠重、22 条链。新流程虚拟材从 400 吨降为 320 吨；相对于旧方案按权威计划期稳定分组后补算的诊断值，宽差总和从 11998 mm 降为 11218 mm，减少 780 mm（约 6.50%）。**质量改善，但单次公共服务耗时从 62.94 秒增至 109.46 秒；不能称为性能改善或 20 对性能验收通过。**

本项实施前提交为 `b02ec8c2d89f02fc2cb0bcbb65afee0f262e9f1c`，不再修改生产求解器、规则、配置、原始五输入或门槛。只添加证据观察、真实结果与图表；当前代码仍复用前面三项实现。

| 项目 | 旧六级独立复跑 | 新七级独立复跑 |
|---|---:|---:|
| 代码提交 | `bb5d92c1f21b89a0c17091d07bdd486cb59dcdf1` | `b02ec8c2d89f02fc2cb0bcbb65afee0f262e9f1c` |
| 禁止违规 / 欠重链 / 链数 | 0 / 0 / 22 | 0 / 0 / 22 |
| 虚拟材数量 / 重量 | 20 / 400 吨 | 16 / 320 吨 |
| 原订单 / 真实片段 | 531 / 533 | 531 / 533 |
| 真实总重量 | 29333.91 吨 | 29333.91 吨 |
| 链间宽差 | 11998 mm，独立派生诊断，非原生评分 | 11218 mm，原生第七项 |
| 候选检查次数 | 102475 | 94518 |
| 完整候选评价次数 | 1061 | 2627 |
| 接受动作数 | 46 | 50 |
| 同期拆单 / 未来借入归还拆单 | 2 / 0 | 2 / 0 |
| 构造图检查 / 允许边 | 140715 / 32536 | 140715 / 32536 |
| 匹配边 / 路径数 / 初始链数 | 514 / 17 / 31 | 514 / 17 / 31 |
| 公共服务耗时 | 62.937681 秒 | 109.463667 秒 |
| 停止原因 | 自然结束 | 自然结束 |

两次运行使用相同的 531 单、任务期序、种子 590531、200000 次上限、180 秒总预算和 10 秒收尾保留；顺序执行，无其他测试或求解并行。两次外层工具退出均为 0，双审计、来源覆盖和逐原订单重量守恒通过。表中时间包含附加观察开销，是单次公共服务观测，不是 20 对外层进程样本。

新质量键为 `(0, 0, 0, 0, 22, 320, 11218)`。原参考 `solver.py` 的第七项为借用重量，旧 V6 原生只有六项，新 V6 第七项为宽差；三者不能当作同一个七维指标相减。

## 搜索分歧与纯调序效果

1. 初始方案先完成原构造与编号，再按任务期序稳定分组；31 条链内容完全不变。原序第七条为 `initial-000007`，分组后第七条为 `initial-000008`。前后方案身份分别为 `6d3a206937521f3fac5ca727c05cc561052a63855f081874321fbf0e2a6e0427`、`5b23fbb27e3ad0a650b1fca72293f6c6d4d0f2613120d21483c4332c1019e5db`。
2. 首次实际整链候选枚举分歧出现在记录的第 5 次生成器调用，双方此前均检查 85 次：旧版供给链为 `initial-000007`，新版为 `initial-000010`，目标链均为 `initial-000005`。因此流程在新增调序阶段之前已经分歧，不能把全部变化归因于最后一次排序。
3. 第一次“前六项相同、仅宽差改善”的接受是第 34 个动作、候选检查第 78214 次：移动 `initial-000001`，宽差 11505→11295 mm，前六项维持 `(2,170.3,1,194.42,22,280)`。当时尚有待修复违规，并非提前宣布可发布。
4. 新流程首轮纯调序：311 次完整候选评价，1 次接受，宽差减少 210 mm；拆后唯一重放中的纯调序：1183 次完整候选评价，4 次接受，11630→11218 mm。两轮分别验证链身份到链内容的映射完全不变，前六项不变，方案顺序身份变化。中间还有其他动作，不能将两轮改善简单相加当作相对旧版的最终净收益。

每轮前后链序及身份、实际阶段计数、第一次接受动作见 [新观察记录](run_new_seven/observation.json)；旧版对应记录见 [旧观察记录](run_old_six/observation.json)。枚举观察仅保留前 16 次整链候选对调用，不声称记录了每个候选。

## 耗时增加在哪里

以下每项的检查次数、完整评价数、接受数和时间都已扣除子阶段，拆单与重放不重复累计。

| 阶段 | 旧检查 / 完整评价 / 接受 | 新检查 / 完整评价 / 接受 | 旧秒 | 新秒 |
|---|---:|---:|---:|---:|
| 首轮整链合并 | 2539 / 366 / 9 | 4740 / 642 / 9 | 28.27 | 50.87 |
| 首轮单节点移动 | 79284 / 24 / 15 | 71136 / 29 / 16 | 0.83 | 0.92 |
| 首轮虚拟材填充 | 4590 / 494 / 11 | 2337 / 242 / 8 | 8.94 | 4.30 |
| 首轮整链调序 | 未启用 | 311 / 311 / 1 | — | 5.04 |
| 拆单本身（不含重放） | 2 / 2 / 2 | 2 / 2 / 2 | 0.15 | 0.16 |
| 拆后整链合并 | 2199 / 126 / 1 | 2597 / 196 / 2 | 20.18 | 24.60 |
| 拆后单节点移动 | 13861 / 49 / 8 | 12212 / 22 / 8 | 0.88 | 0.44 |
| 拆后虚拟材填充 | 0 / 0 / 0 | 0 / 0 / 0 | <0.01 | <0.01 |
| 拆后整链调序 | 未启用 | 1183 / 1183 / 4 | — | 19.39 |

本次总时间增加约 46.53 秒；新增两轮纯调序耗时约 24.43 秒，且它们每个候选都进行了完整评价。另一个主要增量来自分组后搜索路径改变，使整链合并的生成与评价增加。总候选次数虽然减少，但完整评价从 1061 增至 2627，因此不能用候选总次数直接估算耗时。原节点连接缓存仍在使用，新版命中率约 87.11%；本次没有依据把增加归咎于“没有连接缓存”。

拆单仍实际执行两次，唯一重放完整结束，未撞时间或次数上限。本项不临时改评价方式、调度顺序或放宽门槛；性能优化需后续依据这些阶段证据处理。

## 边界、身份与图表

新结果有 549 个节点、22 条链、21 处相邻边界，无首尾闭环。实际排产期分布为第一期 18 链、第二期 2 链、第六期 2 链；任务仍含第三期，只是没有链归到该期。所有表和图保持发布链序，不按链编号重新排序。

两处跨期边界为第 18、20 处，宽差分别 395、675 mm。第 20 处前链实际尾部是 `virtual-000013`，宽 1000 mm，后链实际首单宽 1675 mm，675 mm 正式计入目标，没有跳过虚拟端点。这是两条链的换链目标，不能误当成链内连接规则违规。

旧方案不修改，稳定分组诊断另存原/派生身份：`ca8d5cab1336d2cf86d4bc7760db6f75a1dfcc1bf1a2ce27056d188813ceb939` → `99df8d89b1a1add727ba07bcc5f65ac4a04b5dde79e50358fb67e3afe98fed6b`。旧复跑的排程 CSV、接受轨迹与原 20 万次证据逐字节一致，公开结果身份仍为 `1f1d956fb888a071272f5f6523ccb62f03a44221f3ff00da379a7d332163d98b`。

新公开结果身份为 `05ddff4ca548f2129840696f41acfd564b1e10e07e4ae34a0e72ee4fb4be34e6`，原生方案身份为 `e10761a18e5f2f4443e62bd7f54c67010149fded0b10a8c025425ffdd9152533`。逐对端点、规则原生贡献、原始指标和质量末项均核对为 11218 mm。

图表含宽度、厚度、允许温区、每链重量、交期分布、虚拟数量/重量及新增宽差图。每链重量范围为 700.07～1999.20 吨。交期只展示来源日期，不推算完工或延期；温区不是实测炉温。静态预览：[规格](visualization_previews/preview_trends.png)、[统计](visualization_previews/preview_statistics.png)、[交期](visualization_previews/preview_delivery.png)、[宽差](visualization_previews/preview_boundaries.png)。

离线浏览器实际核对 4 个图区、549 个宽度节点、21 根边界柱及合计、22 行链统计；原生鼠标悬停和单链/全量切换正常。1440 和 736 像素宽度无页面横向溢出、无脚本异常，四份预览已逐图检查。记录见 [浏览器核验](visualization_previews/browser_check.json)。

## 复现命令与保护边界

实际平台为 macOS / Darwin arm64 / zsh；求解与回归使用 Conda `apsgo_v6_3.10.18`。本次代码导出分别位于 `/tmp/apsgo-width-comparison-vFv8gM/old` 和同目录 `new`，结果保存于本文目录。下面命令使用新的临时目录，避免覆盖本次证据：

```bash
task_repo=/Users/miles/dev/dev-py/APSGOV6
task_trial=$(mktemp -d /tmp/apsgo-width-check-XXXXXX)
task_evidence="$task_repo/docs/implementation/evidence/solverpy_path_cover_local_search/function_22_inter_chain_width_gap/step_22_4_real_comparison"
task_python=/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python
mkdir "$task_trial/old" "$task_trial/new"
git -C "$task_repo" archive bb5d92c1f21b89a0c17091d07bdd486cb59dcdf1 | tar -xf - -C "$task_trial/old"
git -C "$task_repo" archive b02ec8c2d89f02fc2cb0bcbb65afee0f262e9f1c | tar -xf - -C "$task_trial/new"
PYTHONDONTWRITEBYTECODE=1 "$task_python" "$task_evidence/run_observed_precheck.py" \
  --code-root "$task_trial/old" --code-repository "$task_repo" \
  --code-revision bb5d92c1f21b89a0c17091d07bdd486cb59dcdf1 --output-dir "$task_trial/run_old_six"
PYTHONDONTWRITEBYTECODE=1 "$task_python" "$task_evidence/run_observed_precheck.py" \
  --code-root "$task_trial/new" --code-repository "$task_repo" \
  --code-revision b02ec8c2d89f02fc2cb0bcbb65afee0f262e9f1c --output-dir "$task_trial/run_new_seven"
```

两个版本分别在独立 Python 进程导入，保护文件与所指 Git 提交逐项核对；不在同一解释器混载两版代码。观察包装只返回原对象、读取现有状态，不增加预算/取消轮询，不重排候选；仅保留前 16 次整链候选生成器调用，未记录完整候选流。阶段时间包含包装开销，嵌套重放扣除后再与核心累计值核对。

每次原预检输出的六个文件保持不变；新增 `observation.json`、`chain_boundary_detail.csv` 由独立 `observation_manifest.json` 绑定原质量报告哈希及观察脚本哈希。观察脚本 SHA256 为 `98725e97d54fa31d4d7dfc36d7ff199d09d4170c601a55d9668c1806dfd57c03`。原预检内的观察说明仅描述它自己的包装，完整新增包装范围以补充记录为准。

绘图使用已安装 Python 3.13.9 / Plotly 6.3.0，仅供证据使用；不增加生产依赖。求解仍使用项目 Python 3.10.18。以下命令接续上面的变量：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/bin/python \
  "$task_repo/docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/budget_200000/visualization/generate_report.py" \
  --result-dir "$task_trial/run_new_seven" \
  --input-orders "$task_repo/tests/baselines/gqga4/inputs/input_orders.csv" \
  --rules "$task_repo/tests/baselines/gqga4/gqga4_rule_set_spec.json" \
  --output-dir "$task_trial/visualization"
NODE_PATH=/Users/miles/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/miles/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  "$task_evidence/inspect_visual_report.cjs" "$task_trial/visualization/report.html" "$task_trial/previews"
```

原旧图表的五个文件已用真实 Plotly 在新临时目录逐字节复现一致。新报告的 HTML 内嵌原始 Plotly 库，沿用现有约定仅对该生成 HTML 关闭文本差异；不改写库字节。图表清单保留 HTML、四份 CSV 的哈希和补充观察清单身份。

## 验证与未完成项

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/integration/test_inter_chain_width_precheck.py tests/integration/test_inter_chain_width_visual.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/integration/test_inter_chain_width_precheck.py tests/integration/test_inter_chain_width_visual.py tests/integration/test_quality_precheck.py tests/integration/test_gqga4_visual_report.py tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/bin/python -m pytest -p no:cacheprovider tests/integration/test_inter_chain_width_visual.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

项目环境中的真实绘图库测试因未安装 Plotly 单独跳过，但在已有绘图环境真实执行；其余数据、身份、端点、默认入口、预算和回读测试不跳过。原 33 项参考身份验证通过，原始参考与 V3 外部归档未修改。四图浏览器核验成功，未更改交期或温度含义。

共享树专项 52 通过/1 可选绘图库跳过（1.01 秒），聚焦及参考 133 通过/1 可选绘图库跳过（1.69 秒）；真实 Plotly 41 通过（0.44 秒）；仓内累计 2723 通过（175.79 秒），退出均为 0。113 个 Python 文件静态/格式检查和浏览器检查脚本语法检查通过。开发期虚拟角色测试曾误写 `generated_virtual`，已按既有枚举值 `virtual_sphc` 修正，不修改生产或原数据；实际新旧求解及图表运行均一次成功。

最终暂存树使用同一专项、聚焦、仓内累计及绘图测试复验；从最终导出重跑新七级，核对发布身份、方案、轨迹和计数，计时允许变化。另用同一份冻结新结果，由最终导出的绘图器重生成图表，逐字节比较；不把两次求解产生的计时及质量报告哈希差异误判为绘图不确定性。共享结果与最终树、导出路径、数量、耗时记于本节及本项提交正文，不在文档内自引用当前提交。

本项不等于功能 21 全部收口：多产线、规定次数的确定性与 20 对全流程性能验收仍需执行。本次两个实际案例均未触发未来借入归还拆单，不能声称该真实数据验证了该分支；构图和路径仅核对所列计数，不声称保存并逐边对比了完整图。新宽差没有另设数值门槛。
