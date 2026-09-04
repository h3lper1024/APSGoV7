# 功能 7：完整方案评价与六级质量

- 实施前提交：`e49ad4256dde27a4e898506c8cb580b93fcac960`，分支 `codex/solverpy-path-cover-clean`。
- 平台：Darwin 27.0.0 arm64、zsh；Conda `apsgo_v6_3.10.18` / Python 3.10.18。
- 权威设计 v0.13，SHA256 `7c7f1985b48cc3f8de19931eb179fe96d21ae34844067dc909ce5cf1712c1891`；用户确认逐链保留 3、5，全局取 5，累计指标求和。
- 本项共 18 个文件：8 个生产文件、6 个测试文件、设计/计划/AGENTS 和本证据。不修改原始五输入、冻结规则/策略、质量与性能门槛、外部 V3 或参考脚本。

## 完成边界

完整评价为纯函数；按冻结配置顺序交织链规则与每条相邻边规则，包含同一宽度规则派生的真实端点检查。NODE 贡献及 PLAN 贡献分别收集，ACTION_ELIGIBILITY 不执行。直接规则的未读字段不加新限制；完整评价总是提供真实临时资源视图。

每链摘要/指标独立保留，规则显式声明累计或最大值。全局报告的精确原值不由质量聚合覆盖：NAMED_VALUE、COUNT、SUM、MAXIMUM 可独立使用。禁止数量、严重度、链数分别保留逐记录/逐链贡献；虚拟和借用重量先精确求和。四类连续段跨链取最大值，不跨链拼接。

六级评分保持冻结顺序，欠重逐链两位半偶舍入；严重度按参考顺序浮点累加后六位，虚拟重量精确汇总后六位。唯一接受定义是质量元组严格小于；同一输入重分链只减少借用仍同分拒绝，合并减少链数即使增加借用仍接受。没有搜索循环、概率接受、候选池或新台账。

搜索状态、核心候选/释放和公开释放的评价字段接通唯一 PlanEvaluation 类型，不提前执行最终审计。精确有符号求和复用原重量累加算法，sum_weights 保留非负校验。临时资源视图按实际排产期和全部真实角色计算，虚拟关联不凭空创造拆分分区。

## 共享树验证

| 范围 | 结果 |
|---|---|
| 三份完整评价/质量/数值专项 | 73 项，0.14 秒 |
| 加规则集、领域、公开结果接线 | 241 项，0.30 秒 |
| 仓内累计 | 1494 项，1.72 秒 |
| Ruff / 格式 | 56 文件通过 |
| 残留与差异保护 | 通过，16 个稳定残留；IDE 不跟踪、不导出 |

上述退出码均 0。独立审查提出的结构贡献序列问题已修正并增加回归；其他独立检查无未关闭实质缺陷。

```bash
# specific
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_plan_evaluation.py tests/core/test_quality_key.py tests/core/test_reference_numeric_projection.py -q
# focus
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_plan_evaluation.py tests/core/test_quality_key.py tests/core/test_reference_numeric_projection.py tests/core/rules/test_process_rule_set.py tests/core/test_model_contracts.py tests/api/test_result_status_matrix.py -q
# repo
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
# static
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
git diff --check
# guard
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

## 参考对照：数值入口

只读执行冻结源码的完整 evaluate_plan 共 13 次：1 个交织顺序金样、9 个欠重边界、3 个二进制舍入。源文件执行前后 SHA 均为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。不修改或替换参考函数，不执行搜索。

- 顺序严重度 [1e16, 1, 1, 2] 的参考评分为 10000000000000002，精确报告总和为 10000000000000004；错误“先所有链规则再所有边”会得到后者，永久金样可暴露该错误。
- 参考第四项依次 0.0298、0.005、0.015、0.025、0.000001、0、0、0、0；目标前五例分别 0.02、0、0.02、0.02、0，其余不变。两位差异由用户确认，不宣称与原始七级所有字段相同。
- 三个二进制半位分别得到 1.234566、1.234568、1.234568，目标一致。
- 另外永久测试覆盖缺口 9.999/99.999/699.999 进位、低上下文精度与 Inexact 陷阱、有限值及投影溢出、无违规/允许偏差隔离。

以下命令从仓根或干净导出根执行，退出 0：

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import hashlib,json,runpy
from pathlib import Path
from dataclasses import replace
from decimal import Decimal as D
p=Path(json.loads(Path("tests/baselines/gqga4/reference_manifest.json").read_text())["script"]["path"])
expected="87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
assert hashlib.sha256(p.read_bytes()).hexdigest()==expected
r=runpy.run_path(str(p),run_name="reference_numeric_projection_probe")
base=r["parse_rule_book"]({}, {"allow_consecutive_reverse_width":True,"reverse_width_carrier_grades":["*"]}, {"period_order":["period"]})
def n(i,w,kind="soft"):
 return replace(r["_sentinel_node"](),node_id="n"+str(i),source_order_id="o"+str(i),source_period="period",weight=D(w),surface_grade="FC",soft_hard_class=kind,material_role="normal_real",node_type="real")
ids=("chain_weight_range","gqga4_soft_hard_connection","chain_high_surface_run_count_lte")
rules=replace(base,enabled={k:{} for k in ids},min_weight=D(0),max_weight=D("2e16"),target_weight=D("2e16"),high_surface_grades=frozenset({"FC"}),max_high_surface_count=1)
value=r["evaluate_plan"]([r["Chain"]([n(i,"1e16",k) for i,k in enumerate(("soft","hard","soft"))],"period")],rules)
assert value.quality[:2]==(4,10000000000000002.0)
assert tuple(v["severity_value"] for v in value.violations)==(1e16,1.0,1.0,2.0)
rows=[{"case":"interleaved_chain_edges","quality":value.quality,"severity_order":[v["severity_value"] for v in value.violations]}]
rules=replace(base,enabled={"chain_weight_range":{}},min_weight=D(700),max_weight=D(2000),target_weight=D(2000))
for weights,score in [(("699.9851","699.9851"),0.0298),(("699.995",),0.005),(("699.985",),0.015),(("699.975",),0.025),(("699.99999851",),0.000001),(("699.999999",),0.0),(("699.9999991",),0.0),(("700",),0.0),(("701",),0.0)]:
 value=r["evaluate_plan"]([r["Chain"]([n(i,w)],"period") for i,w in enumerate(weights)],rules)
 assert value.quality[3]==score,(weights,value.quality)
 rows.append({"case":"underweight","weights":weights,"reference_quality":value.quality})
for raw,score in [("1.2345665",1.234566),("1.2345675",1.234568),("1.2345685",1.234568)]:
 rules=replace(base,enabled={"chain_weight_range":{}},min_weight=D(0),max_weight=D(1000),target_weight=D(1000))
 value=r["evaluate_plan"]([r["Chain"]([n(0,str(D(1000)+D(raw)))],"period")],rules)
 assert value.quality[1]==score
 rows.append({"case":"binary_float_rounding","raw":raw,"reference_quality":value.quality})
assert hashlib.sha256(p.read_bytes()).hexdigest()==expected
print(json.dumps({"source_sha256":expected,"actual_evaluate_plan_calls":len(rows),"cases":rows,"unexpected_differences":0},ensure_ascii=False))'
```

