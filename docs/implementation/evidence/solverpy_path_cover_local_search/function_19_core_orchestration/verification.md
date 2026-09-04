# 功能 19：核心求解总控与释放契约

## 实施基线

- 基线 `d79eca6855bb8151f8ba9abd77be3c4cfed77944`，树 `c471f23fed8a5deba020011191064f2feb3b8aa2`，分支 `codex/solverpy-path-cover-clean`。
- macOS Darwin 27.0.0 arm64 / zsh，Conda `apsgo_v6_3.10.18`；权威设计 v0.14 第 13.3、13.4、23、26、31 节及当前计划 v0.51 第 8.20 节。
- 精确八文件：`core/solver.py`、`core/contracts.py`、三份计划总控测试、本文、实施计划与 AGENTS。已验证四个新生产/测试路径不存在，契约文件属于跟踪树，开始残留检查通过。
- 功能 18 最终干净导出专项 99、聚焦 756、累计 2232 项均通过，独立审计耗时约 0.076 秒；仍有两条欠重链，零欠重基准门槛尚未通过。

## 接线边界

复用既有初始方案评价、预算、缓存、固定局部搜索、受控拆单及独立审计。拆单入口唯一拥有一次必要重放；总控不重复执行。末态已经规范化，冻结快照送审，不重写评价或改内部身份。

直接核心调用只校验已标准化资格，不进行第二次输入转换；规则配置加载及应用结果组装留功能 20。核心结果签发核对三方拆单计数与审计身份，资源事实保持同一对象，硬截止或取消不释放。确定性身份排除耗时。

## 实现与独立复核

- 唯一 `core.solver.solve()` 串接真实构图、匹配、初始构造、首轮局部搜索、受控拆单及其内部唯一重放、无缓存审计与签发。初始评价不重复执行，不重置共享预算、缓存或接受计数。
- 新不可变 `SolverResult` 保留诊断快照、审计、指标、轨迹与身份；签发核对问题/规则绑定、实际评价与事实内容，以及搜索状态/指标/审计三方拆单计数。资源事实仍是功能 18 生成的同一个对象。
- 输入资格错误为 `FAILED/INPUT_INVALID`，字段路径明确；纯业务禁止为完整但不可发布；结构、授权、评价或签发身份错误为系统失败。取消和最终硬截止在审计、释放构造、结果构造后均再次阻止导出。
- 发布身份使用方案、评价、事实、核心审计四个已存在身份；核心结果身份覆盖载体全部字段，但剔除指标中的阶段耗时。没有新增身份框架、规则重评或事实重派生。
- 三份专项共 133 项：编排 19、状态与签发 92、确定性与载体 22。检查真实小样本拆单、单次重放、全部中断阶段、签发后停止、九个三方计数字段、事实同对象、身份污染及三次重复运行。

### 冻结源文件

| 文件 | SHA-256 |
|---|---|
| `core/solver.py` | `647e6584d58ad143198d1e687b6c03d9cf82c50a9c5564de392bd7271d264839` |
| `core/contracts.py` | `dfc8f02455656cd07e803ee6f475b6247101cef7900ebb35b83abe909e8e5f2d` |
| `test_solver_orchestration.py` | `d01e02e47f6bf7dd5d25b4fce2300d3e304efca1a6385e40cfe31770ac74b80b` |
| `test_solver_status_matrix.py` | `2d5c6646934ded83ae06fdac07664c7c7dee2c290b6434b477ee022a5919120c` |
| `test_solver_determinism.py` | `38ba95c1e58f41786a8bad90e67e2c13e8f93daa252afcd918613a8a01bc089f` |

## 真实完整核心入口

本次从冻结输入经既有标准化器加载，调用真正 `core.solver.solve()`；不是从冻结图或中间方案开始。固定种子 590531、100000 次候选检查，以固定单调时钟隔离时间截断，实际阶段耗时单独测量。公开应用服务尚未接线。

