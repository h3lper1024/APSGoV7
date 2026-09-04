# 功能 10：最大二分匹配与最小路径覆盖

- 实施前提交：`140dcf70ce7e972e2f3ff3961052692bc5835c4d`，分支 `codex/solverpy-path-cover-clean`。
- 平台：Darwin 27.0.0 arm64、zsh；Conda `apsgo_v6_3.10.18` / Python 3.10.18。
- 权威设计 v0.14，SHA256 `7cf9a5c7cd2683e2302345435f9bdc4af803f41e2fdc8436fe89ff3b18be2645`；计划 v0.42 第 8.11 节。
- 六文件范围：一个生产模块、两份匹配/路径测试、计划、AGENTS 及本记录。规则、输入、配置、图构造、门槛和外部参考文件均不改。

## 实现与完成边界

唯一入口 minimum_path_cover 只接收完整 ConstructionDAG 和共享预算，不访问规则、重量或原始订单。广度分层、深度增广、路径起点扫描及后继顺序完全使用图的冻结序列；广度扫描发现空闲右节点后仍继续，与指定脚本一致。

显式栈逐帧模拟参考递归。成功增广的整段左右匹配回写不插入取消点，避免半次回写状态；其前后以及广度节点/边、深度扫描、路径恢复、完成签发前后都探测时间/取消。全部调用 permit(0)，不消费完整候选额度。

一个不可变结果保存来源图指纹、有序匹配边、路径、耗时和停止原因。完成时每节点出现一次、每个匹配边存在于图、路径数为节点数减匹配数。未完成只保留合法部分匹配，路径为空，匹配和路径指纹均为空；不能当作最大匹配结果进入初始链。完整指纹覆盖来源图身份及相应有序内容，不含耗时。

目标没有实现初始方案切链、虚拟材料、局部搜索或最终发布。17 条路径不是最终链数。

## 共享树验证

| 范围 | 结果 |
|---|---|
| 匹配/路径专项 | 56 项，0.65 秒 |
| 全部图专项及预算/取消 | 225 项，3.12 秒 |
| 仓内累计 | 1719 项，4.93 秒 |
| Ruff / 格式 | 66 文件通过 |
| 残留及差异保护 | 通过；16 个稳定残留，IDE 不跟踪不导出 |

全部退出 0。专项包括独立穷举全部 64 个四节点前向图的最大匹配大小、固定多解顺序、必须重新配对的增广、无边/断开/全连接图、1100 节点深增广和 GQGA4 全序列。取消与搜索截止覆盖分层、增广、路径和签发后安全点；额外原子性测试在每个实际安全点检查左右匹配互逆，重排后取消不会暴露半次回写。

独立源码复核未发现实质缺陷。早期 55/224/1718 项结果在补入原子性永久回归后不再作为最终依据；上述 56/225/1719 为文件冻结后的重跑。曾尝试未落盘测试文件而未收集测试，以及参考命令编排的 JavaScript 拼接语法错误；修正后全部完整重跑，不放宽测试断言或门槛。

```bash
# 专项
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/graph/test_bipartite_matching.py tests/core/graph/test_minimum_path_cover.py -q
# 聚焦
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/graph tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
# 仓内累计
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --check
```

## GQGA4：原始执行、冻结基线与目标的三方对照

只读执行原始 maximum_path_cover，在函数返回时捕获实际匹配和路径；目标从同一完整图运行新匹配。源码执行前后 SHA256 均为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。原始输入、规则和冻结参考轨迹不修改。

| 项目 | 三方共同结果 |
|---|---|
| 节点 / 有向允许边 | 531 / 32536 |
| 匹配边数 | 514 |
| 路径数 | 17，即 531 - 514 |
| 最长路径节点数 | 61 |
| 图指纹 | 7c5b6b5b50a1df725c91569527f9fcf96536f0f737bfbf32b4e1352c8f80a7ad |
| 匹配指纹 | b2d9302f13dec97193f95d9ba9f6a9e637aa26826c4a72f6569679d01f4b8892 |
| 路径指纹 | d462e286897895777339275057f8b2fd24cb315602ece0d8aa19a8997c9a5f42 |
| 匹配与路径有序内容 | 逐项一致；不仅比较数量 |
| 完整候选消耗 | 0 |