## GQGA4 531 单：同一固定结构的完整评价

仅将原始输入按固定结构分组用于评价对照，不求解、不作为最终排程结果。输入指纹仍为 `cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea`；规则指纹仍为 `420cd13d59763c140b23664f0cb0aca0437e0680b51899d2e0fb39563b7f5365`。

| 固定结构 | 目标六级 | 原始参考七级 | 已解释差异 |
|---|---|---|---|
| 531 条单节点链 | (2,170.3,531,342366.09,531,0) | (2,170.3,531,342366.09,531,0,0) | 前六一致 |
| 原序相邻两节点一组，共 266 链 | (328,2088.45,263,157206.51,266,0) | (415,2175.45,263,157206.51,266,0,374) | 已取消的承载牌号限制多报 87 条、严重度合计 87；借用 374 只统计 |

参考 pair 结构规则计数：逆宽 198（目标 111），厚度 129、温度 80、软硬 6、窄材 2 均相同。首例为 `0030120727-000030` 1227/SPHC → `0030121501-000010` 1330/H260Y；参考在 solver.py:983–998 额外报告承载牌号不允许，严重度 1。目标仍保留逆宽幅度超限 4.15、厚度 6.5 和温度 7.5，并非放过物理规则。

设计 11.8.1 及预期差异表已明确移除承载牌号白名单。本次对原始参考报告逐条分类后，87 条恰为该原因，其余每链禁止规则 ID、发出顺序和严重度都一致；不改写参考原始输出来声称完全一致。独立复核对 531 个节点宽度/热轧牌号逐项核验一致，另外只在内存放开该参考承载集合时前六完全一致；这是诊断探针，不改变冻结配置或基线。

