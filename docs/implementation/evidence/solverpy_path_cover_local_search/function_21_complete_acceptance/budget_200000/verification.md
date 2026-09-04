# 正式候选检查上限 200000：配置修订与质量复测

## 范围和身份

- 日期：2026-09-04；macOS / Darwin arm64，Conda `apsgo_v6_3.10.18`。
- 实施前提交：`4655ea770c1910432763258d347564166b418711`。
- 用户明确确认正式配置采用 200000；不修改搜索顺序、停止语义、生产实现、规则或质量/性能门槛。
- 正式策略指纹：`b74e8ea92660994a71d96cb42f4717ee7e0514206ca0378458003acf51ab99ba`。
- 正式请求指纹：`af40e19498a724e4964ab849c9088cfd6e7bf30c9946ae8bcfea6141c26a4ff3`。
- 问题指纹：`cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea`。
- 规则指纹：`420cd13d59763c140b23664f0cb0aca0437e0680b51899d2e0fb39563b7f5365`。

单订单阶段 `relocation_start` 夹具及其下游填充、拆单、审计历史回放显式读取冻结参考的 100000 次额度，原结果断言不改。整链及完整三邻域测试、正式预检仍读取当前 200000 次策略文件；不泛称全部阶段均为同预算 A/B。预算是最大允许数量，不是必须完成数量；固定邻域及拆后一次搜索完成后自然退出，不实现“首次全部规则符合立即退出”。

提交保护同时按用户确认仅登记两份已提交流程图为项目文档。原始残留清单的 16/992 数量及字节、两份图字节不改；其他残留与 IDE 保护仍生效。

## 验证方法

1. 仓内累计：`PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q`。
2. 专用：同解释器运行映射测试、质量预检测试及 `test_historical_stage_policy_keeps_frozen_candidate_budget`。
3. 参考身份、残留工具 `--self-test` / `--verify`、Ruff 检查和格式、`git diff --check`。
4. 使用既有 `quality_precheck.py` 直接调用唯一应用入口并保存六份完整产物；不替换策略、不伪造时钟、不跳过审计。入口校验的提交由当前暂存树生成只读验证提交对象，分支不移动；报告中的提交对象指明实际代码/配置快照，不冒充实施前提交。
5. 将共享树产物及本记录写入暂存树后，导出最终树，在新目录运行同范围测试和真实预检，逐项回读与对照；只在干净导出执行编译。最终导出命令、退出码、数量、耗时和树身份写入正式 Git 提交信息，避免在提交内容中自引用。

质量门槛仍为最多 22 链、0 欠重、0 禁止违规。本次单次/干净导出复测不替代完整阶段对照、多产线、连续三次确定性和 20 对性能验收。

## 实际记录

| 范围 | 共享树实际结果 | 退出码 |
|---|---|---|
| 仓内累计 | 2568 通过，183.10 秒 | 0 |
| 映射 61 + 预检 21 + 历史隔离 1 + 参考身份 33 | 合计 116 通过，1.55 秒 | 0 |
| 103 文件 Ruff 检查/格式、残留自检/核验、原始参考工具及冻结阶段产物比较 | 全部通过 | 0 |
| 新正式配置真实质量预检 | 22 链、0 欠重、0 禁止违规；双审计、来源守恒通过 | 0 |
| 产物逐字节回读和逐源重量复算 | 22 / 553 / 531 行；前 41 次接受与旧证据相同；完整轨迹及三份 CSV 与授权诊断相同 | 0 |

正式预检的验证提交对象为 `2e8ccc5bdd4382fd7487f3b5e8581145ec5b505d`，对应暂存树 `efa4e6cfcda77cf913aa643ef041cacd3a632b92`；当时分支 HEAD 仍为实施前 `4655ea7`，未伪称已创建正式提交。预检前后校验 44 个生产/输入映射/基线文件和 10 项参考身份。该临时验证对象不成为正式分支历史；可持久追溯的内容证据是报告内逐文件哈希、下述共享产物和本记录所在的正式提交树。

服务耗时 **65.971286375 秒**；入口至第二层自检 **65.702121375 秒**。本次与累计回归并行，仅用于质量与正确性观察，不能解释为独占条件性能样本或性能变快的证明。实际候选检查 **102475**、完整候选评价 **1061**、接受 **46**、同期间拆单 **2**；停止原因 `local_search_complete`（本轮搜索自然完成），不是用满上限，也不证明全局最优。

六级质量 `(0,0,0,0,22,400)`；531 个来源订单共 29333.91 吨完全守恒，533 个真实节点加 20 个虚拟节点。前 41 次接受与旧 100000 证据完全相同；新增第 102465、102466、102468、102471、102475 次检查的五次真实订单移动，分别将原两条欠重链由 575.58 提高至 719.07、由 620 提高至 786 吨。不增加虚拟重量、不修改链数门槛。

### 文件与身份回读

- [质量报告](run_shared/quality_report.json)：SHA-256 `9cdb82d38541fd380860c201d8629e8476ef6f56e7ea81e3d3ad5e4112af5dfe`。
- [完整公开结果](run_shared/public_result.canonical.json)、[接受轨迹](run_shared/accepted_trace.canonical.json)、[链明细](run_shared/chain_detail.csv)、[位置明细](run_shared/schedule_detail.csv)、[来源重量核对](run_shared/source_conservation.csv)：均按报告所列字节哈希回读通过；轨迹身份 `30c90a84c4396537b4aa0fd9bbc439dbbd092d41c818352b8455b2a7931c4f09`。
- 当前策略文件 SHA-256 `c7ca656feae36c690bd253a2ba254a296f00a5401a792ca3b2c1c971c424766d`。
- 质量门槛 SHA-256 `5c6e83148771f0ebc3c71bb970908c63fc53ecb0e9078ec2658bbc1cd39ddee9`、性能门槛 SHA-256 `e6a472ea83288b67e2902fd47c11cf312d174a3cd9803f91f71b8dbbed8209cb`，与实施前一致；设计 v0.14、生产源码、旧预检输出和两张图也与实施前一致。
- 独立复核补齐图文件父目录符号链接负例，指定根目录以内的目录/文件链接均拒绝，根外系统目录别名仍允许；只有两个精确已授权路径提升，不扩展到整个资产目录。

### 本次实际命令

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/app/test_gqga4_rule_set_mapping.py tests/integration/test_quality_precheck.py tests/core/search/test_single_node_reference_trace.py::test_historical_stage_policy_keeps_frozen_candidate_budget tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --self-test
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/capture_solverpy_reference.py --verify-only
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/compare_solver_stages.py --reference-only
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py --output-dir docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/budget_200000/run_shared --code-revision 2e8ccc5bdd4382fd7487f3b5e8581145ec5b505d --code-repository /Users/miles/dev/dev-py/APSGOV6
git diff --check
```

上述输出目录已经存在，复跑须换新目录；后续复跑的 `--code-revision` 使用本记录所在正式提交的完整 SHA。原始 100000 次 `../run_shared` 六项产物不覆盖；本目录完整证据只说明新配置质量复测通过，不宣称功能 21 全部完成。
