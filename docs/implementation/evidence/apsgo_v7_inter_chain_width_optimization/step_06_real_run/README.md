# 步骤 6：完整排程、审计与宽差收益

[打开新报告](report/report.html) · [原始质量与双审计](run_current/quality_report.json) · [全部订单属性](report/all_order_details.csv) · [相邻链边界](report/chain_boundary_detail.csv)

## 首次真实结果

代码为 `6902be0862ce6573f27d5fa28291655e9e8e482a`，从当前提交独立导出至 `/tmp/apsgo-v7-width-step6-start.T4sf2J`；开始工作区干净，原残留检查 `clean_export` 通过。与步骤 0 对照使用相同冻结输入、规则、七级顺序、种子 590531、200000 次候选上限、180 秒总时间及 10 秒收尾预留。求解时没有并行回归或另一份排程。

| 指标 | `main` 基线 | 本次不带热路径观测的结果 |
|---|---:|---:|
| 禁止违规 / 欠重链 | 0 / 0 | 0 / 0 |
| 原订单 / 真实片段 | 531 / 533 | 531 / 533 |
| 真实总重量 | 29333.91 吨 | 29333.91 吨 |
| 链数 | 22 | 22 |
| 总宽差 | 11218 mm | 10912 mm |
| 最大单次宽差，仅诊断 | 820 mm | 820 mm |
| 虚拟数量 / 重量 | 16 / 320 吨 | 30 / 600 吨 |
| 候选检查 / 完整评价 / 接受 | 94518 / 2627 / 50 | 200000 / 3603 / 58 |
| 同期间拆单 / 未来借入归还拆单 | 2 / 0 | 2 / 0 |
| 公共求解服务时间 | 109.671247 秒 | 167.609114 秒 |
| 停止原因 | 原搜索自然结束 | 候选检查达到上限 |

本次七级为 `(0,0,0,0,10912,600,22)`。宽差降低 306 mm，约 2.73%；虚拟材增加 14 个、280 吨，比例约 2.00%，仍满足 5% 上限。这是当前“前四项不变时宽差优先于虚拟重量、再优先于链数”的已确认选择，不是等权评分。

质量门、无搜索缓存核心审计、公开结果自检及逐来源重量守恒均通过。没有改链重、逆宽、连续规则、拆单资格、输入或门槛。一次改善不表示达到全局最优，链数相同也不说明增链能力无效；应分别报告本次实际动作覆盖和小样本已验证能力。

当前新增集中精修耗时 64.303385 秒；旧局部搜索 57.767199 秒、受控拆单及其旧搜索重放 42.011734 秒。旧阶段工作量是否原样保留，由原始前缀轨迹和独立阶段观测核对；上述单次服务时间不是外层进程的 20 对性能验收，也不能作为性能优化结论。

## 原始执行命令

第一条在上述干净导出目录运行，其余绘图和浏览器检查在 V7 仓库根目录运行。重试须换新输出目录，工具拒绝覆盖已有结果。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py --code-repository /Users/miles/dev/dev-py/APSGOV7 --code-revision 6902be0862ce6573f27d5fa28291655e9e8e482a --output-dir /Users/miles/dev/dev-py/APSGOV7/docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_06_real_run/run_current
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/bin/python docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/budget_200000/visualization/generate_report.py --result-dir docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_06_real_run/run_current --input-orders tests/baselines/gqga4/inputs/input_orders.csv --rules tests/baselines/gqga4/gqga4_rule_set_spec.json --output-dir docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_06_real_run/report
NODE_PATH=/Users/miles/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules /Users/miles/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node docs/implementation/evidence/solverpy_path_cover_local_search/function_22_inter_chain_width_gap/step_22_4_real_comparison/inspect_visual_report.cjs docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_06_real_run/report/report.html docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_06_real_run/previews
```

三条均退出 0；绘图使用本机已有 Plotly，不增加生产依赖。首条预检仅保留原有核心调用和缓存创建的各一次包装，不做每次候选的热路径观测。

## 报告与视觉核验

报告完整展示 563 个节点、22 条链、21 处边界，边界总和与原始指标及第五项质量值一致。保持原大辊期、链和链内节点发布顺序；温区是允许上下限，不是实测炉温；交货日期仅分布，不推算生产完工或延期。

离线浏览器核验四个图区、节点/边界/表格行数、合计、单链切换、全量恢复和悬停均通过；1440 与 736 像素视口无页面横向溢出、无脚本异常。主代理逐张视觉检查：[规格](previews/preview_trends.png)、[统计](previews/preview_statistics.png)、[交期](previews/preview_delivery.png)、[宽差](previews/preview_boundaries.png)，未发现缺图、遮挡或误序。

浏览器记录绑定 HTML 哈希 `051260c94fa47a072f3b6079b31cf6d3cf7dc2b87758a8ffb0f358e5f3bba21d`。原始质量报告哈希 `24be32566381aecb116eded10d5e24ef86337fc78d05e56e93352799e8350122`，公开结果指纹 `3d1689274bfa5f06d30411613e2fccdc14a9adc32464d9f4e7e8163fe0f04f67`，轨迹指纹 `aa9b16a7713d9f00e36c741600c483be9a5bcbbb6772e1d03caa850f122c12ae`。原六份输出字节不变；本目录禁用 CSV/JSON 换行转换，避免提交改写审计绑定。

## 观察工具与验证边界

新 `run_width_observed_precheck.py` 只复用原预检入口和旧观察工具中的安全导入/计数读取函数。分别记录原搜索、拆单整体、内层一次旧搜索重放、新精修及其动作和批次；内层重放不重复加到总数。每次动作回调前已扣检查，不能用回调内部计数差冒充检查次数。

原六份审计文件仍由原工具产生；补件另用 `width_observation.json` 和 `width_observation_manifest.json`，绑定代码、public/core 结果、输入、报告、脚本和依赖哈希。不复用旧观察代码中固定的第七项下标，宽差按声明定位。没有第二个求解入口或评价器，不改变候选、预算、取消或求解时钟读取；实际耗时仍可能受观察开销影响，带观察的结果不作为正式性能样本。

新增观察专项 21 项通过（1.14 秒），验证包装前后方案、轨迹、计数、停止及求解时钟/取消读取完全一致，非零重放不重复计数，坏计数/身份/哈希拒绝写补件。初次失败来自测试调整质量声明后未重签配置指纹，复用现有指纹函数修正夹具，未改生产。

共享树观察、原预检、参考身份和报告聚焦 170 项通过、1 项可选绘图库跳过（2.13 秒）；随后在已有绘图环境补验该报告文件，50 项全部通过（0.43 秒）。完整阶段观测、最终暂存树同范围复验及规定的确定性/性能仍按后续记录，不把首跑或专项单测称为全部验收。

## 实际动作覆盖和前置流程对照

同一代码的带观察复跑退出 0、正式质量门全部通过，公共服务 167.886101 秒、精修 64.400140 秒。原始排程、链明细、来源守恒及接受轨迹四文件与 `run_current` 逐字节相同；public/core 结果、两层审计、发布和轨迹语义指纹也完全相同。含耗时的公开 JSON 和质量报告不要求原始字节相同。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python /Users/miles/dev/dev-py/APSGOV7/docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_06_real_run/run_width_observed_precheck.py --code-root /tmp/apsgo-v7-width-step6-start.T4sf2J --code-repository /Users/miles/dev/dev-py/APSGOV7 --code-revision 6902be0862ce6573f27d5fa28291655e9e8e482a --output-dir /Users/miles/dev/dev-py/APSGOV7/docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_06_real_run/run_observed
```

