# 功能 14：单个真实订单移动验证

## 范围、身份与实现

- 实施前提交 `f3d910dede2c13b16398a87725e37255a7bc8025`，树 `a49baa6c209c2c8b3e679bbd62ba66ac614c8f82`；分支 `codex/solverpy-path-cover-clean`。
- macOS Darwin 27.0.0 arm64 / zsh，Conda `apsgo_v6_3.10.18`；设计 v0.14 第 19、20.4、23 节、计划 v0.46 第 8.15 节。
- 六文件白名单：`neighborhoods.py`、两份单节点测试、本文、实施计划和 AGENTS。其他源码、旧设计、冻结基线和规则配置不改。
- 新增 `improve_real_node_relocation()`，复用既有候选接受入口。仅移动真实节点，包含实际过渡材和已有拆片，排除生成型虚拟材料；供体/接收链保留原身份及原索引，不增长虚拟序号。
- 供体剩余重、接收链上限、供体闭合边都在位置计数前；每个位置先计数再检查左右边。空/纯虚拟供体在位置计数及边检查后拒绝，避免构造非法链；拆片目标期由共用资格检查处理，不新增全局虚拟比例等硬门。
- 链重规则停用无隐藏上下限，邻域自然为空；首次严格改善后只重启当前邻域，自然穷尽不标记全局搜索完成。

## 原始阶段与首个差异

原始脚本 SHA-256 为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。独立执行原始整链阶段及单节点阶段，在原始第 1842 行进入填充之前截停：原始累计检查 65594 次，单节点部分 63055 次、24 次完整评价、12 次接受；所有原始动作及有序方案与冻结记录完全相同。原始最终六级为 `(1,670.3,2,548.32,22,140)`，第七项借用重量 23460.88 仅保留为原始证据和目标统计。

新目标在累计第 3801 次检查先接受，而原始首次在 3802 次。两边移动同一 `0030118934-000010`，供体索引 1、接收索引 10、供体节点索引 34；目标插入位置 19，原始选择 20（均从零计数）。位置 19 的邻接为：

`0002002202-000230（1020 mm）→ 0030118934-000010（910 mm）→ 0002002180-000060（921 mm，SPHETI-3）`。

两条相邻边均允许，第二条仅增宽 11 mm，不超过已确认的 20 mm。原始 `solver.py:983–997` 在完整链评价中额外要求增宽承接牌号属于 SPHC，给出 `CHAIN_011:reverse_carrier:20`、严重度 1。该候选原始质量 `(2,671.3,3,913,22,140,23460.88)`，当前质量 `(1,670.3,3,913,22,140)`；用户已明确取消这项牌号过滤，因此当前接受是预期规则差异，不是计数位置缺陷。

## 单一规则差异隔离

只在独立测试进程里，将原始只读规则值的现有 `reverse_width_carrier_grades` 换成其已有的 `{'*'}` 通配值；其他规则、参考算法、输入、枚举与数值公式不改，外部文件不写入。原始本来支持该通配值，所以没有另写替代算法。

隔离后原始与当前的全部 15 次接受动作、供体/接收/节点/插入索引、移动身份、检查位置、前六级质量以及每次完整链的真实节点、虚拟原型/重量/宽厚/温区/计划期均一致。共同累计检查 81823 次（本邻域 79284 次）、本邻域完整评价 24 次；最终六级 `(1,670.3,2,407.25,22,140)`，22 链、7 个虚拟节点、140 吨虚拟重量保持。未解释差异为零；原始 12 动作历史不改写，测试仅新增已双重验证的目标 15 动作金样。

目标方案指纹 `27a71417e7ced252fead81b4009abefd536b57dc012c726cec8f3087d2edf75c`；本邻域轨迹指纹 `b600ba4c408648b4445d22a664f4b570a97bc41a0447032f26679acbb1d3cdb0`，接受序号从 10 到 24（前一整链阶段已接受 9 次）。该指纹只覆盖本邻域轨迹，不混称从初始开始的完整轨迹。

### 原始、规则隔离与目标三方复现命令

