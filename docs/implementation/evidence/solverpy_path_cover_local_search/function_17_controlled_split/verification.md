# 功能 17：受控拆单与一次完整搜索重放验证

## 实施基线与范围

- 实施前提交 `dead198b24ce95f9c3dcdbe76d0f21bec3adcfce`，树 `411a53fee298e9b463ceb532e42274e0b1bc68fa`；分支 `codex/solverpy-path-cover-clean`。
- macOS Darwin 27.0.0 arm64 / zsh，Conda `apsgo_v6_3.10.18`；设计 v0.14 第 10.2.2、19、21、23 节，实施计划 v0.49 第 6.2/8.18 节。
- 精确八文件：新 `controlled_split.py`、既有 `neighborhoods.py`、三份拆单/分区/重放测试、本文、实施计划和 AGENTS。旧设计、规则配置、原始脚本、冻结五输入、原始输出与正式门槛不改。
- 第一轮目标方案指纹 `cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86`，累计检查 86413、接受 35、虚拟序号 18，六级质量 `(1,670.3,1,194.42,22,360)`。这是已验证的阶段起点，不是最终合格排程。

## 已明确的接线要求

1. 只通过规则集获取拆分资格，不在动作中复制窄钢业务公式；相同主体在候选提交前再次向同一入口核验授权。
2. 复用同一完整候选评价、严格接受、动作轨迹和原子序号提交路径；普通候选的真实节点不变校验保持。
3. 精确向上取整，固定最大前片与精确尾片；强制隔离、数量/重量上限及启用虚拟比例通过后才消费候选额度。
4. 片段完整、重量精确守恒、父节点消失、其余真实及虚拟节点不变；谱系、授权期和序号完整。空供体删除，纯虚拟供体拒绝，不能丢弃旧虚拟材料。
5. 拆单模块唯一拥有自然完成后的续行和最多一次完整局部搜索重放；不重置真实预算、不反复拆单、不增加台账或第二套事务。
6. 已接受来源数及两种模式计数原子更新，最终审计由后续功能独立重放。

## 对照必须区分的三层

- 未改动原始脚本：从其冻结第一轮末态核对唯一未来借入 600 吨拆分及原始一次重放历史。
- 同一起点未来归还：当前不可变第一轮末态，同时给目标和仅取消承载牌号过滤的原始程序；目标诊断配置只允许未来归还，隔离动作移植本身。
- 正式两模式：使用未改动目标配置，单列同计划期拆分扩展，不宣称原始脚本已经支持。

当前第一轮重单链 `initial-000012` 排 BR1，依次包含 `0002002055-000120`（570.3 吨、来源 BR1）及 `0002002073-000010`（600 吨、来源 BR6）。主代理与独立代理已交叉核验该结构。实际按冻结顺序先接受前者同期间拆分，余下 600 吨单的原链按最早来源期规范化为 BR6；重扫它时按当时拆前期判为同期间。正式运行总数 2、同期间 2、未来归还 0，不能固定记作两种模式各一次。未来归还模式用独立的同起点诊断配置单独验证。

原始 `final_return_only_future_borrowed=False` 会关闭原始整个拆单功能，不是开启同期间拆单；不得用此开关伪造对照。

## 原始程序独立执行结果

主代理用原始工厂和同一累计预算，实际执行第一轮搜索、拆单、一次重放；独立代理另从核验过的首轮末态执行拆单及重放，两条路径结论一致。原始程序与冻结输入 SHA 保持不变。

| 执行配置 | 拆前检查数 | 拆后检查数 | 拆后原七级质量 | 一次重放终点 |
|---|---:|---:|---|---|
| 未改动原始规则 | 77204 | 77205 | `(1,70.3,3,395.19,23,540,22860.88)` | 94024，自然结束，`(1,70.3,0,0,22,540,22694.88)` |
| 仅在独立进程取消承载牌号过滤 | 86413 | 86414 | `(1,70.3,3,404.12,23,380,22860.88)` | 100000，候选数量截断，`(1,70.3,2,204.42,22,380,22860.88)` |

两者均拆 `0002002073-000010` 的 600 吨为 500/100 吨，整体从借入 BR1 归还来源 BR6。原始规则的强制隔离为 1250×0.5 原型、20 吨；取消承载牌号限制后为 1000×0.5、20 吨，原因是隔离选择也使用三节点禁止轮廓，并非只影响普通移动。

