# 功能 5.6：原订单延后计划期规则验证

## 实施范围

- 实施前提交：`6901e7ab49fa3807991355895c5a39a4d3eb86e4`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`，Python 3.10.18；Ruff 0.12.0。
- 修改两个生产文件：`core/rules/concrete.py` 新增直接继承 Rule 的 `LateOriginalPeriodMoveRule`；`app/rule_set_loader.py` 增加唯一注册项。
- 新增一个专用测试文件、本目录的差分脚本与本记录；只同步实施计划和 AGENTS。目标设计 v0.8、公共契约、已有规则、资源视图、工程配置、工具、冻结输入输出和质量/性能门槛不变。
- 新路径创建前已核验不存在；不覆盖旧残留、不恢复旧 V6 代码。生产规则只依赖已有模型与标准库，外部参考仅通过证据脚本显式加载。

## 规则口径

1. 使用只读任务上下文的计划期顺序，逐链、逐节点稳定检查当前方案实际指定的计划期；不按标识排序、不固定四期、不重新分配链的计划期。
2. 排产期晚于来源期时，每个普通真实节点、实际过渡真实节点和拆单片段各发一条禁止违规，原因 `late_original_due_period_move`、严重度 `Decimal(1)`，主体为唯一 `node_id`。拆片不按来源订单去重。提前及同期不报违规，虚拟节点没有来源期，跳过。
3. 唯一指标 `late_original_due_period_move_count` 是整数节点数；启用且无延后时为 0，停用时违规与指标都为空。
4. 启用参数必须为空，唯一开关是 `enabled`；停用定义仍检查参数通用形状，但不执行业务参数检查。功能 5.19 去掉源配置重复布尔 `params.forbid_late_original_due_period`，不创建第二开关。
5. 未知链排产期或真实节点来源期用带字段定位的 ValueError 拒绝；真实空来源期已由 Node 构造器拒绝。后续功能 6 才将请求问题聚合为输入诊断；`required_fields()` 中的来源期只适用于真实输入，不要求虚拟原型具有来源。
6. 只读取 `PlanRuleSubject.plan`，不依赖或新建资源台账，不修改输入方案、节点或上下文。完整方案评价、资源视图自动计算及最终审计仍待功能 7/18。

## 参考证据与分组边界

参考：`/Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py`；SHA-256 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。冻结规则记录见 `tests/baselines/gqga4/inputs/resolved_rules.json:1128-1153`。

参考 `evaluate_plan():1225-1226` 先调用 `normalize_plan_periods():1209-1215`，将每条链改为其真实节点最早来源期；因此合法期序下，后面的 `assigned_index > source_index` 晚排分支在原始完整入口不会触发。判定与逐节点计数在 `1249-1275`，没有检查规则是否启用；指标在 `1417`。这是“既有设计要求评价不改方案”与参考副作用之间的差异，不能将原始入口的零违规结果当作目标正向金样。

差分脚本明确分组，所有参考失败都核验为本规则，不过滤意外失败；运行前校验脚本 SHA：

| 分组 | 数量 | 结论 |
|---|---:|---|
| 原始完整入口的计划期重分配观察 | 6 | 参考改为来源期，目标保留原分配；差异符合设计 |
| 隔离晚排判定 | 220 | 单链 108、多链 108、可变期数 4，逐节点结果与严重度一致 |
| 停用修正 | 12 | 隔离参考仍报晚排，目标无贡献；符合严格启停设计 |
| 未知期序保护 | 4 | 目标定位字段并拒绝；原始参考可能重写未知排产期，或对未知来源期抛 KeyError |
| 合计 | 242 | 首个未解释差异为 `null` |

隔离时仅在独立 `runpy` 命名空间内临时取消前置改期，运行原评价函数；`finally` 恢复原函数并断言输入未变。没有改参考文件，没有声称隔离结果等于原始完整流程。第一个停用差异：来源 `z-first`、排产 `a-second`，隔离参考输出一条严重度 1 的违规，停用目标输出为空。已拆片的逐片身份、同来源不去重另由专用测试验证。

## 实际验证

共享树退出码均为 0：

- 专用 29 项通过（独立测试 0.07 秒）；聚焦规则/基类/规则集/加载器 243 项通过，0.25 秒。
- 规则累计 553 项通过，0.52 秒；全量累计 812 项通过，1.04 秒，包含此前宽度和连续规则。
- 242 例参考观察/隔离差分/保护案例通过；差分与两项静态检查组合命令外层 1.61 秒，未解释差异为空。
- Ruff 与格式检查覆盖 `src tests tools` 及本差分脚本，共 40 个 Python 文件；差异格式检查通过。
- 开始时残留检查通过：稳定 16、易变 992，未删除；三个既存 Ruff 缓存新增路径保持前次记录，本次均使用 `--no-cache`，不将残留暂存。
- 独立只读复核未发现实质性问题；额外 7128 组任务期序与材料组合无差异。该一次性探测不代替仓库内专用测试和差分脚本。

初验暂存树 `93a0dbc4d37d5d563d397413d2672107a9203403` 导出至 `/tmp/apsgo-late-period-nygvJ4`：聚焦 243 项（0.25 秒）、规则累计 553 项（0.50 秒）、全量 812 项（0.96 秒）通过；242 例参考核验输出与共享树一致，残留报告为 `clean_export`，无新增或删除。差分/残留组合命令外层 2.97 秒；`compileall` 外层 1.26 秒；退出码均为 0。补齐本文及执行记录后须再次验证最终暂存树，不用初验树替代最终提交验证。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_late_original_period_move.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_06_late_original_period_move/reference_differential.py /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_06_late_original_period_move/reference_differential.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_06_late_original_period_move/reference_differential.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --exit-code HEAD -- tests/baselines tools pyproject.toml src/apsgo_scheduler/core/rules/base.py src/apsgo_scheduler/core/rules/rule_set.py src/apsgo_scheduler/core/resource_facts.py docs/design/apsgo_v6_solverpy_rule_driven_path_cover_local_search_detailed_design.md
git diff --check
```

