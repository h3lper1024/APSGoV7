# 功能 15：虚拟过渡材料填充验证

## 范围与身份

- 实施前提交 `50bf0196385e5a831f7fc36f969cce005c0abc73`，树 `02669d8f425cfb78b9641c88e219508427c73b80`；分支 `codex/solverpy-path-cover-clean`。
- macOS Darwin 27.0.0 arm64 / zsh，Conda `apsgo_v6_3.10.18`；设计 v0.14 第 19、20.5、23 节，计划 v0.47 第 8.16 节。
- 六文件白名单：`neighborhoods.py`、两份填充测试、本文、实施计划和 AGENTS；原始脚本、冻结输入/输出、规则配置和正式质量/性能门槛不改。
- 原始脚本 SHA-256：`87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`；目标问题与规则指纹沿用功能 6/5.19。

## 实现边界

新增 `improve_virtual_weight_fill()`，按欠重链、插入位置、原型目录顺序枚举。每个位置与原型组合先消费一次候选检查，再物化、左右连接、链重上限、启用比例检查，最后完整评价；首个严格改善即接受并重扫当前邻域。温度锚点、拟用编号及用途复用材料工厂，接受只经过既有完整候选入口。

链重规则停用或无原型时不生成候选；填充不使用桥/隔离选择器，不受桥数量策略的预筛影响。比例复用原有规则及资源视图，连续虚拟数量等链级限制只在完整评价裁决。失败、同分、预算、时间、取消或异常均不发布半候选、不增长正式虚拟序号。

## 原始、批准规则差异与当前结果

| 项目 | 未改动的原始规则/流程 | 只移除承载牌号过滤的参考 | 当前目标 |
|---|---:|---:|---:|
| 填充起始累计检查 | 65594 | 81823 | 81823 |
| 填充结束累计检查 | 77204 | 86413 | 86413 |
| 填充阶段检查数 | 11610 | 4590 | 4590 |
| 填充阶段完整评价数 | 1288 | 494 | 494 |
| 填充接受次数 | 19 | 11 | 11 |
| 第一轮累计接受次数 | 40 | 35 | 35 |
| 禁止违规数 / 严重度 | 1 / 670.3 | 1 / 670.3 | 1 / 670.3 |
| 欠重链数 / 缺口（吨） | 1 / 185.49 | 1 / 194.42 | 1 / 194.42 |
| 链数 / 虚拟重量（吨） | 22 / 520 | 22 / 360 | 22 / 360 |

全部自然结束，未触及 100000 次候选门槛。原始仍保留七级质量，借用重量 23460.88；目标该值只作统计，正式质量为六级。当前停止原因仍为 `None`，因为本项只实现独立邻域，功能 16 总控尚未签发自然完成原因。

首差已在功能 14 追溯至用户批准取消的承载牌号过滤，不是本项修改规则。这里只在独立参考进程中使用原始规则值已有的 `reverse_width_carrier_grades={'*'}`，其他公式、枚举和规则不改，不写外部文件。原始全部 40 次接受和冻结轨迹一致；隔离后 11 次填充的检查点、接收链、插入位置、原型、每步六级质量及完整有序真实/虚拟物理方案与当前逐一相同，未解释差异为零。

当前 11 次接受检查点：81848、81872、81979、82085、82264、82443、82991、83539、84204、84869、85792；每次新增 20 吨，虚拟数量从 7 到 18。完整动作金样保存在本项参考测试；历史 19 次记录不改写。最终方案指纹 `cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86`，本邻域轨迹指纹 `6c8f43ac2226a090c052a2ecf857be75fc82cfebb8aa1b857af25e12dfbaad70`（接受序号 25～35）。

当前自身欠重从 407.25 降到 194.42；但原始旧规则在该阶段结束为 185.49，不能据上一阶段改善就宣称之后也一定优于旧规则轨迹。当前虚拟用量更少，但六级质量优先比较欠重缺口，不能改评分顺序掩盖该事实。拆单与最终验收尚未执行。

## 独立三方复现命令

先跑原始首轮并核对冻结动作，再只隔离既有承载过滤，最后运行当前填充逐步比较。观察器只计数/捕获，不代替原算法。600 秒仅是诊断保护，不改变正式 180 秒全流程门槛；当前目标按确定性计数夹具验证，不作为性能样本。

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
 results.append((label,rules,plan,evaluation,records,counts,budget,fill_start))
 print(json.dumps({"label":label,"start":fill_start,"end":budget.candidate_checks,"stop":budget.stop_reason,"counts":counts,"accepted":improvements,"quality":list(evaluation.quality),"fill_actions":[{k:v for k,v in row.items() if k not in ("plan","live","phase")} for row in fills]}))
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]

sys.path.insert(0,"src")
from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.contracts import fingerprint
from tests.core.search.test_single_node_reference_trace import relocation_start,single_stage
from tests.core.search.test_whole_chain_reference_trace import target_signature
state,previous,_=single_stage.__wrapped__(relocation_start.__wrapped__())
assert fingerprint(state.current_plan)=="27a71417e7ced252fead81b4009abefd536b57dc012c726cec8f3087d2edf75c"
context=neighborhoods.SearchContext(previous.factory,previous.policy)
snapshots=[]
original_candidate=neighborhoods.try_complete_candidate
def captured(*args,**kwargs):
 frame=sys._getframe(1)
 accepted=original_candidate(*args,**kwargs)
 if accepted:
  snapshots.append({"target_index":frame.f_locals["target_index"],"position":frame.f_locals["position"],"prototype_id":frame.f_locals["prototype"].prototype_id,"candidate_checks":context.factory.budget.candidate_check_count,"plan":state.current_plan,"quality":state.current_evaluation.quality_key})
 return accepted