未改动原始程序的首轮 40 次接受、拆单 1 次接受和再次搜索 9 次接受，逐个有序方案、质量及检查点都与冻结历史一致。重放接受检查点为 `77489,82593,85845,89227,94014,94015,94017,94020,94024`。

取消承载限制版本重放只接受 `86692,91573,94671,97898` 四次，随后达到原有 100000 次上限；因此“新的规则口径下原始脚本也提前停止”已有独立实证。原始停止实现还会碰到后续填充入口但不接受，目标按已确认停止语义跳过该入口，不能为匹配旧调用日志恢复其缺陷。原始两种执行都仍有 570.3 吨同期间订单的 70.3 吨禁止严重度；目标双模式必须单独验证其消除。

## 正式双模式的实际集成执行

从已经核验的 31 链初始方案实际执行首轮三邻域、受控拆单及一次重放，不把冻结中间方案当成搜索结果：

| 阶段/接受动作 | 累计候选检查 | 六级质量 | 说明 |
|---|---:|---|---|
| 首轮结束 | 86413 | `(1,670.3,1,194.42,22,360)` | 35 次接受，完整评价 884 次 |
| 第一笔拆单 | 86414 | `(1,100,3,404.12,23,380)` | 570.3 → 500/70.3，目标 BR1 |
| 第二笔拆单 | 86415 | `(0,0,3,384.12,23,400)` | 600 → 500/100，目标 BR6；当前拆前期已是 BR6 |
| 重放整链插入 | 86701 | `(0,0,2,274.42,22,400)` | 只重放一轮 |
| 重放单节点移动 | 91582 | `(0,0,2,254.42,22,400)` | 保持分片授权目标期 |
| 重放单节点移动 | 94680 | `(0,0,2,230.42,22,400)` | 同上 |
| 重放单节点移动 | 97907 | `(0,0,2,204.42,22,400)` | 同上 |
| 数量截断 | 100000 | `(0,0,2,204.42,22,400)` | 不进入后续填充、不再次拆单 |

结果共 41 次接受、1039 次完整评价、虚拟序号 20；两个拆单均为同期间，未来归还计数 0；借用统计 22860.88 吨。完整有序方案指纹 `fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498`，包含首轮的完整接受轨迹指纹 `11f3ce5755b71fd71631c2ffec506fa04b9ed2241cf448d1e69f82e0ba794585`。独立集成探测本地搜索用时 62.37 秒，仅说明该次执行，不是包含入口/构图/审计/导出的完整耗时样本。

正式两模式与参考的首个结构差异发生在 86414：目标拆 570.3 吨本期单，原始仅未来模式拆 600 吨借入单。这是明确批准的资格扩展，而非未解释的遍历变化。当前搜索评价禁止违规为零，但仍有 2 条欠重链、缺口 204.42 吨；最终无缓存审计未实施，完整 GQGA4 门槛未通过。

## 独立参考执行命令

以下命令只在独立进程加载已固定 SHA 的原始程序；诊断副本仅改变承载牌号集合，原始文件不写入。原始全轨迹对照及目标单模式物理签名断言分别在此命令和可移植专项测试中闭环。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import hashlib,json,runpy,sys,time
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
base=Path("tests/baselines/gqga4")
manifest=json.loads((base/"reference_manifest.json").read_text())
source=Path(manifest["script"]["path"])
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
r=runpy.run_path(str(source),run_name="reference_fill_probe")
data={name:r["decode_contract"](json.loads((base/"inputs"/name).read_text())) for name in ("optimization_problem.json","resolved_rules.json","rule_context.json","solver_config.json")}
legacy=r["parse_rule_book"](data["resolved_rules.json"],data["rule_context.json"],data["solver_config.json"])
nodes={n.node_id:n for n in r["normalize_nodes"](data["optimization_problem.json"],legacy)}
frozen=json.loads((base/"reference_stage_expectations.json").read_text())
initial=[r["Chain"]([nodes[n] for n in row["node_ids"]],row["assigned_period"]) for row in frozen["initial_plan"]["plan"]]
results=[]
def signature(chains):
 def node(n):
  if not n.is_virtual:return n.node_id
  return ("virtual",n.virtual_prototype_id,n.weight,*(None if x is None else D(str(x)) for x in (n.width,n.thickness,n.min_soak_temp,n.max_soak_temp)))
 return tuple((c.assigned_period,tuple(node(n) for n in c.nodes)) for c in chains)