提交前精确暂存本项七文件，用 `git write-tree` / `git archive` 导出到 `mktemp -d` 新目录，执行同一聚焦、规则累计、全量、差分和残留检查；仅在干净导出运行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests`。补齐导出结果后再次导出最终暂存树复测，核验七文件 UTF-8 无 BOM、最终提交树与测试树一致。当前提交 SHA 由 Git 记录，不在内容中自引用。

## 下一项前置核验与未完成边界

功能 5.7 的“未来填充目标”目前无法从文档唯一推导公式：冻结记录 `resolved_rules.json:1155-1175` 只有目标 1200 和恒真断言；参考支持目录保留该规则，但未实现目标指标；设计只要求输出偏差、不进入七级质量键，没有规定比较重量、逐链或全方案范围、仅不足或双向差值。

主代理只读运行互证（Python 标准输入脚本，退出 0、外层 1.23 秒）：同一参考方案有来源期首期 400 吨及未来期 800 吨两个节点；依次设置目标 0、1200、5000 和停用，四份完整评价相同，质量键均为 `(0, 0, 0, 0.0, 1, 0.0, 800.0)`，没有填充目标指标。独立复核得到相同结论。源代码还没有该字段的实际计算，不能把 1200 值当作公式。

已异步询问用户目标的业务含义，确认前不实施 5.7、不跳过串行计划。已有 `EvaluationResourceView` 值类型在 `core/resource_facts.py:223`，并非缺少第二套台账；只是自动派生及主体类型接线仍归功能 7。不得擅自套用 `abs(borrowed_future_weight - target)` 或从历史填充策略猜测单链补重。

本项没有运行 GQGA4 主搜索、最终审计或质量/性能验收；正式链数、欠重、禁止违规和 180 秒门槛均不变。
