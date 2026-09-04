# 功能 12：虚拟材料选择与候选私有编号

- 实施前提交：`bf7e28a4052bbaaeb561abae8c3224c1e0c14df3`；分支 `codex/solverpy-path-cover-clean`。
- 当前平台：Darwin 27.0.0 arm64、zsh；Conda `apsgo_v6_3.10.18` / Python 3.10.18。
- 权威设计 v0.14，SHA256 `7cf9a5c7cd2683e2302345435f9bdc4af803f41e2fdc8436fe89ff3b18be2645`；计划 v0.44 第 8.13 节。
- 精确八文件：virtual_material.py、两份虚拟选择/编号测试、compatibility.py、原边缓存测试、计划、AGENTS、本记录。规则、输入、原始参考、冻结阶段和验收门槛不改。

## 实现与边界

只读 VirtualFactory 绑定既有任务缓存和预算，不保存可变编号；共用纯 materialize 与参考 float 平滑度公式，物化保留原型重量、规格、牌号和规则属性，温度使用左右锚点包络。物理投影及派生平滑度必须有限，异常传播。

三种用途保留各自参考选择语义：连接桥先直接边、再单桥、最后双桥，只按平滑度排名，同分保留目录次序；补重只物化调用方指定原型，不预选最佳；隔离即使两端可直连仍强制插入一个材料，按禁止数量、严重度、平滑度、目录顺序排名。隔离选择器不重复实现尚未接线的拆分授权及完整分区审计。

桥的有效数量上限取策略与启用连续虚拟规则的较小值。0 只允许直接边的空桥；停用不留隐藏数量限制。该上限不被提升成填充/隔离的额外硬过滤。原型目录为空自然没有候选，不增加业务开关。

编号从调用方给定的拟提交正整数开始；枚举失败原型复用拟用号，双桥使用连续两号。同一完整候选的多个边界由调用方根据已选材料数推进局部游标；只在既有 SearchState.commit_accepted 时随完整方案提交。原始订单身份与生成前缀冲突时稳定避让，不使用时间、随机数或拒绝次数。取消/时间只探测零计数预算，不返回枚举一半的最佳材料；本项没有实现邻域接受、主搜索或完整拆单。

## 本项实测触发的缓存修复

重复同一 27 原型、同两端节点的单桥选择，不接受任何候选：

| 版本 | 次数 | 语义边条目 | 对象指纹记忆数 | 当前跟踪字节 |
|---|---:|---:|---:|---:|
| 修复前 | 1 | 29 | 29 | 68793 |
| 修复前 | 10 | 29 | 272 | 290478 |
| 修复前 | 100 | 29 | 2702 | 2137316 |
| 修复后共享复验 | 1 | 29 | 29 | 68539 |
| 修复后共享复验 | 10 | 29 | 272 | 303800 |
| 修复后共享复验 | 100 | 29 | 1024 | 1240062 |
| 修复后共享复验 | 200 | 29 | 1024 | 997563 |

原因是旧加速表以对象地址为键并强持有每次物化对象；语义边字典本身没有增长。修正仅用标准库 OrderedDict 将对象指纹加速表限制为最近使用的 1024 个对象，淘汰后按相同字段重新编码，实际边判定缓存、身份和计数不变。新增两项回归验证容量、命中刷新、淘汰后重算及违规主体重新绑定；指纹仍不使用对象地址。

每次结果始终为 p00 单桥，候选计数始终为 0。tracemalloc 字节随运行分配波动，本诊断只证明同语义临时对象不再无限强留，**不证明全流程所有缓存恒定内存**。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python -c 'import json,tracemalloc
from tests.core.search.test_virtual_material_factory import make_factory,prototype
from apsgo_scheduler.core.compatibility import _NODE_FINGERPRINT_MEMO_LIMIT
factory=make_factory(tuple(prototype(f"p{i:02d}") for i in range(27)),allowed_edges={("left","p00"),("p00","right")})
samples=[]
tracemalloc.start()
try:
 for attempt in range(1,201):
  chosen=factory.bridge(*factory.cache.problem.nodes,max_nodes=2,first_sequence=1)
  assert len(chosen)==1 and chosen[0].virtual_lineage.prototype_id=="p00"
  if attempt in (1,10,100,200):
   current,peak=tracemalloc.get_traced_memory()
   samples.append({"attempts":attempt,"semantic_edge_entries":factory.cache.entry_count,"node_memo_entries":len(factory.cache._node_fingerprints),"current_bytes":current,"peak_bytes":peak})
