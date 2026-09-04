# 功能 11：无虚拟材料初始方案构造

- 实施前提交：`1fd2e229f83ff1c6b9a652ad7b9a1b5a18b71702`，分支 `codex/solverpy-path-cover-clean`。
- 当前平台：Darwin 27.0.0 arm64、zsh；Conda `apsgo_v6_3.10.18` / Python 3.10.18。
- 权威设计 v0.14，SHA256 `7cf9a5c7cd2683e2302345435f9bdc4af803f41e2fdc8436fe89ff3b18be2645`；计划 v0.43 第 8.12 节。
- 六文件范围：一个初始构造模块、两份构造测试、计划、AGENTS 和本记录。不改规则、配置、原始输入、冻结阶段或正式门槛。

## 实现边界

唯一入口 construct_initial_plan 使用完整图和路径覆盖、同任务问题/连接缓存以及共享预算。逐条路径扫描，第一节点直接建链；后续节点依次通过启用链重上限、缓存连接及快速禁止轮廓检查才追加，否则切链。链重停用无隐藏上限；绕过输入标准化的超重原子节点明确拒绝，不拆、不丢弃。

禁止轮廓复用现有二元组，按参考字典序 <= 比较；不改成两个分量分别不得增加。同一不变前缀的结果仅在本次循环复用，没有额外评分缓存。欠重和目标重量不阻止构造，原子节点已有的可修复业务禁止违规允许留给后续搜索。

链按路径/切分顺序获得 initial-000001 起的稳定身份，排产期取任务期序中的最早真实来源期，不重新排序链。输入节点原对象完整保留，既不生成虚拟材也不生成拆单片段。构造完成后一次完整评价，用既有 CoreCandidateSnapshot 保存计划和评价，再计算计划指纹；预算全部零计数探测。中断时返回停止原因，不返回半方案、评价或完成指纹；规则及编码异常向调用边界传播。

独立源码复核覆盖启用规则、精确重量、完整覆盖、参考比较顺序和取消边界；未发现需要业务决策或新增规则框架的缺口。Ponytail Lite 体现在复用既有缓存、值载体和评价，不保留参考不可达的非空初始桥分支。

## 共享树验证

生产及测试文件冻结后，以下命令均退出 0：专项 64 项（0.63 秒）、聚焦 307 项（3.79 秒）、仓内累计 1783 项（5.67 秒）；69 个文件的静态及格式检查通过，残留保护通过。真实参考对照重新通过，构图 2.209269 秒、匹配 0.473057 秒、初始构造 0.189659 秒，完整链序列和质量无差异。