命令先运行未调整的原始流程并核对冻结历史，再只隔离承载过滤，最后执行当前邻域逐项比较。预算子类只观察原始阶段边界，委托原始计数方法；评价包装仅计数并原样委托。600 秒为诊断安全上限，不代表或修改正式 180 秒全流程门槛。

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
observed=[]
boundary={}
phase="initial"
evaluation_counts={}
class BeforeFill(Exception): pass
class ObservedBudget(r["SearchBudget"]):
 def permit(self,count=1):
  global phase
  caller=sys._getframe(1)
  if caller.f_code is r["local_search"].__code__:
   if caller.f_lineno==1842:
    boundary.update(plan=compact_plan(caller.f_locals["best"]),evaluation=caller.f_locals["best_eval"],checks=self.candidate_checks)
    raise BeforeFill
   phase="whole_chain" if caller.f_lineno<1777 else "single_order"
  return super().permit(count)
budget=ObservedBudget(time.monotonic()+600,100000)
def compact_plan(chains):
 return [{"assigned_period":c.assigned_period,"node_ids":[n.node_id for n in c.nodes]} for c in chains]
def accepted(chains,evaluation):
 frame=sys._getframe(1)
 if frame.f_locals["improvements"]:
  action={"phase":phase,"candidate_checks":budget.candidate_checks,"quality":list(evaluation.quality),"plan":compact_plan(chains),"donor_index":frame.f_locals["donor_index"],"target_index":frame.f_locals["target_index"]}
  if phase=="single_order":
   action.update(node_index=frame.f_locals["node_index"],position=frame.f_locals["position"],moved_node_id=frame.f_locals["moved_node"].node_id)
  action["live_plan"]=tuple(chain.clone() for chain in chains)
  observed.append(action)
original_evaluate=r["local_search"].__globals__["evaluate_plan"]
def counted_evaluate(*args,**kwargs):
 evaluation_counts[phase]=evaluation_counts.get(phase,0)+1
 return original_evaluate(*args,**kwargs)
started=time.monotonic()
try:
 r["local_search"].__globals__["evaluate_plan"]=counted_evaluate
 try:r["local_search"](ref_initial,ref_rules,factory,budget,accepted)
 except BeforeFill:pass
finally:r["local_search"].__globals__["evaluate_plan"]=original_evaluate
assert boundary and budget.stop_reason=="search_complete"
expected=[a for a in frozen["search_rounds"][0]["accepted_actions"] if a["phase"] in ("whole_chain","single_order")]
assert len(observed)==len(expected)==21
for index,(got,want) in enumerate(zip(observed,expected)):
 for key,value in got.items():
  if key=="live_plan":continue
  expected_value=want["moved_node"]["node_id"] if key=="moved_node_id" else want[key]
  assert value==expected_value,(index,key)
end=next(x for x in frozen["search_rounds"][0]["phase_boundaries"] if x["event"]=="end" and x["phase"]=="single_order")
assert budget.candidate_checks==end["candidate_checks"]==65594
assert list(boundary["evaluation"].quality)==end["quality"]
assert boundary["plan"]==expected[-1]["plan"]
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]

raw_summary={"candidate_checks":budget.candidate_checks,"single_order_checks":budget.candidate_checks-2539,"accepted_checks":[x["candidate_checks"] for x in observed if x["phase"]=="single_order"],"quality":list(boundary["evaluation"].quality),"evaluation_counts":dict(evaluation_counts)}
legacy_rules=ref_rules
from dataclasses import replace
ref_rules=replace(ref_rules,reverse_width_carrier_grades=frozenset({"*"}))
factory=r["VirtualFactory"](r["load_virtual_prototypes"](data["optimization_problem.json"]),ref_rules)
budget=ObservedBudget(time.monotonic()+600,100000)
observed=[]
boundary={}
phase="initial"
evaluation_counts={}
try:
 r["local_search"].__globals__["evaluate_plan"]=counted_evaluate
 try:r["local_search"](ref_initial,ref_rules,factory,budget,accepted)
 except BeforeFill:pass
