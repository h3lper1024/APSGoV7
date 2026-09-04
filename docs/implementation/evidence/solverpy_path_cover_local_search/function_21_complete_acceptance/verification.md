# 功能 21：质量预检与后续验收状态

## 基线与范围

- 基线 `f9900b949845a54582a6b8813d4306f968ee5db0`，树 `e28425a00818af21e44ba3454347ad07e7dc2968`，分支 `codex/solverpy-path-cover-clean`。
- macOS Darwin 27.0.0 arm64 / zsh，Conda `apsgo_v6_3.10.18`；设计 v0.14 第 30.4、32.8/32.9、33 节，计划 v0.53 第 8.22 节。
- 本轮只增加质量预检及真实证据、既有参考身份核验的两路径系统文件排除、相应回归及计划/AGENTS。生产、冻结五输入、规则规格、策略、质量与性能门槛、外部参考文件均不修改。
- 开始残留保护通过，无关 `docs/design/assets/` 保留不提交。功能 20 最终树 2567 项累计通过，两个审计均通过，但两条欠重链仍不能当作 GQGA4 基准合格。

## 已确认的元数据处理

用户明确回复“忽略”：外部 V3 归档新增 `.DS_Store`、`rolling_final_repair/.DS_Store`。修改前原 170 文件全部存在、内容哈希全部相同，只是观测文件集合多了这两个系统文件。仅这两个常规文件不参与新增路径比较；其他新增文件与符号链接仍不能隐藏。冻结清单、聚合身份 `1a28933a36d019380f17116dbf8411b5163bd30910bf633bec8496c8f8c13897` 和外部文件保持不变。

## 当前执行状态

预检已执行，质量未通过，完整失败报告及明细已保存；不把允许偏差发布或代码测试通过当作质量通过。尚未完成全部阶段 A/B、多产线、三次确定性和 20 对性能验收，不能宣称功能 21 完成。

Ponytail Lite：复用现有请求映射、公开入口、签名编码及参考身份工具；不增加求解包装框架或生产 CSV 适配层。

## 已有阶段证据与不能推断的结论

| 实际对照 | 首轮结束检查数 | 最终检查数 / 停止 | 最终欠重 |
|---|---:|---|---|
| 冻结原始参考 | 77204 | 94024 / 自然结束 | 0 条 |
| 原算法仅取消逆宽承载牌号过滤 | 86413 | 100000 / 数量截断 | 2 条 / 204.42 吨 |
| 当前正式双模式 | 86413 | 100000 / 数量截断 | 2 条 / 204.42 吨 |

以上来自已执行的 [功能 17 对照](../function_17_controlled_split/verification.md)，不是本次重新执行参考程序。原始参考仍保留一项禁止违规；当前双模式已消除这项禁止违规，不能只看欠重就称原始参考全面合格。

首个已解释分叉见 [功能 14 对照](../function_14_real_node_relocation/verification.md)：第 3801 次检查把 `0030118934-000010` 插在位置 19，随后连接 `0002002180-000060`，宽度从 910 到 921 mm。目标按已确认 20 mm 规则允许；原始另要求承载牌号 SPHC，而该后继为 SPHETI-3，故拒绝并在第 3802 次接受另一位置。仅移除该过滤后，原算法和目标的 15 次单节点接受轨迹一致。

正式同期间拆分扩展在 86414、86415 次检查消除大订单连续重量禁止；重放最后一次改善为 97907，然后到 100000 在真实订单移动阶段截断。冻结原始参考最后消除欠重的五次接受均属单订单移动（94014、94015、94017、94020、94024），不能误称补跑虚拟填充就能达标。

目前只能确定：规则变化使搜索路径和数量消耗改变，原算法在同样变化下也出现相同欠重；没有超过当前额度的已存运行证据，不能宣称增大额度必然归零。是否提高诊断额度或修订流程，应在完整明细保存后另行决定，正式输入、规则、策略和门槛不静默调整。

## 已执行回归

| 范围 | 共享树实际结果 |
|---|---|
| 仓内累计 `tests/architecture tests/api tests/app tests/core` | 2567 通过 / 185.79 秒 |
| 外部参考身份与系统文件边界 | 33 通过 / 0.29 秒 |

两条命令退出码均为 0；新增 13 个元数据案例覆盖两个指定常规文件、未批准的新路径、指向文件/目录/不存在目标的符号链接。外部原 170 文件逐项核验通过，V3 清单文件 SHA-256 仍为 `cc617f1282a2f265e862590ba109e627de9b1101f48a3897ac0c16d59f1b0a29`；生产文件与功能 20 无差异。