for label,rules in (("raw",legacy),("carrier_removed_only",replace(legacy,reverse_width_carrier_grades=frozenset({"*"})))):
 phase="initial"
 counts={}
 records=[]
 fill_start=None
 class ObservedBudget(r["SearchBudget"]):
  def permit(self,count=1):
   global phase,fill_start
   caller=sys._getframe(1)
   if caller.f_code is r["local_search"].__code__:
    phase="whole_chain" if caller.f_lineno<1777 else "single_order" if caller.f_lineno<1842 else "virtual_fill"
    if phase=="virtual_fill" and fill_start is None:fill_start=self.candidate_checks
   return super().permit(count)
 budget=ObservedBudget(time.monotonic()+600,100000)
 factory=r["VirtualFactory"](r["load_virtual_prototypes"](data["optimization_problem.json"]),rules)
 def accepted(chains,evaluation):
  frame=sys._getframe(1)
  if not frame.f_locals["improvements"]:return
  row={"phase":phase,"candidate_checks":budget.candidate_checks,"quality":list(evaluation.quality),"target_index":frame.f_locals["target_index"],"plan":[{"assigned_period":c.assigned_period,"node_ids":[n.node_id for n in c.nodes]} for c in chains],"live":tuple(c.clone() for c in chains)}
  if phase=="virtual_fill":
   filler=frame.f_locals["filler"]
   row.update(position=frame.f_locals["position"],prototype_id=filler.virtual_prototype_id)
  records.append(row)
 original=r["local_search"].__globals__["evaluate_plan"]
 def counted(*args,**kwargs):
  counts[phase]=counts.get(phase,0)+1
  return original(*args,**kwargs)
 try:
  r["local_search"].__globals__["evaluate_plan"]=counted
  plan,evaluation,improvements=r["local_search"](initial,rules,factory,budget,accepted)
 finally:r["local_search"].__globals__["evaluate_plan"]=original
 if label=="raw":
  expected=frozen["search_rounds"][0]["accepted_actions"]
  assert len(records)==len(expected)
  for index,(got,want) in enumerate(zip(records,expected)):
   for key in ("phase","candidate_checks","quality","target_index","plan"):
    assert got[key]==want[key],(index,key)
   if got["phase"]=="virtual_fill":assert got["position"]==want["position"]
  assert budget.candidate_checks==77204 and improvements==40
 fills=[x for x in records if x["phase"]=="virtual_fill"]
 results.append((label,rules,plan,evaluation,records,counts,budget,fill_start,factory))
 print(json.dumps({"label":label,"start":fill_start,"end":budget.candidate_checks,"stop":budget.stop_reason,"counts":counts,"accepted":improvements,"quality":list(evaluation.quality),"fill_actions":[{k:v for k,v in row.items() if k not in ("plan","live","phase")} for row in fills]}))
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]

