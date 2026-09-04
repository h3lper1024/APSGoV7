# 功能 13：整链结构调整与完整候选接受验证

## 范围与身份

- 实施前提交：`12dd047741887bbfb887d977269ea7d4d8102812`；树 `fa37589a0bfacdb11e5348328d5ed6427da29de2`。
- 分支 `codex/solverpy-path-cover-clean`，macOS Darwin 27.0.0 arm64 / zsh，Conda `apsgo_v6_3.10.18`。
- 权威设计 v0.14，第 19、20.3、23、24、28.2 节；实施计划 v0.45 第 8.14 节。
- 精确白名单：`neighborhoods.py`、三份本功能测试、实施计划、AGENTS 和本文，共七文件。未触碰设计、规则、冻结输入/输出、门槛、V3 或外部参考源码。
- 参考脚本 SHA-256：`87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。脚本和五输入身份由现有基线清单保护；原始七级质量留档，目标仍为六级。

## 实现边界

复用现有完整评价、快速禁止轮廓、连接缓存、虚拟材料工厂和 `SearchState.commit_accepted()`。不增设候选事务框架、规则公式或资源台账。共用候选入口检查完整值覆盖、唯一身份、拆片目标期及新增虚拟序号连续；保留的旧虚拟节点不得改变原值。所有可能失败的评价与轨迹构造在正式提交前完成，拒绝/取消/异常不改变当前方案及正式编号。

整链专用链对重量预筛位于计数前；合并重量及启用的全局虚拟比例预筛位于计数后。供体欠重优先但不排除其他链，反转使用禁止轮廓字典序比较，供体变体外层、接收变体内层；两个连接方向及全部插入位置保持原始顺序，包括重复端点。接受后把合并链追加到剩余链末尾，从本邻域起点重扫；自然结束不标记全搜索停止。

## 原始整链阶段实跑

只在测试进程中通过 Python 调用观察器，于原始 `local_search()` 第 1777 行进入单节点移动前截取结果并退出。没有改写原始文件、计算公式或候选顺序，没有运行后续邻域。初始方案使用此前已三方核验一致的冻结 31 链。语义探测的 600 秒保护上限仅排除观察器开销，不改变正式 180 秒门槛，也不是性能样本。

实际原始整链结果：2539 次候选检查、9 次接受，接受位置依次为 85、86、147、199、223、263、343、412、934。每次有序方案、供体/接收索引和七级质量与冻结基线逐项相同。最终 22 链，原始质量 `(1, 670.3, 4, 996.37, 22, 140.0, 23460.88)`，含观察器用时 36.735 秒。仍有 4 条欠重链及 1 项禁止违规，不是最终产品结果。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import hashlib,json,runpy,sys,time
from decimal import Decimal
from pathlib import Path
base=Path("tests/baselines/gqga4")
manifest=json.loads((base/"reference_manifest.json").read_text())
source=Path(manifest["script"]["path"])
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
r=runpy.run_path(str(source),run_name="reference_whole_chain_probe")
data={name:r["decode_contract"](json.loads((base/"inputs"/name).read_text())) for name in ("optimization_problem.json","resolved_rules.json","rule_context.json","solver_config.json")}
ref_rules=r["parse_rule_book"](data["resolved_rules.json"],data["rule_context.json"],data["solver_config.json"])
ref_nodes=r["normalize_nodes"](data["optimization_problem.json"],ref_rules)
frozen=json.loads((base/"reference_stage_expectations.json").read_text())
ref_by_id={node.node_id:node for node in ref_nodes}
ref_initial=[r["Chain"]([ref_by_id[name] for name in row["node_ids"]],row["assigned_period"]) for row in frozen["initial_plan"]["plan"]]
factory=r["VirtualFactory"](r["load_virtual_prototypes"](data["optimization_problem.json"]),ref_rules)
budget=r["SearchBudget"](time.monotonic()+600,100000)
observed=[]
boundary={}
class WholeStageComplete(Exception): pass
def compact_plan(chains):
 return [{"assigned_period":c.assigned_period,"node_ids":[n.node_id for n in c.nodes]} for c in chains]
def accepted(chains,evaluation):
 frame=sys._getframe(1)
 if frame.f_locals["improvements"]:
  observed.append({"candidate_checks":budget.candidate_checks,"quality":list(evaluation.quality),"plan":compact_plan(chains),"donor_index":frame.f_locals["donor_index"],"target_index":frame.f_locals["target_index"]})
def isolate(frame,event,arg):
 if event=="call" and frame.f_code is r["SearchBudget"].permit.__code__:
  caller=frame.f_back
  if caller.f_code is r["local_search"].__code__ and caller.f_lineno==1777:
   boundary.update(plan=compact_plan(caller.f_locals["best"]),evaluation=caller.f_locals["best_eval"],chains=caller.f_locals["best"],checks=budget.candidate_checks)
   raise WholeStageComplete
started=time.monotonic()
previous=sys.getprofile()
try:
 sys.setprofile(isolate)
 try: r["local_search"](ref_initial,ref_rules,factory,budget,accepted)
 except WholeStageComplete: pass
finally: sys.setprofile(previous)
assert boundary and budget.stop_reason=="search_complete", (bool(boundary),budget.stop_reason)
expected=[a for a in frozen["search_rounds"][0]["accepted_actions"] if a["phase"]=="whole_chain"]
assert len(observed)==len(expected)==9
for index,(got,want) in enumerate(zip(observed,expected)):
 for key,value in got.items(): assert value==want[key],(index,key)
end=next(x for x in frozen["search_rounds"][0]["phase_boundaries"] if x["event"]=="end" and x["phase"]=="whole_chain")
assert budget.candidate_checks==end["candidate_checks"]==2539
assert list(boundary["evaluation"].quality)==end["quality"]
assert boundary["plan"]==expected[-1]["plan"]
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
print(json.dumps({"source_sha256":manifest["script"]["sha256"],"isolated_original_whole_stage":True,"candidate_checks":budget.candidate_checks,"accepted_checks":[x["candidate_checks"] for x in observed],"quality":list(boundary["evaluation"].quality),"chains":len(boundary["chains"]),"frozen_trace_exact":True,"seconds":time.monotonic()-started,"full_search_executed":False}))
'
```

