# 功能 9：规则连接缓存与稳定构图

- 实施前提交：`2318f8f6ca57ed6b1f1f8b151f35908ec9abde35`，分支 `codex/solverpy-path-cover-clean`。
- 平台：Darwin 27.0.0 arm64、zsh；Conda `apsgo_v6_3.10.18` / Python 3.10.18。
- 权威设计 v0.14，SHA256 `7cf9a5c7cd2683e2302345435f9bdc4af803f41e2fdc8436fe89ff3b18be2645`；计划 v0.41 第 8.10 节。
- 本项十文件：连接缓存/图模块，规则基类及具体类，三份图测试，当前设计/计划/AGENTS 及本记录。必要白名单扩展已列入计划；不修改冻结输入、规则配置、门槛、参考脚本或外部 V3。

## 实现边界

使用普通任务内映射缓存有向相邻判断，不用旧矩阵或 NumPy。启用 EDGE 规则声明全部实际读取字段，可空软硬类别与必填声明分开。规则集、问题和不可变任务上下文绑定在缓存上，语义指纹包含声明的原值及物理投影、材料角色、虚拟原型/用途/关联分区及实际拆分分区；临时身份和接受序号排除。

缓存值为无主体身份模板，保留规则、原因、消息、处置、严重度和指标，命中后绑定当前主体；允许结论仅检查 PROHIBITED。当前实际边规则均报告禁止违规，不新增允许偏差。节点记忆保留不可变对象引用，避免每条边重复 SHA 编码和对象地址复用；地址不进入语义指纹。规则消息/指标也必须只依赖声明的业务字段，身份只允许出现在 subject_id；不符合主体契约的贡献明确拒绝，异常不存入边缓存。

构图按参考执行一次节点打散加稳定主排序，再逐节点执行后继打散加稳定排序；后继重量仅排序时投影 float。同键保留打散次序，战略优先级每节点只计算一次，全局 random 状态不改变。规则允许和构造宽度方向分开，规则缓存仍允许后续搜索使用小幅逆宽。

结果冻结完整节点与前向邻接表；记录检查数、允许数、命中数、方向拒绝数、耗时和停止原因。仅完整结果产生结构指纹；取消/搜索截止返回显式不完整结果，不能进入后续匹配。每节点/边使用零计数安全点，冻结/签发后再次探测预算；不消费完整候选额度。

两两边允许不能豁免跨虚拟段真实端点检查，专项用 A→虚拟→C 反例验证。未实现目标最大匹配、路径还原、初始链、虚拟生成或主搜索。

## 必要修复及开发期问题

- 既有合成宽度规则的两次 Decimal 减法受调用上下文影响。复用已有精确正差函数修复根因，没有把环境精度加入缓存键或要求调用者固定精度。低精度 2/5、不同舍入及 Inexact/Rounded 陷阱下直接规则、缓存命中和新缓存均一致；GQGA4 物理公式不变。
- 首轮图测试因测试辅助模块短名导入失败；改为现有 tests 命名空间的完整导入，不增加 sys.path 或新框架。
- 首轮累计出现 1 失败、1661 通过：后继五元组数字下标 3 命中已有基准数字保护。改为命名解包，排序语义不变，架构断言及门槛未修改。
- 独立只读审查覆盖完整缓存、图及测试；上述门禁问题已修正，无未关闭实质缺陷。

## 共享树验证

| 范围 | 结果 |
|---|---|
| 三份图专项，含真实 GQGA4 冻结图 | 80 项，2.43 秒 |
| 加规则集及预算/取消 | 230 项，2.59 秒 |
| 仓内累计 | 1663 项，4.28 秒 |
| Ruff / 格式 | 63 文件通过 |
| 残留与差异保护 | 通过；16 个稳定残留、IDE 不跟踪不导出 |

全部退出 0。测试耗时含并行运行影响，不用这些秒数冒充完整 180 秒验收。

```bash
# 专项
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/graph -q
# 聚焦
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/graph tests/core/rules/test_process_rule_set.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
# 仓内累计
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --check
```

## 原始参考、冻结图和目标图三方对照

只读执行原始 maximum_path_cover，在函数返回时用既有参考捕获方式读取局部的有序节点与邻接表；没有修改函数/文件。参考函数内部匹配照常运行，但本项目标没有实现匹配或搜索。源文件执行前后 SHA256 均为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。

| 项目 | 结果 |
|---|---|
| 输入节点 | 531 |
| 检查前向边 | 140715 |
| 允许边 | 32536 |
| 缓存命中 / 首次计算 | 33711 / 107004 |
| 完整候选消耗 | 0 |
| 有序节点、全部邻接表 | 原始执行、冻结基线、目标逐项一致 |
| 目标图指纹 | 7c5b6b5b50a1df725c91569527f9fcf96536f0f737bfbf32b4e1352c8f80a7ad |
| 问题指纹 | cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea |
| 规则指纹 | 420cd13d59763c140b23664f0cb0aca0437e0680b51899d2e0fb39563b7f5365 |

共享复测构图内部约 1.9867 秒，探针外包调用及额外断言约 2.0096 秒，仅单次构图，不是全流程性能。稳定非增宽排序使实际前向候选没有额外方向拒绝；另用 +10 宽度案例验证规则允许而构造方向拒绝，且不污染缓存。此前允许小幅逆宽和取消承载牌号等设计变更没有改变本份初始图的允许边集合。

以下完整命令在共享树和干净导出复跑：

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
assert g.fingerprint==fingerprint(observed)
assert budget.candidate_check_count==0 and budget.stop_reason is None
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
print(json.dumps({"source_sha256":manifest["script"]["sha256"],"input_fingerprint":problem.input_fingerprint,"rule_fingerprint":rules.fingerprint,"nodes":len(g.ordered_node_ids),"checked_edges":g.checked_edge_count,"allowed_edges":g.allowed_edge_count,"cache_hits":cache.hit_count,"cache_misses":cache.miss_count,"graph_fingerprint":g.fingerprint,"target_graph_seconds":g.elapsed_seconds,"outer_graph_seconds":time.monotonic()-started,"reference_and_frozen_graph_equal":True,"candidate_checks":budget.candidate_check_count,"target_matching_and_search_executed":False}))'
```

## 干净导出与提交

初验树 `8972063495cd4c6281fc5f078661fff0a77cc098`，导出 `/tmp/apsgo-graph-initial-RCpYM1`。同范围专项 80 项（2.42 秒）、聚焦 230 项（2.59 秒）、累计 1663 项（4.24 秒）全部通过；63 文件静态/格式、干净导出残留保护及上述原始参考三方对照均退出 0，节点/邻接/指纹一致。该次目标构图约 1.9926 秒，仍仅阶段计时。

仅在导出执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools`，退出 0。补齐本记录后再导出最终暂存树复测，并核对精确十文件白名单、共享残留保护及提交树身份。Git 差异检查在共享 Git 工作区执行，不在无 Git 元数据的导出执行。本项提交标识由 Git 历史记录，不自引用。

完整 GQGA4 质量与性能门槛尚未验收。下一项为最大二分匹配与最小路径覆盖。