| 检查 | 实际值 |
|---|---|
| 构图检查 / 允许边 | 140715 / 32536 |
| 匹配边 / 路径 / 初始链 | 514 / 17 / 31 |
| 候选检查 / 完整候选评价 / 接受动作 | 100000 / 1039 / 41 |
| 拆单总数 / 同期 / 未来借入归还 | 2 / 2 / 0 |
| 最终质量 | `(0, 0, 2, 204.42, 22, 400)` |
| 真实重量 / 虚拟重量 / 借用统计 | 29333.91 / 400 / 22860.88 吨 |
| 状态 / 停止 | 允许欠重偏差发布 / 候选额度耗尽 |

与功能 17、18 阶段结果及审计完全一致；两个原料拆分均按拆前实际期判为同期，未来来源不直接等于“未来借入归还”模式。

| 身份 | SHA-256 |
|---|---|
| 方案 | `fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498` |
| 轨迹 | `11f3ce5755b71fd71631c2ffec506fa04b9ed2241cf448d1e69f82e0ba794585` |
| 评价 | `2358236bf600aaeeca0cd0e779737abd711a75182075c8e341a02c1a63862fd4` |
| 资源事实 | `0c9ed8ba25b3f6293d4a479b329aaace94b63347379df8a3a0de4cac2d121951` |
| 核心审计 | `727ca5b348357742b5d1d49926c13b5b1de4a885eeac509328b8097d8e322225` |
| 核心释放 | `fb032769a08db3ddd5c1b842d45c45abc5bc690866636b7b28748f9369d782a0` |
| 核心结果（排除耗时） | `f510e6f744f601c8670c8a92d878f6a39681d10cdb13e854ff7d53f88049e400` |

共享树本次核心耗时 64.0046 秒；该值不是功能 21 的独立进程全流程性能样本。正式 GQGA4 零欠重门槛仍不通过，原始参考脚本、配置和门槛均未修改。

## 验证与提交状态

| 检查范围 | 共享树 | 初验干净导出 |
|---|---|---|
| 专项 | 133 通过 / 0.78 秒 | 133 通过 / 0.80 秒 |
| 聚焦 | 889 通过 / 180.99 秒 | 889 通过 / 180.82 秒 |
| 仓内累计 | 2365 通过 / 183.91 秒 | 2365 通过 / 183.54 秒 |
| 静态及格式 | 94 文件通过 | 94 文件通过 |
| 残留保护 | 通过 | 通过 |
| 真正核心入口 | 64.0046 秒，全部断言通过 | 64.2432 秒，全部断言通过 |
| 编译 | 未在共享树执行 | 通过 |

以上退出码均为 0。初验暂存树 `86a9325ab9fbe47e0990c1f4992ce8e9c4cd4094`，新导出 `/tmp/apsgo-core-initial-lq5PUc`。两次核心运行的方案、轨迹、评价、资源、审计、释放和核心结果身份全部相同，只有观测耗时不同。

本记录补齐后再次导出最终暂存树，重复相同检查（含编译及真正核心入口）；只有全部通过且精确八文件无额外变动才提交。最终树及提交身份由 Git 历史和下一项进入记录标识，不在本文自引用。正式 GQGA4 质量与全流程性能验收未完成。

### 实际命令

