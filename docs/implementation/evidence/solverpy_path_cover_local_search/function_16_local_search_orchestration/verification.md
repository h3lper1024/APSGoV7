# 功能 16：固定顺序局部搜索总控验证

## 范围与实施基线

- 提交前基线 `94a5014e966ef953c9f5fe55b3fa7401d673c607`，树 `ab5460b18c721ce4799823a27242501ab42584d9`；分支 `codex/solverpy-path-cover-clean`。
- macOS Darwin 27.0.0 arm64 / zsh，Conda `apsgo_v6_3.10.18`；设计 v0.14 第 20、23 节，计划 v0.48 第 8.17 节。
- 精确六文件：`neighborhoods.py`、总控与停止两份专项测试、本文、实施计划和 AGENTS。原三个算法、规则、冻结输入/输出和门槛不修改。
- 生产共新增 18 行，含一个枚举导入和 17 行总控（含分隔）；SHA-256 `c84028e2aaf2b563de8fa80bd99fb40232985ac3c043bd62bae625c59e80914d`。

## 已实现边界

`run_local_search()` 先校验现有状态与上下文，然后固定执行整链调整、单订单移动、虚拟填充。三个入口使用同一状态、材料工厂、缓存、预算、截止与取消信号；不复制状态、不归零计数、不随机、不回跳。

阶段开始前的零计数探测同时阻止上一阶段留下的停止原因，并响应阶段间新取消或截止；第三阶段返回后再探测一次，只有仍可搜索才写入 `LOCAL_SEARCH_COMPLETE`。异常原样向上抛出，不伪造正常完成。重复调用已自然完成或已截断的预算都不自动复活。

本项只签发一轮局部搜索自然结束。后续功能 17 的明确阶段拥有者须仅撤销首轮自然完成标记，保留数量/时间/取消等真实停止与全部预算，才能按计划进入拆单。拆单关闭或零接受也须正确恢复自然完成；有接受才完整重放一次。该后续接线未在本项提前实现，不把当前三个邻域结果当成最终业务发布。

## 实际总控与已验证阶段对照

主代理命令从实际构造的 31 条初始链调用新总控；阶段观察器只委托原函数并记录边界，不提供替代搜索。不以末态夹具代替任何一个邻域，也不重新修改已冻结的阶段金样。

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
'
```

实际对照通过，复现功能 13～15 已独立复验的三个边界：

| 已完成阶段 | 累计候选检查 | 累计完整评价 | 累计接受 |
|---|---:|---:|---:|
| 整链调整 | 2539 | 366 | 9 |
| 单订单移动 | 81823 | 390 | 24 |
| 虚拟填充 | 86413 | 884 | 35 |

最终方案为 `cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86`，合并轨迹为 `98836d09c4e96c1dbb4f1b1652f707e48c96692627a3dc8fea4090d2b16faf09`；自然结束原因为 `local_search_complete`。原始规则的 40 次接受历史及已批准规则差异，沿用[功能 15 三方证据](../function_15_virtual_weight_fill/verification.md)，本项不机械改写或重建原始基线。

## 验证状态

生产独立只读复核未发现具体缺陷，既有预算/取消/候选/架构回归 167 项通过（0.51 秒）。共享专项 30 项（40.85 秒）、聚焦 556 项（89.64 秒）、累计 2032 项（91.76 秒）通过；82 文件静态/格式与残留保护通过。独立实际总控命令退出码 0，约 39.88 秒仅为单轮局部搜索诊断，不是完整运行性能样本。两份新测试分别 4 项总控与 26 项停止，SHA-256 分别 `b4b26642341ce3a2a1ce9c01a2e89ad9433f37808b2192517939cab2546610fd`、`a4be34a250ff8f5c839f2d5053c3a6853bc2ba065f395d77a7e1f0a8a285e2ad`。

共享命令如下，全部退出码 0。首轮导出树 `84067aa6fd5b123bc0cc3ccc908fc3bba3265cd3`，目录 `/tmp/apsgo-orchestration-initial-kOcbq3`；专项 30 项（40.43 秒）、聚焦 556 项（89.30 秒）、累计 2032 项（91.21 秒）通过，82 文件静态/格式、残留保护、实际总控命令及编译全部退出码 0。补齐本记录后再次验证最终暂存树的相同命令集合，通过才按精确白名单提交；最终身份由 Git 历史及下一项入口记录，不在本提交自引用。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_local_search_orchestration.py tests/core/search/test_local_search_stop_semantics.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# Only in the clean export:
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```

不包含拆单、最终审计、应用组装或正式质量/性能验收；当前前三邻域已知仍有一项禁止违规和一条欠重链。正式 GQGA4 门槛仍为最多 22 链、零禁止违规、零欠重，以及 20 个完整运行样本中位数/第 95 百分位均不超过 180 秒。