## 同方案规则对照

把上述九个原始接受方案的真实节点、虚拟原型和温区逐项转换到当前值类型，再用当前正式规则评价；九次前六级质量均一致。该对照只证明这些相同方案的评价一致，不能代替新实现实际枚举轨迹；其他被拒绝候选仍可能受已确认规则差异影响。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import json,runpy,sys
from pathlib import Path
from decimal import Decimal as D
from dataclasses import replace
sys.path.insert(0,"src")
from apsgo_scheduler.core.model import Chain,SchedulePlan,VirtualPurpose
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.construction.test_initial_solution_reference_stage import initial_stage
_,problem,cache,budget,initial=initial_stage.__wrapped__()
factory=VirtualFactory(cache,budget)
frozen=json.loads(Path("tests/baselines/gqga4/reference_stage_expectations.json").read_text())
real={n.node_id:n for n in problem.nodes}
prototypes={p.prototype_id:p for p in problem.virtual_prototypes}
rows=[]
for step in [a for a in frozen["search_rounds"][0]["accepted_actions"] if a["phase"]=="whole_chain"]:
 nodes={}
 for chain in step["plan"]:
  for name in chain["node_ids"]:
   if name in real: nodes[name]=real[name]; continue
   raw=frozen["node_catalog"][name]
   proto=prototypes[raw["virtual_prototype_id"]]
   prepared=factory.materialize(proto,problem.nodes[0],problem.nodes[0],purpose=VirtualPurpose.EDGE_BRIDGE,sequence=len(nodes)+1)
   nodes[name]=replace(prepared,node_id=name,min_temperature=None if raw["min_soak_temp"] is None else D(str(raw["min_soak_temp"])),max_temperature=None if raw["max_soak_temp"] is None else D(str(raw["max_soak_temp"])))
 plan=SchedulePlan(tuple(Chain(f"probe-{i}",tuple(nodes[name] for name in row["node_ids"]),row["assigned_period"]) for i,row in enumerate(step["plan"])))
 evaluation=evaluate_plan(plan,cache.rule_set,cache.context)
 expected=tuple(D(str(v)) for v in step["quality"][:6])
 rows.append({"check":step["candidate_checks"],"reference_quality":step["quality"][:6],"target_same_plan_quality":[str(v) for v in evaluation.quality_key],"equal":evaluation.quality_key==expected})
