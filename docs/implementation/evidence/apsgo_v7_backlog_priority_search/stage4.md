# 阶段 4：旧欠优先九级评分与接受语义

2026-09-14，macOS / Darwin，Conda `aps_3.10.18` / Python 3.10.18。实施前分支 `codex/delivery-objective-optimization@5ce8a39`，工作区干净。

本阶段实现显式新评分、接受保护、报告与工具接线。**新九级尚未进行完整 GQGA4 搜索；固定旧方案的新指标重算，不是新求解结果。** 正式 SQLite / YAML、预算、旧结果及工艺约束不改，不部署、合并或推送。

## 1. 实际修改

| 位置 | 实际行为 |
|---|---|
| `core/delivery_timing.py` | 在已有原订单累计循环中取旧欠最后完成小时最大值；空旧欠为 0，不再次遍历节点，不改原计时及累计负担 |
| `core/rules/concrete.py`、`rules/rule_set.py` | 交期规则启用时仅接收可选布尔参数 `include_backlog_clearance`；新模式额外贡献清空小时，并校验已确认顺序；原空参数仍两项 |
| `app/delivery_request.py` | 新选项默认 `False`；显式 `True` 生成新九级、新规则版本 2 和独立规则集后缀；拒绝重复追加与错误布尔类型 |
| `core/chain_order.py` | 新模式定位清空小时作为首个交期评分；经过允许偏差检查后，取消欠重数量/缺口分别不增加的额外限制，交由完整九级比较 |
| 原完整候选与审计 | 只复用，不修改 `neighborhoods.py`、`width_optimization.py`、核心无缓存审计或应用审计；原目标下标驱动的整链前缀自然缩为两项禁止指标，普通搜索与后置精修均走新评分 |
| `app/delivery_report.py` | 新模式增加清空小时和 `delivery_metric_roles`；评分项和仅统计项均从最终顺序复算核对。原九级返回字段保持；诊断仍不作为发布结果 |
| 三个既有比较/报告工具及原位置明细工具 | 新 CLI 变体 `delivery-backlog-priority`；旧 `delivery` 不变。正式欠重门按指标名称读取，不能将新第 3 项小时误当欠重链数；位置明细复用报告开关 |

新顺序：禁止数、禁止严重度、旧欠全部完成小时、累计等待与延期吨·小时、欠重链数、欠重缺口、链间宽差、虚拟重量、链数。`newly_late_original_weight` 不参与评分、同分裁决或额外否决，但仍贡献统计并审计。没有增加第二种求解流程、依赖或新时间缓存，这是 Ponytail Lite 对本次实现的裁剪。

## 2. 新旧身份及固定方案核对

通过 `tools.run_delivery_comparison.prepare()` 对同一七级输入与工时来源，分别传默认选项和 `include_backlog_clearance=True`。起排为 `2026-06-01T00:00:00+08:00`，虚拟速度 100；仅在内存中准备，没有运行搜索或改写旧文件。

| 身份 | 原九级 | 旧欠优先九级 |
|---|---|---|
| 请求指纹 | `068afa20c9c62008268eb1149a61ead6b679a84a873891b0fd7047ec974a5443` | `4d4b420b2e6e76182cecdeaf5040004b9aa7e14b895472030f5fa3caa940275f` |
| 规则集指纹 | `294a23dca9bdf0443aadd57fa03f8ad802aa3ea6aca79f329943c8995f28facb` | `160a764f3e5444e47ce74d689c4fb99d1c76246e3fa1f74ba5ea755fccdffede` |
| 订单/时长/起排/速度/策略 | 与冻结请求逐值相同 | 与原九级逐值相同 |

使用 `tools.compare_backlog_search_runs.read_run()` 回读原长时限结果和阶段 3 主结果：完整评价指纹、发布与最终方案/计数、531 原单日期及守恒一致；默认准备结果等于两份冻结类型请求。原结果分别仍为 BEST_EFFORT / PASS，不修改历史状态。