finally:
 tracemalloc.stop()
assert all(row["semantic_edge_entries"]==29 for row in samples)
assert all(row["node_memo_entries"]<=_NODE_FINGERPRINT_MEMO_LIMIT for row in samples)
assert samples[-1]["node_memo_entries"]==samples[-2]["node_memo_entries"]==_NODE_FINGERPRINT_MEMO_LIMIT
assert factory.budget.candidate_check_count==0 and factory.budget.stop_reason is None
print(json.dumps({"internal_memo_capacity":_NODE_FINGERPRINT_MEMO_LIMIT,"samples":samples,"candidate_checks":factory.budget.candidate_check_count,"selected_prototype_unchanged":True}))'
```

## 共享树验证

| 范围 | 实际结果 |
|---|---|
| 虚拟工厂、编号及边缓存专项 | 129 项，0.40 秒 |
| 搜索基础、构造、图、评价及预算聚焦 | 400 项，3.95 秒 |
| 仓内累计 | 1876 项，5.86 秒 |
| Ruff / 格式 | 72 文件通过 |
| 残留 / 差异保护 | 通过，IDE 与旧资产不进入提交 |

以上全部退出 0。129 项包含 38 项材料选择、53 项身份/取消和 38 项边缓存（含本次新增 2 项）。独立源码复核未发现未解决问题。virtual_material.py SHA256 为 a9a6454d7b47c53f0e9a90c6857660c4eb50a67ac9f3c292523c0d6d99f3955e；compatibility.py SHA256 为 228ed741e251cdcc342879fddf58e15ff08e5bab98a5b8644cfdd61d3a8c0df5。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_virtual_material_factory.py tests/core/search/test_virtual_numbering.py tests/core/graph/test_edge_decision_cache.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --check
```

缓存调整后重新执行[功能 11 的原始/冻结/目标完整阶段命令](../function_11_initial_solution/verification.md)：图、匹配路径及 31 条初始链均一致，计划指纹仍为 6d3a206937521f3fac5ca727c05cc561052a63855f081874321fbf0e2a6e0427；构图 2.181586 秒、匹配 0.461301 秒、初始构造 0.182155 秒。该命令原样复用，不把本轮阶段测量称为 20 次全流程性能验收。

## 指定参考工厂对照与已确认差异

只读执行原始脚本，SHA256 执行前后均为 87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318。

- 40 对固定输入、每对桥上限 1 和 2，共 80 次：39 次无桥、8 次直接空桥、28 次单桥、5 次双桥；原型、重量、规格及温度全部一致。
- 40 对输入 × 27 原型，共 1080 次物化和平滑度比较完全一致。
- 6 组隔离探针：3 组原始结果相同；3 组因用户已取消的逆宽承载牌号过滤而不同。
- 原始尝试编号会累加，目标使用稳定拟用号，这是已登记差异；比较结构时不要求原始临时节点 ID 相同。
- 0 桥上限与启停由目标策略契约的单测验证，不冒称原始 bridge(..., 0) 正确执行该限制。

| 首次差异输入 | 原始选择 | 目标选择 | 具体原因 |
|---|---|---|---|
| 0030119959-000010 | virtual_sphc:1250x0.6 | virtual_sphc:1000x0.6 | 原始对 SPHETI-5 增加一项承载牌号禁止；移除此已取消过滤后目标材料更平滑 |
| 0030125207-000110 | virtual_sphc:1500x0.6 | virtual_sphc:1250x0.6 | 原始对 SPHETI-3 增加相同禁止 |
| 0030124683-000010 | virtual_sphc:1500x0.6 | virtual_sphc:1250x0.6 | 原始对 SPHETI-5 增加相同禁止 |

每个可连接原型均执行原始三节点评价，仅移除 rule_id=reverse_width_limit 且主体含 :reverse_carrier: 的已取消记录，逐项比较余下数量、严重度与目标评价；按原始平滑度排序后的选择全部与目标一致。原始脚本/规则/结果从未改写。隔离探针只替换节点显示 ID 和 400/100 吨重量，以检查材料选择，不冒充已授权拆单或实际局部搜索。