assert len(rows)==9 and all(row["equal"] for row in rows), rows
print(json.dumps(rows))
'
```

## 开发期修正与结果

一次只读同方案探测误用了不存在的 `VirtualPurpose.BRIDGE` 名称，按当前枚举改为 `EDGE_BRIDGE` 后通过；未修改生产枚举或测试门槛。独立复核发现初稿只检查了仍在候选中的未声明受影响链，却没有拒绝省略该链后把其节点移入其他链的情况；已补齐未受影响链原身份、原值和相对顺序保护，新增省略/重排回归，不扩大业务规则。

生产文件 `neighborhoods.py` 为 392 行，SHA-256 `5f2c45534918b54af99f3655a456f54050968754ce9c6d0caea88596aea71f34`；生命周期 32 项、整链合成 14 项通过，静态/格式及残留保护通过。新目标完整整链阶段独立实跑为 2539 次候选检查、366 次完整评价和 9 次接受；全部接受位置、六级质量、有序真实节点及虚拟原型/重量/宽厚/温区和计划期与原始基线一致。正式虚拟序号为连续 1～7，不使用参考按尝试次数累加的临时号。

目标阶段诊断用时 29.393 秒，独立复核另一次为约 29.42 秒；二者都不是正式全流程性能样本。阶段自然结束，预算停止原因为空，后续邻域可继续；最终 22 链、六级质量 `(1,670.3,4,996.37,22,140)`。方案指纹 `196bc6f5c071569fe8ffd9bd98ef4ed8ea324047be8b23c36587180a7f65a64f`，接受轨迹指纹 `c84d7a58e662282948363a4199fa39567093576ed27530cdeaf5a769f5f98d25`。

## 目标整链对照命令

完整阶段只在本测试进程的搜索状态子类中记录已提交快照，不改变候选选择或接受判断。对照时仅去除已批准的虚拟编号差异，虚拟原型及全部物理字段仍逐项比较。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import json,sys,time
from decimal import Decimal as D, InvalidOperation
from pathlib import Path
sys.path.insert(0,"src")
from apsgo_scheduler.core.model import SearchState,MaterialRole
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.neighborhoods import SearchContext,improve_whole_chain
from tests.core.construction.test_initial_solution_reference_stage import initial_stage
from tests.core.test_solver_policy import policy
_,problem,cache,_,initial=initial_stage.__wrapped__()
snapshots=[]
def semantic_plan(plan):
 return tuple((chain.assigned_period,tuple(
  ("virtual",n.virtual_lineage.prototype_id,str(n.weight),str(n.width),str(n.thickness),str(n.min_temperature),str(n.max_temperature)) if n.material_role is MaterialRole.GENERATED_VIRTUAL else ("real",n.node_id)
  for n in chain.nodes)) for chain in plan.chains)
class ObservedState(SearchState):
 def commit_accepted(self,plan,evaluation,**kwargs):
  super().commit_accepted(plan,evaluation,**kwargs)
  snapshots.append({"checks":budget.candidate_check_count,"quality":tuple(evaluation.quality_key),"semantic_plan":semantic_plan(plan)})
state=ObservedState(initial.candidate.plan,initial.candidate.search_evaluation)
selected=policy(seed=590531,total_time_limit_seconds=D(610),finalization_reserve_seconds=D(10),candidate_check_limit=100000,whole_chain_pair_scan_slack_weight=D(40))
started=time.monotonic()
budget=SolveRuntimeBudget.from_policy(selected,started)
context=SearchContext(VirtualFactory(cache,budget),selected)
improve_whole_chain(state,context)
elapsed=time.monotonic()-started
frozen=json.loads(Path("tests/baselines/gqga4/reference_stage_expectations.json").read_text())
expected=[a for a in frozen["search_rounds"][0]["accepted_actions"] if a["phase"]=="whole_chain"]
def raw_semantic_plan(rows):
 result=[]
 for chain in rows:
  nodes=[]
  for name in chain["node_ids"]:
   n=frozen["node_catalog"][name]
   if n["node_type"]=="virtual_sphc":
    nodes.append(("virtual",n["virtual_prototype_id"],str(D(str(n["weight"]))),str(D(str(n["width"]))),str(D(str(n["thickness"]))),str(D(str(n["min_soak_temp"]))) if n["min_soak_temp"] is not None else "None",str(D(str(n["max_soak_temp"]))) if n["max_soak_temp"] is not None else "None"))
   else: nodes.append(("real",name))
  result.append((chain["assigned_period"],tuple(nodes)))
 return tuple(result)
def canonical_numbers(value):
 if isinstance(value,tuple): return tuple(canonical_numbers(x) for x in value)
 if isinstance(value,str):
  try: return D(value)
  except InvalidOperation: return value
 return value
differences=[]
for i,(got,want) in enumerate(zip(snapshots,expected),1):
 if got["checks"]!=want["candidate_checks"]: differences.append({"step":i,"field":"checks","reference":want["candidate_checks"],"target":got["checks"]})
 if got["quality"]!=tuple(D(str(x)) for x in want["quality"][:6]): differences.append({"step":i,"field":"quality","reference":want["quality"][:6],"target":[str(x) for x in got["quality"]]})
 if canonical_numbers(got["semantic_plan"])!=canonical_numbers(raw_semantic_plan(want["plan"])): differences.append({"step":i,"field":"ordered_plan"})
if len(snapshots)!=len(expected):differences.append({"field":"accepted_count","reference":len(expected),"target":len(snapshots)})
assert differences==[], differences[:8]
assert budget.stop_reason is None and budget.candidate_check_count==2539
assert state.accepted_move_count==len(context.accepted_move_traces)==9
assert [x["checks"] for x in snapshots]==[85,86,147,199,223,263,343,412,934]
assert state.virtual_sequence==7 and context.complete_candidate_evaluation_count==366
assert fingerprint(context.accepted_move_traces)=="c84d7a58e662282948363a4199fa39567093576ed27530cdeaf5a769f5f98d25"
assert fingerprint(state.current_plan)=="196bc6f5c071569fe8ffd9bd98ef4ed8ea324047be8b23c36587180a7f65a64f"
print(json.dumps({"candidate_checks":budget.candidate_check_count,"stop_reason":str(budget.stop_reason),"accepted_count":state.accepted_move_count,"accepted_checks":[x["checks"] for x in snapshots],"quality":[str(x) for x in state.current_evaluation.quality_key],"chain_count":len(state.current_plan.chains),"virtual_sequence":state.virtual_sequence,"complete_evaluation_count":context.complete_candidate_evaluation_count,"trace_fingerprint":fingerprint(context.accepted_move_traces),"plan_fingerprint":fingerprint(state.current_plan),"seconds":elapsed,"differences":differences[:8],"difference_count":len(differences),"full_search_executed":False}))
'
```