for label,rules,initial_plan,initial_evaluation,prior_records,prior_counts,budget,fill_start,factory in results:
 split_records=[]
 def split_accepted(chains,evaluation):
  frame=sys._getframe(1)
  split_records.append({"candidate_checks":budget.candidate_checks,"quality":list(evaluation.quality),"chain_index":frame.f_locals["chain_index"],"node_index":frame.f_locals["node_index"],"parent_node":frame.f_locals["node"].node_id,"origin":frame.f_locals["assigned_period"],"piece_weights":[str(n.weight) for n in frame.f_locals["fragments"]],"separators":[(n.virtual_prototype_id,str(n.weight)) for n in frame.f_locals["separators"]],"plan":[{"assigned_period":c.assigned_period,"node_ids":[n.node_id for n in c.nodes]} for c in chains]})
 split_start=budget.candidate_checks
 plan,evaluation,accepted=r["final_cross_period_split_return"](initial_plan,rules,factory,budget,split_accepted)
 if label=="raw":
  assert accepted==1 and budget.candidate_checks==77205
  assert split_records[0]["plan"]==frozen["split"]["final"]["plan"]
  assert split_records[0]["quality"]==frozen["split"]["final"]["quality"]
 split_end=budget.candidate_checks
 split_quality=list(evaluation.quality)
 replay_records=[]
 def replay_accepted(chains,evaluation):
  caller=sys._getframe(1)
  if caller.f_locals["improvements"]:
   replay_records.append({"candidate_checks":budget.candidate_checks,"quality":list(evaluation.quality),"plan":[{"assigned_period":c.assigned_period,"node_ids":[n.node_id for n in c.nodes]} for c in chains]})
 if accepted:
  plan,evaluation,_=r["local_search"](plan,rules,factory,budget,replay_accepted)
 if label=="raw":
  expected=frozen["search_rounds"][1]
  assert budget.candidate_checks==94024 and len(replay_records)==9
  for got,want in zip(replay_records,expected["accepted_actions"]):
   for key in got:assert got[key]==want[key],key
  assert list(evaluation.quality)==expected["final"]["quality"]
 print(json.dumps({"label":label,"actual_first_round_then_split_then_one_replay":True,"split_start":split_start,"split_end":split_end,"split_quality":split_quality,"split_records":[{k:v for k,v in x.items() if k!="plan"} for x in split_records],"replay_checks":[x["candidate_checks"] for x in replay_records],"final_checks":budget.candidate_checks,"final_quality":list(evaluation.quality),"stop":budget.stop_reason}))
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
'
```

## 目标实际连续执行命令

先验证功能 16 原有完整首轮轨迹不变，再实际拆单并执行一次重放。时间断言不参与质量判断；固定候选预算与结果/轨迹指纹用于确定性核验。早期探测有一次汇总打印误把统计当作直接属性，已改为读取 `PlanEvaluation.metrics`；该探测退出 1 不记通过，以下正确命令已实际退出 0。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import json,sys,time
from decimal import Decimal as D
from pathlib import Path
sys.path.insert(0,"src")
from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.contracts import fingerprint,SolverPolicy,SearchStopReason
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.model import SearchState
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.construction.test_initial_solution_reference_stage import initial_stage
_,problem,cache,_,initial=initial_stage.__wrapped__()
policy=SolverPolicy(**json.loads(Path("tests/baselines/gqga4/gqga4_solver_policy.json").read_text(),parse_float=D))
runtime=SolveRuntimeBudget.from_policy(policy,0,clock=lambda:1.0)
context=neighborhoods.SearchContext(VirtualFactory(cache,runtime),policy)
state=SearchState(initial.candidate.plan,initial.candidate.search_evaluation)
before=fingerprint(problem)
names=("improve_whole_chain","improve_real_node_relocation","improve_virtual_weight_fill")
originals={name:getattr(neighborhoods,name) for name in names}
boundaries=[]
def wrap(name,original):
 def call(current,current_context):
  assert current is state and current_context is context
  assert current_context.factory.budget is runtime and runtime.stop_reason is None
  result=original(current,current_context)
  boundaries.append((name,runtime.candidate_check_count,context.complete_candidate_evaluation_count,state.accepted_move_count))
  return result
 return call
started=time.monotonic()
try:
 for name in names:setattr(neighborhoods,name,wrap(name,originals[name]))
 assert neighborhoods.run_local_search(state,context) is state
finally:
 for name in names:setattr(neighborhoods,name,originals[name])
assert boundaries==[(names[0],2539,366,9),(names[1],81823,390,24),(names[2],86413,884,35)]
assert runtime.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
assert fingerprint(problem)==before
assert fingerprint(state.current_plan)=="cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86"
assert fingerprint(context.accepted_move_traces)=="98836d09c4e96c1dbb4f1b1652f707e48c96692627a3dc8fea4090d2b16faf09"
print(json.dumps({"actual_orchestrator_from_initial":True,"boundaries":boundaries,"stop_reason":runtime.stop_reason.value,"quality":[str(x) for x in state.current_evaluation.quality_key],"plan_fingerprint":fingerprint(state.current_plan),"combined_trace_fingerprint":fingerprint(context.accepted_move_traces),"local_search_seconds":time.monotonic()-started,"split_audit_and_quality_gate_executed":False}))

from apsgo_scheduler.core import controlled_split
original_candidate=neighborhoods.try_complete_candidate
split_records=[]
replay_entries=[]
def observe_candidate(current,current_context,chains,**kwargs):
 accepted=original_candidate(current,current_context,chains,**kwargs)
 if accepted:
  split_records.append(dict(action=kwargs["action_name"],checks=runtime.candidate_check_count,quality=[str(x) for x in state.current_evaluation.quality_key],plan_fingerprint=fingerprint(state.current_plan),split_sequence=state.split_sequence,same=state.accepted_same_period_split_count,future=state.accepted_future_borrow_return_count))
 return accepted
original_replay=controlled_split.run_local_search
def observe_replay(current,current_context):
 replay_entries.append(dict(checks=runtime.candidate_check_count,evaluations=context.complete_candidate_evaluation_count,split_sequence=state.split_sequence))
 return original_replay(current,current_context)
try:
 neighborhoods.try_complete_candidate=observe_candidate
 controlled_split.try_complete_candidate=observe_candidate
 controlled_split.run_local_search=observe_replay
 assert controlled_split.run_controlled_order_split(state,context) is state
finally:
 neighborhoods.try_complete_candidate=original_candidate
 controlled_split.try_complete_candidate=original_candidate
 controlled_split.run_local_search=original_replay
assert fingerprint(problem)==before

assert (runtime.candidate_check_count,context.complete_candidate_evaluation_count,state.accepted_move_count,state.virtual_sequence,state.split_sequence,state.accepted_same_period_split_count,state.accepted_future_borrow_return_count)==(100000,1039,41,20,2,2,0)
assert state.current_evaluation.quality_key==(0,D(0),2,D("204.42"),22,D(400))
assert runtime.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
assert fingerprint(state.current_plan)=="fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498"
assert fingerprint(context.accepted_move_traces)=="11f3ce5755b71fd71631c2ffec506fa04b9ed2241cf448d1e69f82e0ba794585"
assert [r["checks"] for r in split_records]==[86414,86415,86701,91582,94680,97907]
assert replay_entries==[dict(checks=86415,evaluations=886,split_sequence=2)]

print(json.dumps(dict(actual_initial_first_round_split_replay=True,checks=runtime.candidate_check_count,evaluations=context.complete_candidate_evaluation_count,accepted=state.accepted_move_count,virtual_sequence=state.virtual_sequence,split_sequence=state.split_sequence,same=state.accepted_same_period_split_count,future=state.accepted_future_borrow_return_count,quality=[str(x) for x in state.current_evaluation.quality_key],borrowed=str(state.current_evaluation.metrics["borrowed_future_weight"]),stop_reason=runtime.stop_reason.value,plan_fingerprint=fingerprint(state.current_plan),trace_fingerprint=fingerprint(context.accepted_move_traces),records=split_records,replay_entries=replay_entries,total_local_seconds=time.monotonic()-started)))
'
```