生产文件 SHA256 为 c27023d12e8ff2bce294c56db34dd9b6d0fcfd33f6ee928fbaa45539b7fa8d31。60 项合成/边界测试覆盖重量容差及低 Decimal 上下文、链重停用、词典序比较、连接拒绝、任务期序、图/路径/问题绑定、预算零计数、计算异常及评价/指纹后取消；4 项真实初始阶段测试验证所有节点、链和指标。两轮独立代码复核没有未解决发现。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/construction -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --check
```

## 原始执行、冻结阶段及目标三方对照

原始 construct_initial_plan / evaluate_plan 直接执行，路径来自已逐项一致的原始匹配。原始脚本执行前后 SHA256 均为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。原始初始链序列、计划期、全部违规、指标及七维评分与冻结文件完全一致；目标再从输入构图、匹配并构造初始方案，逐链逐节点比较。

| 项目 | 共同结果 |
|---|---|
| 输入节点 / 真实重量 | 531 / 29333.91 吨 |
| 原始覆盖路径 / 初始链数 | 17 / 31 |
| 禁止违规数量 / 严重度 | 2 / 170.3 |
| 欠重链数 / 欠重总缺口 | 15 / 4003.93 吨 |
| 生成型虚拟材 / 原始工厂生成计数 | 0 / 0 |
| 未来借用重量 | 21354.53 吨，仅统计，不参与目标评分 |
| 目标初始方案指纹 | 6d3a206937521f3fac5ca727c05cc561052a63855f081874321fbf0e2a6e0427 |

目标六级质量为 (2, 170.3, 15, 4003.93, 31, 0)，与原始前六项完全相同。原始第七项借用重量按用户确认删除，仅保留统计；两位欠重投影在此数据下没有额外数值差异。链显示身份改为目标稳定编号，因此目标整体指纹不要求等于原始脚本的原生输出身份。

两项禁止均来自初始阶段尚未拆分的输入节点超过窄钢连续重量 500 吨：0002002055-000120 为 570.3 吨，0002002073-000010 为 600 吨；分别产生 70.3 和 100 的严重度。后续已批准的受控拆分仍须实施。初始候选允许保留这些违规，正式发布不允许。

初步目标构图 2.133429 秒、匹配 0.464069 秒、初始构造 0.181755 秒；只是阶段测量。候选检查计数为 0，没有执行局部搜索、末端拆单或最终审计，不宣称正式 GQGA4 质量/性能验收。

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
observed={}
def capture(frame,event,arg):
 if event=="return" and frame.f_code is r["maximum_path_cover"].__code__:
  values=frame.f_locals
  observed["ordered_node_ids"]=tuple(ref_nodes[i].node_id for i in values["indices"])
  observed["adjacency"]={ref_nodes[i].node_id:tuple(ref_nodes[j].node_id for j in successors) for i,successors in values["adjacency"].items()}
  observed["matching_edges"]=tuple((ref_nodes[i].node_id,ref_nodes[values["match_left"][i]].node_id) for i in values["indices"] if values["match_left"][i] is not None)
  observed["paths"]=tuple(tuple(n.node_id for n in path) for path in arg)
old=sys.getprofile()
try:
 sys.setprofile(capture)
 r["maximum_path_cover"](ref_nodes,ref_rules,random.Random(590531))
finally:
 sys.setprofile(old)
frozen=json.loads((base/"reference_stage_expectations.json").read_text())["path_cover"]
assert observed["ordered_node_ids"]==tuple(frozen["ordered_node_ids"])
assert observed["adjacency"]=={row["node_id"]:tuple(row["successor_node_ids"]) for row in frozen["adjacency"]}
t=runpy.run_path("tests/app/test_input_normalizer.py")
spec=t["gqga4_spec"].__wrapped__()
request=t["gqga4_request"].__wrapped__(spec)
problem=t["normalize"](request)
rules=load_rule_set(spec)
context=RuleEvaluationContext(problem.period_order,dict(zip(problem.period_order,range(len(problem.period_order)))),tuple(p.prototype_id for p in problem.virtual_prototypes))
cache=RuleEdgeDecisionCache(problem,rules,context)
started=time.monotonic()
budget=SolveRuntimeBudget(started,started+120,started+130,0,0,None)
g=build_construction_dag(problem,cache,budget,seed=590531)
assert g.complete,(g.stop_reason,g.checked_edge_count)
assert g.ordered_node_ids==observed["ordered_node_ids"]
assert g.adjacency==observed["adjacency"]
assert g.fingerprint==fingerprint({key:observed[key] for key in ("ordered_node_ids","adjacency")})
from apsgo_scheduler.core.path_cover import minimum_path_cover
result=minimum_path_cover(g,budget)
assert result.complete,result.stop_reason
expected_edges=tuple((name,frozen["match_left"][name]) for name in frozen["ordered_node_ids"] if frozen["match_left"][name] is not None)
expected_paths=tuple(tuple(path) for path in frozen["paths"])
assert result.matching_edges==observed["matching_edges"]==expected_edges
assert result.paths==observed["paths"]==expected_paths
assert len(result.paths)==len(g.ordered_node_ids)-len(result.matching_edges)
assert result.matching_fingerprint==fingerprint({"graph_fingerprint":g.fingerprint,"matching_edges":expected_edges})
assert result.path_fingerprint==fingerprint({"graph_fingerprint":g.fingerprint,"paths":expected_paths})
assert budget.candidate_check_count==0 and budget.stop_reason is None
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
from decimal import Decimal
from apsgo_scheduler.core.initial_solution import construct_initial_plan
from apsgo_scheduler.core.model import MaterialRole
ref_by_id={n.node_id:n for n in ref_nodes}
ref_paths=[[ref_by_id[name] for name in path] for path in observed["paths"]]
factory=r["VirtualFactory"](r["load_virtual_prototypes"](data["optimization_problem.json"]),ref_rules)
ref_initial=r["construct_initial_plan"](ref_paths,ref_rules,factory)
ref_initial_eval=r["evaluate_plan"](ref_initial,ref_rules)
frozen_initial=json.loads((base/"reference_stage_expectations.json").read_text())["initial_plan"]
ref_plan=[{"assigned_period":c.assigned_period,"node_ids":[n.node_id for n in c.nodes]} for c in ref_initial]
assert ref_plan==frozen_initial["plan"]
assert list(ref_initial_eval.quality)==frozen_initial["quality"]
assert list(ref_initial_eval.violations)==frozen_initial["violations"]
assert dict(ref_initial_eval.result_metrics)==frozen_initial["metrics"]
assert factory.counter==0 and not any(n.is_virtual for c in ref_initial for n in c.nodes)
initial=construct_initial_plan(problem,g,result,cache,budget)
assert initial.complete,initial.stop_reason
plan=initial.candidate.plan
evaluation=initial.candidate.search_evaluation
target_plan=[{"assigned_period":c.assigned_period,"node_ids":[n.node_id for n in c.nodes]} for c in plan.chains]
assert target_plan==ref_plan
assert tuple(Decimal(str(value)) for value in ref_initial_eval.quality[:6])==evaluation.quality_key
assert evaluation.metrics["borrowed_future_weight"]==Decimal(str(ref_initial_eval.quality[-1]))
assert initial.plan_fingerprint==fingerprint(plan)
assert len({n.node_id for c in plan.chains for n in c.nodes})==len(problem.nodes)
assert not any(n.material_role is MaterialRole.GENERATED_VIRTUAL for c in plan.chains for n in c.nodes)
assert budget.candidate_check_count==0 and budget.stop_reason is None
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
print(json.dumps({"source_sha256":manifest["script"]["sha256"],"graph_fingerprint":g.fingerprint,"path_fingerprint":result.path_fingerprint,"plan_fingerprint":initial.plan_fingerprint,"paths":len(result.paths),"initial_chains":len(plan.chains),"initial_quality":[str(v) for v in evaluation.quality_key],"borrowed_statistics_only":str(evaluation.metrics["borrowed_future_weight"]),"reference_virtual_factory_counter":factory.counter,"target_graph_seconds":g.elapsed_seconds,"target_matching_seconds":result.elapsed_seconds,"target_initial_seconds":initial.elapsed_seconds,"reference_and_frozen_initial_plan_equal":True,"target_and_reference_chains_equal":True,"candidate_checks":budget.candidate_check_count,"search_and_final_audit_executed":False}))
'
```

## 干净导出与提交

首轮六文件暂存树 aab4c6a568417932873ff53a852eebbed8175ce5 导出至 /tmp/apsgo-initial-solution-initial-1oK7xq，同范围专项 64 项（0.62 秒）、聚焦 307 项（3.74 秒）、累计 1783 项（5.61 秒）通过；69 文件静态/格式、残留、参考对照及 compileall 全部退出 0。参考对照中目标初始构造 0.182911 秒，链序列/评分/指纹与共享树相同。

本记录补齐后继续导出最终暂存树，执行相同命令集合，全部通过才提交并核对最终测试树与提交树一致。最终树及当前提交 SHA 由工具输出和 Git 历史标识，不在提交内容中自引用。

## 过程偏差

一次只读查看冻结结构时误用 chains 键，实际键为 plan；该查看命令退出 1，修正后核验完整真实结构，未修改任何输入或基线。所有正式比较均使用 plan 键。测试准备中对纯前缀函数的调用先后曾写得过严，按实际参考“先追加后原前缀”核实后，测试改为检查不同前缀各一次及语义结果；不得据此放松链顺序或评分断言。