finally:r["local_search"].__globals__["evaluate_plan"]=original_evaluate
assert boundary and budget.stop_reason=="search_complete"
overlay_actions=[a for a in observed if a["phase"]=="single_order"]
overlay_summary={"candidate_checks":budget.candidate_checks,"evaluation_counts":dict(evaluation_counts),"accepted_checks":[a["candidate_checks"] for a in overlay_actions],"quality":list(boundary["evaluation"].quality)}
legacy_first=original_evaluate(list(overlay_actions[0]["live_plan"]),legacy_rules)
legacy_carriers=[v for v in legacy_first.violations if ":reverse_carrier:" in v["subject_id"]]
assert len(legacy_carriers)==1 and legacy_carriers[0]["severity_value"]==1.0
assert legacy_first.quality[:2]==(2,671.3), legacy_first.quality
import json,sys,time
from dataclasses import replace
from decimal import Decimal as D
sys.path.insert(0,"src")
from apsgo_scheduler.core.model import SearchState
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.neighborhoods import SearchContext,improve_real_node_relocation
from tests.core.search.test_single_node_reference_trace import relocation_start
from tests.core.search.test_whole_chain_reference_trace import target_signature,reference_signature
frozen,cache,plan,evaluation,policy=relocation_start.__wrapped__()
snapshots=[]
class ObservedState(SearchState):
 def commit_accepted(self,plan,evaluation,**kwargs):
  caller=sys._getframe(2)
  action={"checks":budget.candidate_check_count,"quality":[str(x) for x in evaluation.quality_key],"donor_index":caller.f_locals["donor_index"],"target_index":caller.f_locals["target_index"],"node_index":caller.f_locals["node_index"],"position":caller.f_locals["position"],"moved_node_id":caller.f_locals["moved"].node_id,"plan":plan}
  super().commit_accepted(plan,evaluation,**kwargs)
  snapshots.append(action)
selected=replace(policy,total_time_limit_seconds=D(610),finalization_reserve_seconds=D(10))
started=time.monotonic()
budget=SolveRuntimeBudget.from_policy(selected,started)
budget.candidate_check_count=2539
context=SearchContext(VirtualFactory(cache,budget),selected)
state=ObservedState(plan,evaluation,accepted_move_count=9,virtual_sequence=7)
improve_real_node_relocation(state,context)

def live_reference_signature(chains):
 def item(n):
  if not n.is_virtual:return n.node_id
  return ("virtual",n.virtual_prototype_id,n.weight,*(None if value is None else D(str(value)) for value in (n.width,n.thickness,n.min_soak_temp,n.max_soak_temp)))
 return tuple((c.assigned_period,tuple(item(n) for n in c.nodes)) for c in chains)
assert len(overlay_actions)==len(snapshots)==15
for index,(reference,target) in enumerate(zip(overlay_actions,snapshots)):
 assert reference["candidate_checks"]==target["checks"],(index,"checks")
 assert tuple(D(str(v)) for v in reference["quality"][:6])==tuple(D(v) for v in target["quality"]),(index,"quality")
 for key in ("donor_index","target_index","node_index","position","moved_node_id"):
  assert reference[key]==target[key],(index,key)
 assert live_reference_signature(reference["live_plan"])==target_signature(target["plan"]),(index,"ordered_plan")
