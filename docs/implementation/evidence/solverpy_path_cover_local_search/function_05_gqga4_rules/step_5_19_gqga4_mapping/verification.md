# 功能 5.19 GQGA4 完整规则与策略样本验证

- 实施前 `04152afa5307453744c12615ad1175ae318a000b`；Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 仅六文件：两份新 JSON、映射测试、本文、实施计划及 AGENTS。生产代码、原始五份输入和正式门槛不修改。
- 目标不是改写原始快照：保留原 17 条中的 15 条原身份及启停，排除借用比例和牌号策略，新增独立拆单，共 16 条配置、15 启用、1 停用。
- 配置规则集指纹 `420cd13d59763c140b23664f0cb0aca0437e0680b51899d2e0fb39563b7f5365`；策略指纹 `6ddfb11e815f79273b0e510e7a882e21a799a540af603fb5d098c7e289e7e14e`。

样本为既有类型的直接字段表达；Decimal 用小数数字、整数仍用整数，消费者按 `parse_float=Decimal` 读回后构造既有类型。没有引入编码器、通用适配器或新的规则层级。

| 检查 | 共享树 | 干净导出 |
|---|---|---|
| 映射专用 | 61 项，0.18 秒，退出 0 | 61 项，0.16 秒，退出 0 |
| 规则与映射累计 | 1028 项，1.12 秒，退出 0 | 1028 项，1.09 秒，退出 0 |
| 仓内累计 | 1267 项，1.42 秒，退出 0 | 1267 项，1.38 秒，退出 0 |