## 回归、导出与提交

共享专项 50 项（32.50 秒）、聚焦 450 项（36.90 秒）、累计 1926 项（38.68 秒）通过；76 文件静态/格式、残留保护通过。原始阶段对照仍为 2539/9 且九份方案逐项相同，同方案九次评价一致；目标检查 2539 次、完整评价 366 次、接受 9 次，方案及轨迹指纹保持，未解释差异为零。这里的时间包含测试开销/并行诊断，不能用于全流程性能门槛。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_complete_candidate_lifecycle.py tests/core/search/test_whole_chain_neighborhood.py tests/core/search/test_whole_chain_reference_trace.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

首轮暂存树 `09e0d072d393b878ebbaedd41f67a8d224a665a2` 导出到 `/tmp/apsgo-whole-initial-G6HMhd`。专项 50 项（32.60 秒）、聚焦 450 项（36.59 秒）、累计 1926 项（38.61 秒）通过；76 文件静态/格式、残留保护及导出编译 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools` 通过。三份原始阶段/同方案/目标阶段对照命令均通过，接受顺序、节点物理语义、六级质量、计数及目标两项指纹保持，未解释差异为零；目标阶段诊断 29.822 秒。

补齐本记录后再次导出最终暂存树，按相同集合复验，只有全部通过、精确七文件白名单和最终测试树一致，才创建本项独立提交。最终树及提交身份由 Git 历史和执行输出标识，不在本文自引用。不预先宣称局部搜索总控、拆单、最终审计或 GQGA4 门槛完成。