共享复测目标构图约 1.9917 秒、匹配及路径恢复约 0.4476 秒；只是阶段计时，不是全流程性能验收。指纹列使用目标统一规范编码对参考相同内容计算，不声称原脚本原生输出这些相同名称的指纹。

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
print(json.dumps({"source_sha256":manifest["script"]["sha256"],"graph_fingerprint":g.fingerprint,"nodes":len(g.ordered_node_ids),"matching_edges":len(result.matching_edges),"paths":len(result.paths),"max_path_length":max(map(len,result.paths)),"matching_fingerprint":result.matching_fingerprint,"path_fingerprint":result.path_fingerprint,"target_graph_seconds":g.elapsed_seconds,"target_matching_seconds":result.elapsed_seconds,"reference_and_frozen_matching_paths_equal":True,"candidate_checks":budget.candidate_check_count,"target_initial_and_search_executed":False}))'
```

## 参考递归深度风险的隔离证据

另用只读内存适配固定图：第 i 个节点连接 i+1/i+2，后继顺序为 i+2 在前，禁止随机打散；匹配和路径恢复仍调用原始函数。当前递归上限 1000：8 节点返回一条完整路径，1100 节点发生 RecursionError。目标永久测试在同族图恢复 1099 条匹配和一条完整路径，且不修改递归上限。

这是匹配内核的栈风险证据，不是原始规则或真实 GQGA4 排程对照；只在内存临时替换图谓词并在 finally 恢复，不修改源文件或基线。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import hashlib,json,runpy,sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
manifest=json.loads(Path("tests/baselines/gqga4/reference_manifest.json").read_text())
p=Path(manifest["script"]["path"]); h=manifest["script"]["sha256"]
assert hashlib.sha256(p.read_bytes()).hexdigest()==h
f=runpy.run_path(str(p))["maximum_path_cover"]; g=f.__globals__; old=g["_direct_dag_allowed"]
class OrderedRandom:
 def shuffle(self,values): pass
out=[]
try:
 g["_direct_dag_allowed"]=lambda a,b,r: b.index in (a.index+1,a.index+2)
 for n in (8,1100):
  nodes=[SimpleNamespace(index=i,node_id=str(i),width=float(n-i),strategic_rank=0,thickness=.5,min_soak_temp=700.,weight=Decimal(i+1)) for i in range(n)]
  try:
   paths=f(nodes,None,OrderedRandom()); out.append({"n":n,"paths":[[x.index for x in path] for path in paths]})
  except RecursionError:
   out.append({"n":n,"error":"RecursionError"})
finally:
 g["_direct_dag_allowed"]=old
assert hashlib.sha256(p.read_bytes()).hexdigest()==h
assert out[0]["paths"]==[list(range(8))]
assert out[1].get("error")=="RecursionError"
print(json.dumps({"recursion_limit":sys.getrecursionlimit(),"cases":out,"scope":"fixed_DAG_adapter_only","source_sha256":h}))'
```

## 干净导出与提交

首轮六文件精确暂存树 `23dc2d466ac29317312a0b01c45c4677b8b69bfe` 已导出至 `/tmp/apsgo-path-cover-initial-Y4VziT`：专项 56 项（0.64 秒）、聚焦 225 项（3.10 秒）、累计 1719 项（4.89 秒）全部通过；66 个文件的静态及格式检查、残留保护和 compileall 通过，所有命令退出码均为 0。原始参考、冻结阶段与目标图/匹配/路径逐项一致，目标构图 1.985600 秒、匹配 0.446757 秒；8/1100 节点的参考递归隔离核验也通过。

本记录补齐后，最终暂存树继续以同一组专项、聚焦、累计、静态、残留、两组参考及 compileall 命令复测；只有全部退出码为 0 才提交，并核对提交树与该最终测试树一致。最终树与当前提交 SHA 由工具输出和 Git 历史标识，不在提交内容中自引用。

本项不是完整排程或正式质量/性能验收；下一项为直接追加或切链的初始方案。