初验导出 `/tmp/apsgo-mapping-71sa2t`，树 `16b8725561eae56fcd9e1db1c15dd61b409800b7`；同集合、静态、残留及编译全部退出 0。补齐记录后最终暂存树按同集合复测，通过才提交并核对提交树身份。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/app/test_gqga4_rule_set_mapping.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/app/test_gqga4_rule_set_mapping.py tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
```

原输入 `optimization_problem.json` 的 `summary.rule_domain=month/GQGA4/default/rolling_strict` 和原规则源信息支持本样本 `GQGA4/default/month` 身份，不声称原 resolved 顶层有 process_code 字段。新版本 1 与原数据库版本 59 分开，原短配置指纹不得代替新完整指纹。

规则参数逐项依据原 `resolved_rules.rule_snapshot.dsl_rules`；虚拟宽度 200 来自 `evaluation.virtual_sphc_width_tolerance`，温度自适应来自 `rule_context.virtual_sphc_temperature_adaptive`，拆单限额来自原 `solver_config.historical_solver_policy.repair_policy.rolling_final_cross_period_repair`。拆单和连续窄钢分别取得同一来源 500；两种拆单模式为用户已确认扩展。

参考 `evaluate_chain()` 的真实顺序先链重、逆宽次数、连续虚拟，再整批相邻宽/厚/温/软硬、跨虚拟真实端点、高表面、窄钢、同规格。目标配置依此冻结相对顺序，不沿用原 DSL 的排列。节点构造优先随后列出，三条方案规则及动作资格分属其作用域。作用域索引数量为节点 1、相邻 4、链 7、方案 3、动作 1；多出的内部宽度链检查没有公开配置身份。完整跨作用域交织仍在功能 7，不能用本项索引检查替代完整评价顺序测试。

六项聚合为 `named_value/sum/sum/sum/named_value/named_value`，第二/六项参考六位浮点投影、第四项逐链两位再求和；允许偏差使用真实原因码 `chain_weight_below_minimum`，不是参考展示名 `underweight`。原七级质量档案不改，借用、目标重量、填充缺口及客户优先不加入目标接受键。

策略种子 590531、候选 100000 保持参考参数；40 吨链对扫描松弛与最多双节点桥有源算法依据。目标总时间 180 秒来自已确认门槛，10 秒收尾预留只是显式初始工程配置，尚未验证其充足性。参考 30 秒是搜索参数，不是本项目完整外层时间门槛；后续仍须实跑 20 次中位数/第 95 百分位并保留固定门槛。

逐个停用规则的加载试验使用只含两个固定禁止指标的测试规格；另测完整六项关闭链重会因欠重指标失去生产者而拒绝。这不改变正式 JSON，也不是搜索开关试验。

两个样本 UTF-8 无 BOM，规则 JSON SHA256 `7c3f7ae0af00c0e3669657990bce60d505f8eb760820c6a9f47055f82d65bf16`，策略 JSON SHA256 `afe1d9f4981f7aa443293b60091ba2b6aa78034195df3e5af96209d6ce21541a`。实际加载回读通过，共享静态检查/格式检查通过（49 文件）；最终精确暂存并双树复测后提交。

开发期首轮 57 通过/4 失败是新测试误用决策字段 `split_mode/target_period`，按已有 `mode/target_assigned_period` 修正后 61 项通过，没有生产故障。复核把错误的取消类型名 `GradeConnectionPolicyRule` 改为实际 `GradeConnectionRule`，保证守卫检查真实取消项；没有新增类或改变正式配置。独立复核样本实际加载、重算指纹、1028/1267 项通过；源码、原始五输入及质量/性能门槛无差异。

不运行主搜索、性能或 V3 清单；功能 5 完成不代表 GQGA4 求解验收。提交后继续功能 6，必要回退仅恢复本项新增样本和记录，不改原始基线。

## 提交前追加检查：暂停，尚未提交

最终测试导出 `/tmp/apsgo-mapping-final-QvgQzu`，暂存树 `d4fafa48d1b9782b7f673597dd4ca4c57ff61d96`：专用 61 项（0.17 秒）、规则与映射 1028 项（1.09 秒）、仓内累计 1267 项（1.38 秒）通过；静态/格式 49 文件、干净导出残留及编译均退出 0。

随后在共享树运行 `PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify` 退出 1，重复核验仍然失败，唯一错误为 `changed_stable_residual: .idea/APSGOV6.iml`。原清单摘要 `f4a9682dcb00f75d91c7d625ec1280409d5a11b09cd321ab72f1fc4c0ea417d6`，当前文件摘要 `418cdc8de79157b6fb090edc0420c01e66295144bc731e92c4f0133e36aac6a7`；`stat` 显示修改时间为本地 2026-09-03 17:14:50、405 字节。只确认字节发生变化，不推断修改者或宣称已定位自动改写原因。

文件为未跟踪的 IDE 项目配置，`git ls-files --error-unmatch .idea/APSGOV6.iml` 退出 1；精确暂存仍只有本项六文件。其他 15 个稳定残留、冻结清单、生产代码、原始五输入及正式门槛未改。四条 Ruff 易变缓存新增路径按既有政策只报告，不是本次失败原因。未修改、恢复、删除或提交任何 IDE 文件，未改保护脚本、忽略规则或冻结清单。

已向用户询问是否允许保留当前 IDE 配置、将本次变化登记为已确认的本地环境变化后继续。确认前不提交 5.19，也不开始功能 6；本节与同步状态是上述最终测试树之后的纯记录修改，后续获准提交仍需重新导出最终暂存树验证。

用户随后明确要求将 `.idea` 目录加入 Git 忽略。据此独立修复本地 IDE 保护范围：允许根 IDE 内容变化，但仍禁止跟踪或导出任何 IDE 文件，原始残留快照与其他文件保护不改；本项不再等待用户确认，修复后复测最终暂存树再提交。首次失败事实保留。

修复提交 `a212d3679c3aa181a6fff645e054d27c45e68509` 已完成，最终导出 1206 项通过，共享保护检查恢复通过；其验证见[IDE 忽略记录](../../function_00_project_boundary/ide_ignore_verification.md)。本配置单元实施期间增加了该独立前置提交，因此最终提交父节点为 `a212d36`，不是最初实施前的 `04152af`。本项源码、61 项测试及两个样本未因 IDE 事件改变；最终六文件暂存树在新父节点上重新导出，按本文同集合复测后提交。
