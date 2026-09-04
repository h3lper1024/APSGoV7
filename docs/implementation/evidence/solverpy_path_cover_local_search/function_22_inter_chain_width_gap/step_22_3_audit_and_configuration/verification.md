# 功能 22.3：有序审计、公开结果与正式七级配置

- 日期：2026-09-04；macOS / Darwin arm64 / zsh。
- 实施前：`091684bfc6c3e311701e8a424c413072ad31cc25`。
- 依据：设计 v0.16，实施计划 v0.59 第 8.23.8 节。
- 本项不执行新七级 GQGA4 完整排程，不改变 200000 次额度、质量或性能门槛，不改旧输出、原始五输入、参考脚本或外部 V3。

## 实际范围与根因

核验复现：三条单节点链 A1000→B1500→C1200 与逆序的总宽差均为 800；旧核心审计按链身份字典比较，调换方案顺序但保留旧搜索评价也能通过。现仅在新规则启用时检查有序链身份，既有逐链/指标/质量比较保留；另记录期序倒置的不变量诊断。审计只核验，不排序，异常继续失败关闭。原无新规则的顺序不敏感测试保留。

直接核心入口原来只从边规则提取原型宽厚要求，本项与应用标准化对齐至全部启用规则。应用组装/结果契约已有有序引用和身份核验，仅补真实调用回归，无需新增排序、边界容器或公开字段。

精确文件范围共 19 文件：

- 生产：`src/apsgo_scheduler/core/final_audit.py`、`src/apsgo_scheduler/core/solver.py`。
- 配置：`tests/baselines/gqga4/gqga4_rule_set_spec.json`、同目录新增 `gqga4_rule_set_spec_six_level_historical.json`。
- 测试：`tests/core/audit/test_chain_order_audit.py`、`tests/app/test_chain_order_release.py`、`tests/core/test_solver_orchestration.py`、`tests/app/test_gqga4_rule_set_mapping.py`、`tests/app/test_input_normalizer.py`、`tests/core/test_plan_evaluation.py`、`tests/core/construction/test_initial_solution_reference_stage.py`、`tests/core/search/test_post_split_single_replay.py`、`tests/integration/test_gqga4_visual_report.py`。
- 工具：`function_21_complete_acceptance/quality_precheck.py` 仅改请求与规则集期待身份；旧 `budget_200000/visualization/generate_report.py` 的默认规则路径指向六级历史快照。
- 文档：目标设计、实施计划、AGENTS 和本文。

## 配置与历史隔离

新配置只追加 PLAN 宽差规则和 `MINIMIZE / SUM / EXACT_DECIMAL` 第七项；原 16 条规则、原六项评分、其他顶层业务字段逐字段及顺序保持。加载后 17 配置/16 启用/连续逆宽 1 停用，目标索引由声明派生为 6，不写进通用核心。

| 身份 | 实际值 |
|---|---|
| 原六级 JSON 与历史快照 SHA256 | `7c3f7ae0af00c0e3669657990bce60d505f8eb760820c6a9f47055f82d65bf16` |
| 新七级 JSON SHA256 | `d0a73350aedbb5c7dff0ed427bae8f90110df092cab32089b3905f6c4c596564` |
| 新规则集指纹 | `cd4e21b37e815c9100edd9f72b0dd0b76bb5d315463719c5e1fdf546ce96deef` |
| 新请求指纹 | `261c1e94e9c23a5d5812ddaed0659828d34907972bec151ed45dbde952d8de76` |
| 问题指纹（不变） | `cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea` |
| 策略指纹（不变） | `b74e8ea92660994a71d96cb42f4717ee7e0514206ca0378458003acf51ab99ba` |

旧映射/完整评价/参考初始及拆后轨迹/旧图表明确读六级快照，数值及顺序期望不重写；当前标准化、边缓存、构造图与质量预检身份仍读正式七级。独立读取 Git 中旧文件并与快照比对相等，实际加载与 531 单规范化重算上述身份成功。

独立复核发现旧图表的命令行默认规则路径仍读正式配置，导致历史重生成报 `Rule configuration hash mismatch`。只修改默认路径并补默认入口回归；旧 HTML、CSV、清单和 README 字节不改。新报告显式传入新规则配置，不能复用旧配置身份。

## 验证命令与结果

共享树与最终暂存树干净导出使用同一集合：

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/audit/test_chain_order_audit.py tests/app/test_chain_order_release.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/audit tests/core/test_solver_orchestration.py tests/app/test_chain_order_release.py tests/app/test_result_contract_audit.py tests/app/test_gqga4_rule_set_mapping.py tests/app/test_input_normalizer.py tests/core/test_plan_evaluation.py tests/core/graph/test_construction_dag.py tests/core/graph/test_edge_decision_cache.py tests/integration/test_quality_precheck.py tests/integration/test_gqga4_visual_report.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
git diff --check
# 仅在干净导出执行
conda run --no-capture-output -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```

共享树新专项 26 项通过（0.54 秒）；补齐旧图表默认入口后，聚焦 484 项通过（36.50 秒），仓内累计 2723 项通过（169.60 秒），退出 0。109 文件静态/格式、四份文档 UTF-8/围栏/60 个本地链接、残留保护及独立复核通过。最终以 `git write-tree` / `git archive` 固定实际树并复验，树、导出路径及最终结果记入本项提交正文。

开发期测试修正均只涉及新夹具：3 个映射案例缺少本模块正式配置夹具注册；5 个公开案例误用了 `result_audit` 而非 `audit_report`；修正后其中 3 个案例还缺少 `PlanRuleSubject` 的显式资源视图参数；新增图表入口断言把原值 `400.0` 误当作文本 `400`，改用 Decimal 数值比较。已按既有契约修正，没有调整生产行为或旧预期掩盖失败。最终未解释差异见本项提交。

下一项 22.4 才进行真实新旧对照、全部相邻边界明细及新图表；本项测试成功不代表零禁止/零欠重/最多 22 链或 20 对性能验收已经通过。