[阶段和批次原始明细](run_observed/width_observation.json)及[身份清单](run_observed/width_observation_manifest.json)保存全部 18 个批次，未只选成功动作：

| 宽差动作 | 已扣候选检查 | 完整评价 | 接受 |
|---|---:|---:|---:|
| 单节点移动 | 39092 | 258 | 7 |
| 单节点交换 | 39085 | 662 | 1 |
| 连续段移动 | 6749 | 0 | 0 |
| 连续段交换 | 6749 | 56 | 0 |
| 切链与两段联合安置 | 13497 | 0 | 0 |
| 纯整链调序 | 310 | 0 | 0 |
| 新阶段合计 | 105482 | 976 | 8 |

原局部搜索消耗 78524 次检查、1224 次评价、34 次接受；原受控拆单及一次重放消耗 15994 次检查、1403 次评价、16 次接受。其中重放为内层明细，不再重复累加。精修开始时准确恢复基线 `(0,0,0,0,11218,320,22)` 及 94518 / 2627 / 50 计数，最终新增阶段与各动作、各批次合计均闭合。

8 次接受都发生在节点族，接受后按设计重建四类候选；最后一个已接受状态上的纯整链调序自然扫描完，其余三类仍有未扫描描述，最终因 200000 次额度停止。零完整评价不等于没有尝试：相关描述已扣次后，在角色、链重、期序、宽差或完整候选结构保护等检查中被拒绝；当前观察没有按拒绝原因进一步细分，不能仅凭这张表断言全部因某一规则失败。

不能声称“连续段和切链已贡献实际收益”，也不能声称它们没有潜在收益。当前证实的是：正式规则下总宽差严格降低、四类按共享预算均有实际访问、已接受变化全部通过双审计；不是无预算限制时的最优搜索。

## 独立一致性核验

独立只读检查逐单确认 531 个来源重量守恒，旧 549 节点（包括 16 虚拟、4 拆片）完整规范编码不变，新增仅 14 个各 20 吨的连接桥。前 50 条轨迹与基线相同；全部 563 行排程 CSV 的身份、所属期、位置、物理字段及谱系与公开方案一致，22 行链重量及报告节点基础字段一致。

按实际构造公式复算公开结果、发布、核心审计、公开自检与轨迹指纹；核对 48 个保护文件、HTML/四份报告 CSV 和浏览器记录哈希。审查中首次用裸报告字段重算核心审计指纹的临时断言失败，回到 `final_audit._outcome` 的实际完整载荷后复算通过，是检查方法修正，不是产物缺陷。

全部 21 个边界合计 10912 mm，最大仍 820 mm。第 18 处 BR1→BR2 为 250 mm，第 20 处 BR2→BR6 为 675 mm，两处均使用实际虚拟尾端；不跳过虚拟或跨期边界。

## 提交验证与后续

最终暂存树按上述 170 项聚焦范围、已有绘图环境 50 项和仓内累计范围复验；导出后重新生成报告，六份报告文件逐字节对照。实际命令、退出码、数量、耗时、暂存树和导出路径由本项提交正文保留。

首次暂存差异检查命中生成报告内嵌 Plotly 的原始行尾空格，退出 2；沿用既有 22.6 报告的精确 `report/report.html -diff -text` 产物属性，保留报告原字节及哈希，不修改第三方内嵌代码或全局检查规则。

本步骤实际收益与报告范围已验证，仍需步骤 7 的连续 3 次无墙钟截断运行及 20 对外层性能样本。不自动合并 `main`，不把一次约 168 秒服务时间视为性能门通过；也不自动关闭旧功能 21 的其他未完成范围。