assert budget.candidate_check_count==overlay_summary["candidate_checks"]==81823
assert context.complete_candidate_evaluation_count==overlay_summary["evaluation_counts"]["single_order"]==24
assert budget.stop_reason is None and state.accepted_move_count==24 and state.virtual_sequence==7
assert fingerprint(state.current_plan)=="27a71417e7ced252fead81b4009abefd536b57dc012c726cec8f3087d2edf75c"
assert fingerprint(context.accepted_move_traces)=="b600ba4c408648b4445d22a664f4b570a97bc41a0447032f26679acbb1d3cdb0"
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
print(json.dumps({"raw":raw_summary,"carrier_filter_removed_only":overlay_summary,"target_all_15_moves_and_plans_equal_overlay":True,"first_differing_candidate":3801,"legacy_first_quality":list(legacy_first.quality),"legacy_extra_carrier":legacy_carriers,"target_first_quality":snapshots[0]["quality"],"target_final_quality":[str(x) for x in state.current_evaluation.quality_key],"target_plan_fingerprint":fingerprint(state.current_plan),"target_trace_fingerprint":fingerprint(context.accepted_move_traces),"unexplained_differences":0,"full_search_executed":False}))
'
```

### 实际前后邻域连续执行

此命令实际执行整链搜索后，把同一个状态、缓存和预算直接交给单节点移动；不使用冻结末态代替前一搜索。复用现有便携夹具采用确定性工作量计数，不作为时间门验收。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import json,sys,time
sys.path.insert(0,"src")
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core.neighborhoods import improve_real_node_relocation
from tests.core.search.test_whole_chain_reference_trace import whole_stage
started=time.monotonic()
_,_,state,context,_=whole_stage.__wrapped__()
assert fingerprint(state.current_plan)=="196bc6f5c071569fe8ffd9bd98ef4ed8ea324047be8b23c36587180a7f65a64f"
single_started=time.monotonic()
improve_real_node_relocation(state,context)
assert context.factory.budget.stop_reason is None
assert context.factory.budget.candidate_check_count==81823
assert context.complete_candidate_evaluation_count==390
assert state.accepted_move_count==len(context.accepted_move_traces)==24
assert state.virtual_sequence==7 and state.split_sequence==0
assert fingerprint(state.current_plan)=="27a71417e7ced252fead81b4009abefd536b57dc012c726cec8f3087d2edf75c"
assert fingerprint(context.accepted_move_traces[:9])=="c84d7a58e662282948363a4199fa39567093576ed27530cdeaf5a769f5f98d25"
assert fingerprint(context.accepted_move_traces[9:])=="b600ba4c408648b4445d22a664f4b570a97bc41a0447032f26679acbb1d3cdb0"
print(json.dumps({"actual_whole_then_single":True,"candidate_checks":context.factory.budget.candidate_check_count,"complete_evaluations":context.complete_candidate_evaluation_count,"accepted_count":state.accepted_move_count,"plan_fingerprint":fingerprint(state.current_plan),"combined_trace_fingerprint":fingerprint(context.accepted_move_traces),"quality":[str(x) for x in state.current_evaluation.quality_key],"single_seconds":time.monotonic()-single_started,"prefix_seconds":time.monotonic()-started,"deterministic_count_probe":True,"full_search_and_audit_executed":False}))
'
```

## 开发修正、验证与提交状态

独立规则隔离探测曾把冻结 JSON 的 Decimal 物理值直接交给原始 float 算法，触发适配层类型错误；恢复原始四个物理字段的 float 表示后对照通过，重量仍是 Decimal。没有修改生产代码、规则或冻结证据来迁就该测试错误。

合成测试首轮有一个接收上限边界断言误把“首个超重移动被跳过”当成“所有后续节点都不可移动”；实际后续合法节点仍应进入位置计数。已改为精确验证首个接受节点及一次计数，不修改生产逻辑或重量门槛。生产增量 104 行，文件 SHA-256 `7d02fa99b67859c1db40734fa0f53628d3d881f75facb8e01f797ecdded5b88a`；两份新测试分别为 34 项合成和 5 项参考/规则差异回归，独立生产复核无剩余具体缺陷。

共享专项 39 项（1.80 秒）、聚焦 489 项（38.04 秒）、累计 1965 项（40.05 秒）通过；78 文件静态/格式及残留保护通过。三方参考命令通过，15 个接受动作及完整方案一致；实际连续执行两个邻域为累计 81823 次检查、390 次完整评价、24 次接受，方案指纹与独立单节点验证相同。从整链开始的合并轨迹指纹为 `f410126e35986e38cab41fc9cb6f8a72e798750ee7e14cc5ce6bdea191b5fdc0`。单节点阶段诊断约 1.02 秒、两邻域约 31.01 秒，不是正式全流程性能样本。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_single_real_node_relocation.py tests/core/search/test_single_node_reference_trace.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

首轮干净导出树 `5b9add5c7f00bc1e074149ff817de57c88d623e9`，目录 `/tmp/apsgo-relocation-initial-c6y9Gx`：专项 39 项（1.76 秒）、聚焦 489 项（37.92 秒）、累计 1965 项（39.75 秒）通过；78 文件静态/格式、残留保护、三方参考及实际两邻域连续执行均通过，全部退出码 0。导出额外执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools`，退出码 0。补齐本记录后再次导出最终暂存树，按相同集合复验，通过后核对精确六文件和提交树一致再提交；最终树与提交身份由 Git 历史及下一项执行入口记录，不在本文自引用。

当前结果仍有两条欠重链和一项禁止违规，填充、拆单、搜索总控与正式审计尚未完成；不称为最终排程或性能验收。