## 真实质量预检结果

实际命令完整执行唯一公开入口，使用真实时钟、原种子 590531、原候选上限 100000、原总时限 180 秒。退出码 **2**，质量结论 **未通过（BEST_EFFORT）**，不是脚本异常。

仅两项检查失败：`maximum_underweight_chain_count` 与 `allowed_final_deviations`；两者都指向同一业务问题“仍有链重低于下限”，不是额外两类禁止违规。其余门槛、输入守恒、每个来源重量、拆单授权事实、评价一致性与第二层自检均通过。

| 欠重链 | 计划期 | 真实 / 虚拟 / 总重 | 距 700 吨下限 |
|---|---|---|---|
| `initial-000030` | BR_00000002 | 415.58 / 160 / 575.58 吨 | 124.42 吨 |
| `split-chain-partition-14eed158aa2fb5d0bb6b46f70510931e923f66a097fc0e062e2687e39b166626` | BR_00000006 | 600 / 20 / 620 吨 | 80 吨 |

第二条由来源订单 `0002002073-000010` 的 500/100 吨拆片及 20 吨隔离材料组成。第一条的全部订单与虚拟材料位置保存在 CSV；不从汇总值猜测其组成。

- 最终 22 条链、0 项禁止违规、2 条欠重、总缺口 204.42 吨；六级质量 `(0,0,2,204.42,22,400)`。
- 531 个原订单 / 29333.91 吨完整保留；两次同期间拆分后真实节点 533，加 20 个虚拟节点共 553 个位置。每个原来源重量检查通过。
- 候选检查 100000、完整候选评价 1039、接受 41、同期间拆单 2、未来借入归还拆单 0。停止于数量额度，不是 180 秒时间限。
- 固定 LF 导出后的当前服务耗时 64.759769167 秒；入口至第二层自检完成 64.480371541 秒。本次没有独立进程配对采样，不作为正式性能门槛通过的证据；初次运行 66.1131 秒仅保留为下文换行修复前历史。
- 搜索边缓存命中 860895 次、未命中 180302 次、记录 180302 条，命中率约 82.68%。统计范围为该次构图及搜索共用缓存；只旁观构造出的原对象，不改变热路径或用缓存代替最终审计。
- 公开结果身份 `2dbd6043de68462477b0927db63f5797a7bce6a395bfa016571daee5d368a391`，完整接受轨迹身份 `11f3ce5755b71fd71631c2ffec506fa04b9ed2241cf448d1e69f82e0ba794585`，与功能 20 完全一致；只增加开发证据导出，未改变解。

### 产物与回读

[逐链明细](run_shared/chain_detail.csv)、[逐位置明细](run_shared/schedule_detail.csv)、[531 个来源重量核对](run_shared/source_conservation.csv)、[完整公开结果](run_shared/public_result.canonical.json)、[41 次接受轨迹](run_shared/accepted_trace.canonical.json)、[质量报告](run_shared/quality_report.json)。

已独立回读六项产物：22 / 553 / 531 行数、531 项逐源精确重量、总重 29333.91、两条缺口合计 204.42、结果/轨迹身份及报告列出的五项产物字节哈希全部通过。完整公开结果包含诊断快照、双审计和五类资源事实；使用既有 `canonical_json` 编码，不新增结果反序列化框架。运行清单的开发构建标识仍为 `unversioned`，外层工具核验生产、测试输入映射及基线共 44 个文件与明确的功能 20 提交一致，并前后核验 10 项参考文件身份。

| 当前脚本 / 测试 / 报告 | SHA-256 |
|---|---|
| `docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/run_shared/quality_report.json` | `93de428ad3698edb92ef5bfb7b2279e53a03e3e0d02fabac51dabd7e5b6a05c6` |
| `docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py` | `a215acecdc06cd686ae1f9c679913c0da137efe1311829e5d15aa36064c2a5e6` |
| `tests/integration/test_quality_precheck.py` | `9dfa181c20cabd6faaa253fbe544b58d64ec5a31cdd5be0245c54669ea083927` |
| `tests/integration/test_reference_baseline_identity.py` | `535b81a3f1ed6ed08239585021d316d2dd638d7e5243850dd9be6f750faf6143` |