首次严格全等对照在输入索引 9 失败，随后追查全部候选的原始违规和排名，找到以上三项已批准差异；正式命令保留精确差异断言，未解释差异为 0，没有通过修改业务断言或门槛掩盖差异。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import hashlib,json,random,runpy,sys,time
from pathlib import Path
sys.path.insert(0,"src")
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache,build_construction_dag
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import fingerprint
base=Path("tests/baselines/gqga4")
manifest=json.loads((base/"reference_manifest.json").read_text())
source=Path(manifest["script"]["path"])
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
r=runpy.run_path(str(source),run_name="reference_graph_probe")
data={name:r["decode_contract"](json.loads((base/"inputs"/name).read_text())) for name in ("optimization_problem.json","resolved_rules.json","rule_context.json","solver_config.json")}
ref_rules=r["parse_rule_book"](data["resolved_rules.json"],data["rule_context.json"],data["solver_config.json"])
ref_nodes=r["normalize_nodes"](data["optimization_problem.json"],ref_rules)
t=runpy.run_path("tests/app/test_input_normalizer.py")
spec=t["gqga4_spec"].__wrapped__()
request=t["gqga4_request"].__wrapped__(spec)
problem=t["normalize"](request)
rules=load_rule_set(spec)
context=RuleEvaluationContext(problem.period_order,dict(zip(problem.period_order,range(len(problem.period_order)))),tuple(p.prototype_id for p in problem.virtual_prototypes))
cache=RuleEdgeDecisionCache(problem,rules,context)
started=time.monotonic()
budget=SolveRuntimeBudget(started,started+120,started+130,0,0,None)
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from apsgo_scheduler.core.virtual_material import VirtualFactory, virtual_smoothness
from apsgo_scheduler.core.model import VirtualPurpose
factory=VirtualFactory(cache,budget)
ref_factory=r["VirtualFactory"](r["load_virtual_prototypes"](data["optimization_problem.json"]),ref_rules)
def target_shape(node):
 return (node.virtual_lineage.prototype_id,str(node.weight),None if node.width is None else float(node.width),None if node.thickness is None else float(node.thickness),None if node.min_temperature is None else float(node.min_temperature),None if node.max_temperature is None else float(node.max_temperature))
def ref_shape(node):
 return (node.virtual_prototype_id,str(node.weight),node.width,node.thickness,node.min_soak_temp,node.max_soak_temp)
counts=Counter()
differences=[]
pairs=[]
for i in range(40):
 left_index=i*29%len(problem.nodes)
 right_index=(i*43+17)%len(problem.nodes)
 if left_index==right_index: continue
 pairs.append((problem.nodes[left_index],problem.nodes[right_index],ref_nodes[left_index],ref_nodes[right_index]))
for index,(left,right,ref_left,ref_right) in enumerate(pairs):
 for cap in (1,2):
  actual=factory.bridge(left,right,max_nodes=cap,first_sequence=1)
  expected=ref_factory.bridge(ref_left,ref_right,max_nodes=cap)
  actual_shape=None if actual is None else tuple(target_shape(n) for n in actual)
  expected_shape=None if expected is None else tuple(ref_shape(n) for n in expected)
  assert actual_shape==expected_shape,(index,cap,left.node_id,right.node_id,actual_shape,expected_shape)
  counts["bridge_none" if actual is None else "bridge_"+str(len(actual))]+=1
  assert budget.stop_reason is None
 for prototype,ref_prototype in zip(problem.virtual_prototypes,ref_factory.prototypes):
  actual=factory.materialize(prototype,left,right,purpose=VirtualPurpose.WEIGHT_FILL,sequence=1)
  expected=ref_factory.materialize(ref_prototype,ref_left,ref_right)
  assert target_shape(actual)==ref_shape(expected),(index,prototype.prototype_id)
  assert virtual_smoothness(left,actual,right)==r["_virtual_smoothness"](ref_left,expected,ref_right)
  counts["materialization"]+=1