以下命令在共享树与两次暂存树导出中相同；编译只在导出树执行。独立测试进程并行以减少等待，运行秒数不作为性能验收样本。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_solver_orchestration.py tests/core/test_solver_status_matrix.py tests/core/test_solver_determinism.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_solver_orchestration.py tests/core/test_solver_status_matrix.py tests/core/test_solver_determinism.py tests/core/audit tests/core/search tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
git diff --check
```

真实核心入口探针使用上述 Python 环境的 `python -c`，完整脚本如下，不写共享树产物：

```python
import json,sys,time
from decimal import Decimal as D
from pathlib import Path
sys.path.insert(0,"src")
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import SolverPolicy,SolveStatus,SearchStopReason,fingerprint
from apsgo_scheduler.core.solver import solve
from tests.app.test_input_normalizer import gqga4_request,gqga4_spec
spec=gqga4_spec.__wrapped__()
rules=load_rule_set(spec)
problem=normalize_input(gqga4_request.__wrapped__(spec),rules)
policy=SolverPolicy(**json.loads(Path("tests/baselines/gqga4/gqga4_solver_policy.json").read_text(),parse_float=D))
runtime=SolveRuntimeBudget.from_policy(policy,0,clock=lambda:1.0)
before=fingerprint((problem,rules,policy))
started=time.monotonic()
result=solve(problem,rules,policy,runtime)
elapsed=time.monotonic()-started
assert fingerprint((problem,rules,policy))==before
assert result.status is SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION
assert result.stop_reason is runtime.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
assert result.release is not None and result.core_audit.passed and not result.issues
m=result.metrics
assert (m.graph_edge_check_count,m.graph_allowed_edge_count,m.matching_edge_count,m.path_count,m.initial_chain_count)==(140715,32536,514,17,31)
assert (m.candidate_check_count,m.complete_candidate_evaluation_count,m.accepted_move_count,m.accepted_split_count,m.accepted_same_period_split_count,m.accepted_future_borrow_return_count,m.final_chain_count)==(100000,1039,41,2,2,0,22)
assert result.release.canonical_plan is result.diagnostic_candidate.plan
assert result.release.plan_fingerprint=="fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498"
assert fingerprint(result.trace)=="11f3ce5755b71fd71631c2ffec506fa04b9ed2241cf448d1e69f82e0ba794585"
assert result.core_audit.report_fingerprint=="727ca5b348357742b5d1d49926c13b5b1de4a885eeac509328b8097d8e322225"
assert result.release.resource_fingerprint=="0c9ed8ba25b3f6293d4a479b329aaace94b63347379df8a3a0de4cac2d121951"
assert result.release.evaluation_fingerprint=="2358236bf600aaeeca0cd0e779737abd711a75182075c8e341a02c1a63862fd4"
assert result.release.audited_evaluation.quality_key==(0,D(0),2,D("204.42"),22,D(400))
assert result.release.resource_facts.input_real_weight==result.release.resource_facts.scheduled_real_weight==D("29333.91")
assert result.release.resource_facts.generated_virtual_weight==D(400)
assert result.release.resource_facts.borrowed_future_weight==D("22860.88")
assert (result.core_audit.audited_split_count,result.core_audit.audited_same_period_split_count,result.core_audit.audited_future_borrow_return_count)==(2,2,0)
print(json.dumps(dict(actual_core_entry_all_algorithm_stages=True,status=result.status.value,stop_reason=result.stop_reason.value,graph_checks=m.graph_edge_check_count,allowed_edges=m.graph_allowed_edge_count,matching_edges=m.matching_edge_count,paths=m.path_count,initial_chains=m.initial_chain_count,candidate_checks=m.candidate_check_count,complete_evaluations=m.complete_candidate_evaluation_count,accepted=m.accepted_move_count,splits=m.accepted_split_count,same=m.accepted_same_period_split_count,future=m.accepted_future_borrow_return_count,final_chains=m.final_chain_count,quality=[str(v)for v in result.release.audited_evaluation.quality_key],plan_fingerprint=result.release.plan_fingerprint,trace_fingerprint=fingerprint(result.trace),core_audit_fingerprint=result.core_audit.report_fingerprint,resource_fingerprint=result.release.resource_fingerprint,core_release_fingerprint=result.release.release_fingerprint,core_result_fingerprint=result.core_result_fingerprint,stage_seconds={k:str(v)for k,v in m.stage_duration_seconds.items()},core_seconds=elapsed,application_service_implemented=False,full_gqga4_quality_gate_passed=False)))

```