预检专用 20 项（0.56 秒）、103 文件静态/格式、残留保护、原参考身份工具和冻结阶段产物比较均退出 0；较早 17/19 项预检测试仅为中途检查，最终使用 20 项。原参考工具此次只回读，不重新求解。脚本保护已存在目录/最终目标符号链接，但不将 macOS 正常的 `/tmp` 别名作为阻塞；既有旧包残留仍由仓库保护工具核验，不纳入本次新生产包身份。

### 实际命令与提交范围

精确 12 文件：AGENTS、实施计划、两份集成测试、预检脚本、本文、上述六项真实产物。不会提交旧 V6、外部参考目录、IDE 文件或无关设计资产。本次为元数据修复与质量预检证据单元，**不是功能 21 最终验收提交**。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/integration/test_quality_precheck.py -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/capture_solverpy_reference.py --verify-only
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/compare_solver_stages.py --reference-only
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py --output-dir docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/run_shared --code-revision f9900b949845a54582a6b8813d4306f968ee5db0 --code-repository /Users/miles/dev/dev-py/APSGOV6
git diff --check
```

真实预检命令预期返回 2，报告仍必须是上述真实质量失败而非内部异常；回归与静态命令预期为 0。暂存树将导出至新目录重复同范围检查及预检（输出使用另一新目录），并执行编译和 CSV/JSON 回读；不得复用已有输出目录掩盖失败。共享产物保留本次真实字节，导出复测只比较稳定语义与 CSV/轨迹，不要求观测耗时相同。

## 尚未完成

- 不能将本次失败预检标为完整验收；完整阶段 A/B、多产线、正式三次确定性、20 对性能和质量达标仍未收口。
- 若扩大数量额度作诊断，须单独明确授权及证据，不修改当前冻结配置，不用该诊断替代同预算验收，也不能预先断言必然归零。

## 修正换行后的干净导出复测

暂存树 `1ebcd9c64a8968cc5c3fb50afb735f0df0128d0f` 导出至 `/tmp/apsgo-quality-initial-VkEBPM`。先回读导出树中已存产物，五项字节哈希及 22/553/531 行明细均通过，再运行前述同范围命令。

| 范围 | 干净导出实际结果 |
|---|---|
| 仓内累计 | 2567 通过 / 187.37 秒 |
| 参考身份 | 33 通过 / 0.29 秒 |
| 预检专项 | 20 通过 / 0.58 秒 |
| 103 文件静态/格式、残留、参考身份工具、冻结阶段产物回读、编译 | 全部退出 0 |
| 真实质量预检 | 退出 2；仍仅零欠重与不允许偏差两项失败 |
| 新产物哈希、逐来源重量、行数和稳定语义比较 | 全部退出 0 |

导出树真实运行的新输出位于 `/tmp/apsgo-quality-initial-results-KiSjB1/run`，服务耗时 65.152257875 秒、入口至第二层自检 64.877599417 秒。除观测耗时与包含耗时的完整公开结果文件字节哈希外，报告其余字段全部逐项相同；接受轨迹及三份 CSV 字节完全一致。输出目录独立，未覆盖共享树证据。

编译仅在干净导出中执行：

```bash
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py
```

以上是已实际完成的初次有效导出验证，不把最初 CRLF 哈希失败的树计为通过。补入此记录后，最终暂存树仍须重新导出、运行相同范围测试与真实预检、回读新旧产物并编译，且提交树必须与该最终验证树一致；最终执行记录随 Git 提交信息保存，避免在提交内容中自引用。

## 初次暂存导出发现的换行问题

首个暂存树 `1c65cee022d279bbb15644971cc485e728fa5a55` 导出至 `/tmp/apsgo-quality-initial-nVhtRw` 后，三份 CSV 被 Git 的文本换行转换改成 LF，与报告中记录的运行时 CRLF 字节哈希不符；两个 JSON 产物哈希仍一致。该树不通过证据回读，不作为最终验证树，尚未在其上执行后续累计回归。

修复仅为 `csv.DictWriter(..., lineterminator="\n")`，并新增中文、逗号、引号的 CSV 往返及 UTF-8 无 BOM / LF 回归。不修改 Git 属性、冻结参考 CSV 或求解逻辑。初次真实输出整体移动保存至 `/tmp/apsgo-quality-crlf-evidence-JBitvN/run_shared`，未删除；当前同名输出由新真实运行重新生成，不能手改报告哈希掩盖差异。
