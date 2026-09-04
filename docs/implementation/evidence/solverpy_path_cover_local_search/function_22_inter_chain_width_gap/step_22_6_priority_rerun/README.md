# 当前目标顺序下的 GQGA4 完整重跑与报告

[打开本次交互报告](report/report.html) · [质量及双审计结果](run_current/quality_report.json) · [全部排程节点](report/all_order_details.csv) · [逐小辊期统计](report/chain_statistics.csv) · [21 处首尾宽差](report/chain_boundary_detail.csv)

## 本次结果

用户在 `c2d87bc92c3a69d0b104f5f2a8b1aff277e25c0b` 完成评分顺序及链数门槛修改后，要求重跑完整 GQGA4 并更新报告。本次真实调用唯一公开求解入口，退出 0，质量门槛全部通过；不是把旧质量向量换位后重用旧结果。

| 项目 | 实际结果 |
|---|---:|
| 禁止违规 / 欠重链 / 超重链 | 0 / 0 / 0 |
| 原订单数 / 真实排程片段 | 531 / 533 |
| 真实订单总重量 | 29333.91 吨，逐原订单守恒 |
| 虚拟材数量 / 重量 | 16 / 320 吨 |
| 排程总节点 / 总重量 | 549 / 29653.91 吨 |
| 小辊期数 | 22；这是实际结果，不是验收上限 |
| 每链重量范围 | 700.07～1999.20 吨 |
| 相邻链首尾宽差总和 | 11218 mm，共 21 处边界 |
| 候选检查 / 完整候选评价 / 接受动作 | 94518 / 2627 / 50 |
| 同期间拆单 / 未来借入归还拆单 | 2 / 0 |
| 公共求解服务单次耗时 | 103.485174 秒 |
| 停止方式 | 自然结束，未触及时间或次数上限 |

当前质量向量为 `(0, 0, 0, 0, 11218, 320, 22)`，依次为禁止违规数、禁止严重度、欠重链数、欠重总缺口、链间宽差、虚拟重量、非空链数。全部越小越好，逐项比较，不是加权总分。链数无验收上限；零禁止、零欠重、来源覆盖守恒、虚拟比例不超过 5%、不得延后原计划期、拆单授权与双审计仍全部检查。

种子为 590531，候选检查上限 200000，总时间配置 180 秒、收尾预留 10 秒，均未改动。完整求解期间未并行运行测试或其他求解。上述 103.49 秒是预检工具测量的公开服务调用耗时，不是外层进程 20 对样本的性能验收。

## 与上轮的区别

与[原宽差末项七级结果](../step_22_4_real_comparison/README.md)按指标名称对比：**实际方案、全部指标和计数相同，没有观察到本次优先级调整带来的质量改善。**排程 CSV、链 CSV 和来源守恒 CSV 三份文件逐字节一致，原生方案身份也相同。配置、请求、评价及结果身份已按新优先级重新计算，不应相同。

旧质量 `(0,0,0,0,22,320,11218)` 与当前质量不能按相同下标直接相减。两次分别是不同配置下的独立真实运行；结果相同不表示配置没有生效，也不能推断所有可能候选的接受判断均相同。

上轮记录约 109.46 秒，本次约 103.49 秒；上轮还带有更细的阶段观察包装，且均只有单次样本，因此不宣称本次性能优化或性能门槛已验收。

## 图表与独立核对

报告包括全部节点的宽度、厚度、允许温区变化，逐小辊期重量、虚拟个数/重量、全局及逐小辊期交期分布，以及相邻链首尾宽差。沿用既有 Python / Plotly 工具，四个交互图区均可离线查看。

图表保持公开结果链序：第一大辊期 18 链、第二大辊期 2 链、第六大辊期 2 链；任务第三大辊期仍存在，只是本次没有链归属该期。横轴不是生产日历；温度为允许温区，不是实际炉温；交货日期只作来源分布，不据此判断延期。

报告从已由质量报告哈希绑定的节点和链导出复算相邻端点，合计与已审计原始宽差指标及**第五项**质量值一致。跨期边界为第 18、20 处，分别 395、675 mm；第 20 处前链尾部是虚拟节点 `virtual-000013`，1000 mm 接后链真实首单 1675 mm，完整计入。没有略去虚拟端点，也没有首尾闭环。

当前普通预检无需历史观察补件；报告不新造观察脚本或第二套求解逻辑。报告工具按质量声明定位宽差，兼容原第七项和当前第五项；若历史观察补件任一文件存在，仍要求整套补件完整且哈希、端点及合计匹配，不能静默跳过损坏证据。公开评价序列化只保存指标合计，不声称存在或核对了未导出的逐规则贡献。

独立核对包括：531 个来源逐单重量，方案/评价/CSV 链序，公开结果/方案/评价/资源/释放/轨迹身份，质量与报告产物哈希，全部 549 个报告节点和 21 处边界。未发现差异。旧六级和原宽差末项七级报告用本次绘图器重生成，合计 11 个文件与原始产物逐字节相同；未覆盖旧图表。