# Splitting is not executed: create only two ordinary geometry/weight probes at one source spec.
for index in (0,9,11,73,137,211):
 original=problem.nodes[index]
 ref_original=ref_nodes[index]
 left=replace(original,node_id="probe-left",weight=Decimal("400"))
 right=replace(original,node_id="probe-right",weight=Decimal("100"))
 ref_left=replace(ref_original,node_id="probe-left",weight=Decimal("400"))
 ref_right=replace(ref_original,node_id="probe-right",weight=Decimal("100"))
 actual=factory.separator(left,right,first_sequence=1,related_partition_id="probe-partition")
 expected=r["_forced_virtual_separator"](ref_left,ref_right,ref_rules,ref_factory)
 from apsgo_scheduler.core.evaluation import quick_chain_prohibited_profile
 from apsgo_scheduler.core.model import Chain
 adjusted_candidates=[]
 for prototype,ref_prototype in zip(problem.virtual_prototypes,ref_factory.prototypes):
  rv=ref_factory.materialize(ref_prototype,ref_left,ref_right)
  tv=factory.materialize(prototype,left,right,purpose=VirtualPurpose.SPLIT_SEPARATOR,sequence=1,related_partition_id="probe-partition")
  raw_allowed=r["edge_allowed"](ref_left,rv,ref_rules) and r["edge_allowed"](rv,ref_right,ref_rules)
  target_allowed=cache.allows(left,tv) and cache.allows(tv,right)
  assert raw_allowed==target_allowed
  if not raw_allowed: continue
  raw_eval=r["evaluate_chain"](r["Chain"]([ref_left,rv,ref_right],ref_rules.period_order[0]),0,ref_rules)
  raw_prohibited=[v for v in raw_eval.violations if v.get("prohibited")]
  carrier=[v for v in raw_prohibited if v["rule_id"]=="reverse_width_limit" and ":reverse_carrier:" in v["subject_id"]]
  retained=[v for v in raw_prohibited if v not in carrier]
  adjusted_profile=(len(retained),round(sum(float(v.get("severity_value") or 0.0) for v in retained),6))
  target_profile=quick_chain_prohibited_profile(Chain("probe",(left,tv,right),left.source_period),rules,context)
  assert tuple(Decimal(str(v)) for v in adjusted_profile)==target_profile,(index,prototype.prototype_id,adjusted_profile,target_profile)
  adjusted_candidates.append(((*adjusted_profile,r["_virtual_smoothness"](ref_left,rv,ref_right)),ref_shape(rv),len(carrier)))
 adjusted=min(adjusted_candidates,key=lambda item:item[0]) if adjusted_candidates else None
 actual_shape=None if actual is None else target_shape(actual)
 expected_shape=None if expected is None else ref_shape(expected)
 assert actual_shape==(None if adjusted is None else adjusted[1]),(index,actual_shape,adjusted)
 if actual_shape!=expected_shape:
  assert adjusted[2]>0
  differences.append({"index":index,"source_node_id":original.node_id,"reference_prototype":expected.virtual_prototype_id,"target_prototype":actual.virtual_lineage.prototype_id,"removed_carrier_violation_count":adjusted[2]})
 else:
  counts["separator_raw_equal"]+=1
 assert budget.stop_reason is None
 counts["separator"]+=1
assert [d["index"] for d in differences]==[9,137,211]
assert budget.candidate_check_count==0 and budget.stop_reason is None
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
print(json.dumps({"source_sha256":manifest["script"]["sha256"],"counts":dict(counts),"bridge_and_materialization_equal":True,"separator_approved_carrier_differences":differences,"unexplained_differences":0,"candidate_checks":budget.candidate_check_count,"reference_attempt_counter":ref_factory.counter,"target_has_global_counter":hasattr(factory,"counter"),"full_search_executed":False}))
'
```

## 干净导出与提交

首轮八文件暂存树 `152e873c3c4b5b24bbde48fba0b485852d3bd818` 导出到 `/tmp/apsgo-virtual-initial-N3kJx5`。专项 129 项（0.39 秒）、聚焦 400 项（3.98 秒）、累计 1876 项（5.86 秒）通过；静态检查及 72 文件格式检查、残留保护和 compileall 通过。材料参考仍为 80 次桥、1080 次物化一致，6 次隔离中 3 次原始一致、3 次已确认承载过滤差异，未解释差异为零。初始方案指纹、31 条链和六级质量不变；图/匹配/初始阶段分别为 2.136/0.459/0.187 秒，不作为正式性能样本。

同一导出的内存复测：1/10/100/200 次重复尝试的对象加速表为 29/272/1024/1024 项，语义边缓存始终 29 项，当前内存分别为 73478/309176/1273895/974263 字节；选材不变，完整候选预算消耗为零。补齐本记录后再次导出最终暂存树，按上述同范围命令复验；全部通过才提交，并核对提交树与最终测试树一致。最终树及提交 SHA 由 Git 历史和执行输出标识，不在内容中自引用。

下一项是整链结构调整及完整候选接受；正式 GQGA4 质量与全流程性能仍未验收。