## 验证命令与身份

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_controlled_order_split.py tests/core/search/test_split_partition_invariants.py tests/core/search/test_post_split_single_replay.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# Only in the clean export:
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```

- `neighborhoods.py` SHA-256：`f74acc3c366fbe8f954896b5dc146bde0da43651fac3e75768c93e5b6b32554c`。
- `controlled_split.py` SHA-256：`d51f411989d6f1b02cbf4ef497aaad6004219b4e3f363af1d341f2630f510b6f`。
- 三份测试 SHA-256 按拆单、分区、重放顺序为：`4d23b9d281006d58cd1189addf55065c17f2f095b07d950e18443e91e72a7fb5`、`6cab27f16bfd9341b11cce3ede7d6d5011331aaced1aee9708371e46fc4d2663`、`33ffa32be2308a719703545d78e51b41b72d30d23eecdfcbe5829413d872c71f`。
- 独立测试代理：前两文件 80 项通过（0.47 秒），连同旧候选/邻域轻量关联 218 项通过（0.73 秒）；重放文件 21 项通过（57.49 秒）。主代理重新执行同一三文件专项 101 项通过（57.18 秒）。这些数字不是完整验收次数。

## 当前状态

共享工作树已完成本项实现、独立复核和验证：专项 101 项（57.18 秒）、聚焦 657 项（145.63 秒）、累计 2133 项（149.27 秒），86 文件静态/格式、残留保护、原始参考命令及目标连续执行命令全部退出码 0。实际目标搜索阶段 61.55 秒，不是完整运行性能样本。随后对暂存树导出执行相同命令及编译，通过后补齐实际导出结果，再验证最终暂存树并按精确八文件提交。

首轮干净导出树 `27560fb0275969f34c6c4cc42e55aee87f6e540d`，目录 `/tmp/apsgo-split-initial-hYHpUG`：专项 101 项（57.01 秒）、聚焦 657 项（144.35 秒）、累计 2133 项（146.27 秒），静态/格式 86 文件、残留保护、原始参考、目标连续执行及编译全部退出码 0。首轮导出实际目标搜索阶段 61.76 秒。仅补充本段证据后再次验证最终暂存树的相同命令集合，通过才提交；最终树与提交身份由 Git 历史及下一项入口记录，不在本提交内自引用。

最终无缓存审计、完整应用发布及 GQGA4 质量/性能验收不属于本项完成声明；门槛和原始历史不改。