实际离线浏览器核验通过：4 个图区、549 个宽度点、21 根边界柱且合计 11218、22 行链统计；鼠标悬停和单链/全量切换有效，1440 与 736 像素视口无页面横向溢出，无脚本异常。四张预览已逐张人工视觉核对：[规格](previews/preview_trends.png)、[统计](previews/preview_statistics.png)、[交期](previews/preview_delivery.png)、[宽差](previews/preview_boundaries.png)；[机器检查记录](previews/browser_check.json)绑定本次 HTML 哈希。

## 身份与执行命令

| 身份 | 值 |
|---|---|
| 实际求解代码基线 | `c2d87bc92c3a69d0b104f5f2a8b1aff277e25c0b` |
| 规则集 | `d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0` |
| 请求 | `0d9ee1cfe251306bb516a12e440120923853bf9114b57df1864880ab0ffa5a35` |
| 原生方案 | `e10761a18e5f2f4443e62bd7f54c67010149fded0b10a8c025425ffdd9152533` |
| 公开结果 | `a20c1e8b082a66d828e838431cbbcca4f7a7de9952059764fc3961c054ec7563` |
| 接受轨迹 | `e97fa663be97b8289b102be0afcd23e76b23fb90b1f5a05bdd014189192ae50d` |
| 质量报告 SHA256 | `30a38f423dfa9f616cdef00b6e77267c2e4c571eeada97454ab327f1a22e41cd` |
| 本次 HTML SHA256 | `0fb9cfcf93bdf5c656d385fb914c1603f9407f273465ffce21171abad18d9efd` |

运行平台为 macOS / Darwin arm64 / zsh，求解使用 Conda `apsgo_v6_3.10.18`；绘图使用已有 `/Users/miles/anaconda3/bin/python` 环境的 Plotly，不新增生产依赖。以下为本次实际命令，工作目录为项目根目录；再次运行须更换输出目录，工具拒绝覆盖任何已存在目录。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py --code-revision c2d87bc92c3a69d0b104f5f2a8b1aff277e25c0b --output-dir docs/implementation/evidence/solverpy_path_cover_local_search/function_22_inter_chain_width_gap/step_22_6_priority_rerun/run_current
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/bin/python docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/budget_200000/visualization/generate_report.py --result-dir docs/implementation/evidence/solverpy_path_cover_local_search/function_22_inter_chain_width_gap/step_22_6_priority_rerun/run_current --input-orders tests/baselines/gqga4/inputs/input_orders.csv --rules tests/baselines/gqga4/gqga4_rule_set_spec.json --output-dir docs/implementation/evidence/solverpy_path_cover_local_search/function_22_inter_chain_width_gap/step_22_6_priority_rerun/report
NODE_PATH=/Users/miles/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules /Users/miles/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node docs/implementation/evidence/solverpy_path_cover_local_search/function_22_inter_chain_width_gap/step_22_4_real_comparison/inspect_visual_report.cjs docs/implementation/evidence/solverpy_path_cover_local_search/function_22_inter_chain_width_gap/step_22_6_priority_rerun/report/report.html docs/implementation/evidence/solverpy_path_cover_local_search/function_22_inter_chain_width_gap/step_22_6_priority_rerun/previews
```

上述三个命令退出均为 0。质量预检只包装核心调用和边缓存创建各一次，不做热路径跟踪；保护的生产代码、基准及输入在运行前后均与指明的提交逐项校验，原始参考身份也已核验。

## 回归与完成边界

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/integration/test_gqga4_visual_report.py tests/integration/test_inter_chain_width_visual.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_gqga4_quality_priority.py tests/app/test_gqga4_rule_set_mapping.py tests/integration/test_quality_precheck.py tests/integration/test_reference_baseline_identity.py tests/integration/test_inter_chain_width_precheck.py tests/integration/test_inter_chain_width_visual.py tests/integration/test_gqga4_visual_report.py -q
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/bin/python -m pytest -p no:cacheprovider tests/integration/test_inter_chain_width_visual.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

新增 9 个报告兼容案例，覆盖本次真实结果回读、当前第五项、无补件派生、原始指标与评分不一致拒绝、哈希缺失拒绝、单链及低精度上下文；同时回归历史第七项及损坏补件拒绝。项目环境未安装 Plotly 的原有可选测试在已有绘图环境真实补验，不省略图表验证。

共享树实际验证：报告专项 76 通过 / 1 可选绘图库跳过（0.28 秒），聚焦及参考身份 242 通过 / 1 可选绘图库跳过（1.98 秒），已有绘图环境 50 通过（0.43 秒），仓内累计 2739 通过（169.80 秒），退出均为 0。113 个 Python 文件静态与格式检查通过；上述历史报告 11 文件逐字节复现、当前四图浏览器与视觉核对也已通过。

共享树及最终暂存树使用上述同范围回归；实际数量、耗时、退出码、最终树和导出位置由本项提交正文记录，避免自引用提交。最终导出的报告工具需对同一份已冻结结果重生成，并与本次六个报告文件逐字节比较。生产代码、规则、策略、输入及门槛不随本项修改；只提交报告工具兼容、测试、本项证据和当前实施状态。

本项完成的是当前配置的一次完整 GQGA4 排程和报告更新，**不是功能 21 全部验收**。尚未补做多产线、规定次数确定性或 20 对性能测试，也不声称达到全局最优。未来借入归还拆单本次未触发，不以同期间拆单替代该分支的真实验证。