started=time.monotonic()
try:
 neighborhoods.try_complete_candidate=captured
 neighborhoods.improve_virtual_weight_fill(state,context)
finally:neighborhoods.try_complete_candidate=original_candidate
_,_,ref_plan,ref_evaluation,ref_records,ref_counts,ref_budget,fill_start=results[-1]
fills=[x for x in ref_records if x["phase"]=="virtual_fill"]
assert len(fills)==len(snapshots)==11
for index,(reference,target) in enumerate(zip(fills,snapshots)):
 for key in ("target_index","position","prototype_id","candidate_checks"):
  assert reference[key]==target[key],(index,key)
 assert tuple(D(str(x)) for x in reference["quality"][:6])==target["quality"],(index,"quality")
 assert signature(reference["live"])==target_signature(target["plan"]),(index,"full_ordered_plan")
assert signature(ref_plan)==target_signature(state.current_plan)
assert context.complete_candidate_evaluation_count==ref_counts["virtual_fill"]==494
assert context.factory.budget.candidate_check_count==ref_budget.candidate_checks==86413
assert context.factory.budget.stop_reason is None
assert state.virtual_sequence==18 and state.accepted_move_count==35
assert state.current_evaluation.quality_key==(1,D("670.3"),1,D("194.42"),22,D(360))
print(json.dumps({"target_all_fill_moves_equal":True,"checks":86413,"local_checks":86413-fill_start,"full_evaluations":494,"plan_fingerprint":fingerprint(state.current_plan),"trace_fingerprint":fingerprint(context.accepted_move_traces),"local_fill_seconds":time.monotonic()-started,"unexplained_differences":0,"full_search_audit_executed":False}))
'
```

## 三个邻域实际连续执行

本命令实际执行整链、单订单移动和填充，共用同一状态、连接缓存和预算，不以冻结阶段末态代替前一搜索。仅用于接线实证，不提前实现功能 16 的生产总控入口。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import json,sys,time
sys.path.insert(0,"src")
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core.neighborhoods import improve_real_node_relocation,improve_virtual_weight_fill
from tests.core.search.test_whole_chain_reference_trace import whole_stage
started=time.monotonic()
_,_,state,context,_=whole_stage.__wrapped__()
improve_real_node_relocation(state,context)
assert fingerprint(state.current_plan)=="27a71417e7ced252fead81b4009abefd536b57dc012c726cec8f3087d2edf75c"
fill_started=time.monotonic()
improve_virtual_weight_fill(state,context)
assert context.factory.budget.stop_reason is None
assert context.factory.budget.candidate_check_count==86413
assert context.complete_candidate_evaluation_count==884
assert state.accepted_move_count==len(context.accepted_move_traces)==35
assert state.virtual_sequence==18 and state.split_sequence==0
assert fingerprint(state.current_plan)=="cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86"
assert fingerprint(context.accepted_move_traces[24:])=="6c8f43ac2226a090c052a2ecf857be75fc82cfebb8aa1b857af25e12dfbaad70"
print(json.dumps({"actual_whole_then_single_then_fill":True,"candidate_checks":context.factory.budget.candidate_check_count,"complete_evaluations":context.complete_candidate_evaluation_count,"accepted_count":state.accepted_move_count,"plan_fingerprint":fingerprint(state.current_plan),"combined_trace_fingerprint":fingerprint(context.accepted_move_traces),"quality":[str(x) for x in state.current_evaluation.quality_key],"fill_seconds":time.monotonic()-fill_started,"prefix_seconds":time.monotonic()-started,"deterministic_count_probe":True,"orchestration_split_and_audit_executed":False}))
'
```

## 验证状态

主代理和独立代理分别完成三方探测：原始历史一致，仅移除承载牌号过滤的参考与目标全部填充一致，退出码 0。共享专项 37 项（11.15 秒）、聚焦 526 项（48.90 秒）、累计 2002 项（51.19 秒）通过；80 文件静态/格式与残留保护通过。实际连续三邻域共 86413 次检查、884 次完整评价、35 次接受，合并轨迹指纹 `98836d09c4e96c1dbb4f1b1652f707e48c96692627a3dc8fea4090d2b16faf09`。当前填充阶段诊断约 9.28 秒，三邻域约 40.17 秒，不是完整运行时间门验收。

生产增量为 109 行新增、1 行导入调整，文件 SHA-256 `03144713d8e4f599fa2793b6f25e0499d737f722ca861779596e5430d9b323ea`；合成测试 32 项、参考测试 5 项，主代理及独立代理复核无剩余具体问题。现有两个邻域正文未改。合成/参考测试文件 SHA-256 分别为 `9d75ce39866f5601289ae17208a09616686b8c311e8c951ce43a79f604b4deaf`、`f84a6d562500af71837c9d1fe7766b472dab6270398040fb5e0135fe814e655d`。

共享验证命令如下，全部退出码 0。首轮导出树 `56cd72a87ba1de72fe8891a2ea49d533a20edc7a`，目录 `/tmp/apsgo-fill-initial-KCmo8U`；专项 37 项（11.15 秒）、聚焦 526 项（48.67 秒）、累计 2002 项（50.73 秒），静态/格式、残留保护、三方参考、实际三邻域连续执行及编译全部退出码 0。补齐本记录后对最终暂存树做相同集合复验，通过后按精确六文件独立提交；最终身份由 Git 历史及下一项入口记录，不在本提交自引用。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_virtual_weight_fill.py tests/core/search/test_virtual_fill_reference_trace.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# Only in the clean export:
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```