共享一次纯评价分别约 0.051100 / 0.032925 秒，仅表示这两个固定结构的评价调用，不是总求解或正式 180 秒性能验收。命令退出 0：

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import hashlib,json,runpy,sys,time
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0,"src")
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.model import Chain,SchedulePlan
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.core.evaluation import evaluate_plan
base=Path("tests/baselines/gqga4")
manifest=json.loads((base/"reference_manifest.json").read_text())
p=Path(manifest["script"]["path"])
assert hashlib.sha256(p.read_bytes()).hexdigest()==manifest["script"]["sha256"]
r=runpy.run_path(str(p),run_name="reference_function7_full_input_probe")
data={name:r["decode_contract"](json.loads((base/"inputs"/name).read_text())) for name in ("optimization_problem.json","resolved_rules.json","rule_context.json","solver_config.json")}
ref_rules=r["parse_rule_book"](data["resolved_rules.json"],data["rule_context.json"],data["solver_config.json"])
ref_nodes=r["normalize_nodes"](data["optimization_problem.json"],ref_rules)
t=runpy.run_path("tests/app/test_input_normalizer.py")
spec=t["gqga4_spec"].__wrapped__()
problem=t["normalize"](t["gqga4_request"].__wrapped__(spec))
rules=load_rule_set(spec)
context=RuleEvaluationContext(problem.period_order,dict(zip(problem.period_order,range(len(problem.period_order)))),tuple(p.prototype_id for p in problem.virtual_prototypes))
assert [n.node_id for n in ref_nodes]==[n.node_id for n in problem.nodes]
rows=[]
for size in (1,2):
 chunks=[problem.nodes[i:i+size] for i in range(0,len(problem.nodes),size)]
 plan=SchedulePlan(tuple(Chain("c"+str(i),chunk,min((n.source_period for n in chunk),key=context.period_index.get)) for i,chunk in enumerate(chunks)))
 ref_plan=[r["Chain"](ref_nodes[i:i+size],min((n.source_period for n in ref_nodes[i:i+size]),key=context.period_index.get)) for i in range(0,len(ref_nodes),size)]
 started=time.perf_counter(); observed=evaluate_plan(plan,rules,context); elapsed=time.perf_counter()-started
 expected=r["evaluate_plan"](ref_plan,ref_rules)
 reference_quality=tuple(D(str(x)) for x in expected.quality[:6])
 carriers=[v for v in expected.violations if v["reason"].startswith("reverse-width carrier grade ")]
 assert len(carriers)==(0 if size==1 else 87)
 assert all(v["severity_value"]==1.0 and v.get("prohibited") for v in carriers)
 assert observed.quality_key[:2]==(reference_quality[0]-len(carriers),reference_quality[1]-len(carriers))
 assert observed.quality_key[2:]==reference_quality[2:]
 from collections import Counter
 target_counts=Counter(v.rule_id for v in observed.violations if v.disposition.value=="prohibited")
 reference_counts=Counter(v["rule_id"] for v in expected.violations if v.get("prohibited") and not v["reason"].startswith("reverse-width carrier grade "))
 assert target_counts==reference_counts
 for actual_chain,expected_chain in zip(observed.chain_evaluations,expected.chain_evaluations):
  actual=[(v.rule_id,float(v.severity)) for v in actual_chain.violations if v.disposition.value=="prohibited"]
  reference=[(v["rule_id"],v["severity_value"]) for v in expected_chain.violations if v.get("prohibited") and not v["reason"].startswith("reverse-width carrier grade ")]
  assert actual==reference,(actual,reference)
 rows.append({"layout":"singletons" if size==1 else "adjacent_pairs","orders":len(problem.nodes),"chains":len(plan.chains),"target_quality":observed.quality_key,"reference_quality":expected.quality,"target_evaluation_seconds":elapsed,"removed_carrier_records":len(carriers),"first_removed_carrier":carriers[0] if carriers else None,"raw_borrowed_weight":observed.metrics["borrowed_future_weight"]})
assert hashlib.sha256(p.read_bytes()).hexdigest()==manifest["script"]["sha256"]
print(json.dumps({"actual_reference_evaluations":len(rows),"input_fingerprint":problem.input_fingerprint,"rule_fingerprint":rules.fingerprint,"rows":rows,"search_executed":False},default=str))'
```

## 开发期问题及修正

- 初版把结构严重度作为单个总和放入通用聚合，COUNT/MAXIMUM 会误读；已改为逐违规贡献，并覆盖零违规、数量 COUNT、严重度最大值、每链单位贡献。
- 欠重量化精度需要留进位；已按量化指数及额外进位位数计算，不使用会误中架构基准常量保护的硬编码 3，9.999 等真实链案例通过。门禁未改。
- 新测试一度把既有虚拟连续指标名误写成 max_consecutive_virtual_count，已按权威 max_consecutive_virtual_sphc 修正测试，未改原规则字段。
- 首次真实 pair 对照误断言六项完全一致，退出 1；随后定位为上述已登记的 87 条承载限制差异，按原始报告逐条分类闭环。没有以放宽门槛或修改规则消除差异。
- 首轮本地格式检查的未用导入/分号已修正；不影响既有业务逻辑。

## 干净导出与提交

初验树 `e00789a34cd2dbdcee420a69cf4ddaba0f5cc6f9`，导出 `/tmp/apsgo-evaluation-initial-s6Xgp9`。专项 73 项（0.13 秒）、聚焦 241 项（0.29 秒）、累计 1494 项（1.69 秒）均通过；56 文件静态/格式与干净导出残留保护通过。上述两组参考命令复跑退出 0，13 次数值入口及 2 次真实全输入入口结论、指纹与共享树一致；纯评价分别约 0.055698 / 0.035772 秒，仍不是完整求解性能验收。

仅在导出执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools`，退出 0。`git diff --check` 在共享 Git 工作区检查，不在无 Git 元数据的导出目录执行；其他测试集合、静态和参考命令保持相同。证据首次暂存检查曾发现文件尾多余空行，已修正并重新生成本初验树，未跳过差异检查。

补齐本记录后重新导出最终暂存树，按相同集合复测，提交前再检查共享树残留及精确白名单，最后核对提交树身份。提交标识由 Git 历史记录，本文件不自引用本次提交 SHA。

本功能完成后进入功能 8 共享预算与取消状态；主搜索和正式 GQGA4 验收仍未实施。