仅对阶段 3 最终方案使用新规则重新完整评价并执行 `audit_core_without_search_cache()`：通过；新投影为 `(0,0,587.4388867756726549279204323,881989.3561402928129467713230,0,0,14348,1400,24)`。小时最大值等于原旧欠最后完成时刻的相对小时；仍是原来的 24 条链，包含原有受控拆片。该数值只是下一阶段的偏好对照起点，不是新评分优化后的成绩。

## 3. 测试与失败记录

新增两份测试复用现有 pytest 与领域模型，不增加框架。最终新专项 **50 项 / 0.68 秒，退出 0**：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider \
  tests/core/test_backlog_priority_objective.py tests/app/test_backlog_priority_preparation.py -q
```

覆盖：空/全旧欠、非零时分秒、截止等于起排、最后拆片、虚拟占时、最大值区别于累计值；新旧有序键/类型/身份、重复追加拒绝；普通完整候选、纯整链和后置精修；仅统计的晚交不影响评分；禁止、来源及编号保护；无缓存审计、公共结果绑定、报告复算和两种 CLI 小型实际求解；欠重按名称验收。

真实节点移动样例：清空 `4→1` 小时、累计负担 `400→800`、欠重 `0→1 条/100 吨`、晚交吨位 `0→700`。新模式按已确认优先级接受且核心审计通过，旧模式拒绝；该数值来自 4 单测试，不是 GQGA4。另测清空不变时累计负担改善、前四项相同时欠重数量优先于缺口，以及全部九级同分时不能靠统计破同分。

首次应用专项为 **3 失败 / 18 通过**：构造篡改后的公共结果时，既有公共身份校验已提前拒绝，测试未等到预期的报告检查。修正测试预期，保留构造层拒绝检查，再用明确的报告单元对象检查其独立重算；未删除或放松生产校验。随后 49 项、补充同清空样例后 50 项全部通过。

相关集中检查命令（最终共享树和精确暂存树导出使用同一范围）：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider \
  tests/architecture tests/api \
  tests/core/test_delivery_objective.py tests/core/test_delivery_timing.py \
  tests/core/test_delivery_search_audit.py tests/core/test_backlog_priority_objective.py \
  tests/core/test_urgent_order_search.py tests/core/search tests/core/audit \
  tests/core/rules/test_process_rule_set.py \
  tests/app/test_backlog_priority_preparation.py tests/app/test_backlog_search_comparison.py \
  tests/app/test_delivery_preparation.py tests/app/test_solver_profile_tool.py \
  tests/app/test_result_contract_audit.py tests/app/test_chain_order_release.py \
  tests/app/test_rule_set_loader.py tests/app/test_input_normalizer.py -q
```

首轮相关检查 **1625 项 / 35.31 秒通过**；补充同清空样例及新报告角色标记后，最终共享树 **1626 项 / 33.41 秒通过，退出 0**。精确暂存树导出使用同范围，实际树身份、数量和耗时记录在本阶段提交正文，避免在提交内容内自引用。不是含服务的全仓累计回归，阶段 6 仍须按计划执行。

## 4. 保护及交接

- 六项输入/保护哈希与原长时限清单相等；原五个结果文件逐字节 SHA-256 不变。阶段 3 产物按自身冻结清单检查，不覆盖旧报告。
- 共享树残留检查仍退出 1，仅报告已知 8 个 V6 旧文件缺失；不恢复残留、不修改旧清单。精确导出检查独立执行，结果随提交正文保留。
- 最终只暂存本项 6 个生产模块、4 个工具、2 份测试、本专项两文档、`AGENTS.md` 和本记录；不提交 `diagnostics/`、IDE、外部 V3 或 C#。
- 下一阶段用 `--variant delivery-backlog-priority`、相同 400000 / 9999+10 预算完整运行，保存新目录，与阶段 3 及原方案按指标名称比较。新评分可能提前清空但增加晚交、欠重或资源消耗，已确认取舍不再被隐藏否决；最终正式质量/性能状态仍单列。
