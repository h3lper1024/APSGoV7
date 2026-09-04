# 功能 18：核心独立审计与唯一资源事实

## 实施基线与边界

- 基线 `fd32e4918144c8c7e4619fd2552ee0f9c7a47fbd`，树 `3b12195296dcf3fe1e8e26e997a1310fa065298e`，分支 `codex/solverpy-path-cover-clean`。
- macOS Darwin 27.0.0 arm64 / zsh；Conda `apsgo_v6_3.10.18`；设计 v0.14 第 10.2.2、13.4、21、22、25.1/25.2、32.7 节，实施计划 v0.50 第 8.19 节。
- 精确八文件：`final_audit.py`、`resource_facts.py`、三份计划审计测试、本文、实施计划与 AGENTS。现有搜索、规则配置、原始脚本、基线和验收门槛不改。
- 已核验进入结果：531 原始订单拆后 533 真实节点、20 个虚拟节点、2 个同期间分区；搜索质量 `(0,0,2,204.42,22,400)`，最终计划指纹 `fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498`。本项必须独立核对，不能把先前搜索结论作为审计通过依据。

## 关键接线

使用已有 `CoreCandidateSnapshot` 同时携带方案和待比较搜索评价，不接搜索状态或缓存。入口返回小型不可变 `CoreAuditOutcome`，包含报告、独立评价、唯一资源事实及定位诊断；释放身份留功能 19 三方计数核对后签发。

片段可以分布于授权期内的不同链；虚拟温度保留原物化值，不能按最终邻居重新推算。分区按接受序号重授权，覆盖、重量、谱系、资源来源和全部启用规则独立重算。只有链重下限允许偏差，禁止项不能借允许原因绕过。最终资源事实只由核心审计调用私有组装入口生成。

## 实现与审查范围

- 独立覆盖检查包括输入节点唯一、真实来源精确守恒、未知计划期、链期规范化和完整分区；真实原单属性不得静默改变。
- 按接受序号从原始父订单重放两种拆单模式的授权；复用已有纯分片算法与分区身份算法，不另写一套授权。隔离节点按生成序号稳定排序。
- 复用已有完整评价及作用域调度；内部跨虚拟材真实端点规则仍执行。链身份、摘要、原始指标、违规多重集合和质量键分别比较，不将评价集合的偶然顺序当作差异。
- 最终资源事实由一个私有扫描入口生成现有五类不可变明细；仅审计调用。资源指纹覆盖全部事实字段，分区指纹覆盖既有授权语义，报告指纹绑定输入、规则、方案、搜索评价、报告正文和完整诊断。
- 不创建释放凭证，不新增文件或网络访问，不消耗候选额度；取消、硬截止及异常均不返回部分已审计材料。候选数耗尽不妨碍保留时间内完成审计。

主代理全读生产与新增测试；另有独立生产复核及两组测试编写。复核发现：如果内部对象被污染为倒置虚拟温区，且温度规则停用，仅检查浮点有限性不足。已复用领域模型的 `validate_dimensions()` 独立检查，不按最终邻居重新生成温度。

## 可复现命令

以下命令在仓库根或对应干净导出根执行；共享目录禁用字节码和 pytest 缓存，编译仅在导出中执行。实际 GQGA4 连续探测从已验证初始方案开始，真正执行首轮、拆单、一次重放再审计；固定时钟仅隔离确定性候选预算，不作为完整墙钟性能样本。这里没有重新运行无改动的外部参考脚本，原始对照和已确认差异沿用功能 17 证据。

### specific

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/audit -q
```

### focus

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/audit tests/core/search tests/core/construction tests/core/graph tests/core/test_plan_evaluation.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
```

### repo

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
```

### static

```bash
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
```

### guard

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

### integration

```bash
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

from apsgo_scheduler.core.contracts import CoreCandidateSnapshot,CoreAuditStatus
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.model import MaterialRole
before_audit=fingerprint(state)
audit_started=time.monotonic()
outcome=audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan,state.current_evaluation),problem,cache.rule_set,runtime)
assert outcome.report.status is CoreAuditStatus.COMPLETED and outcome.report.passed
assert outcome.report.search_evaluation_matches is True
assert outcome.audited_evaluation==state.current_evaluation
facts=outcome.resource_facts
assert facts is not None
assert (outcome.report.audited_split_count,outcome.report.audited_same_period_split_count,outcome.report.audited_future_borrow_return_count)==(2,2,0)
assert len(facts.assignments)==553 and len(facts.virtual_generations)==20 and len(facts.split_partitions)==2 and len(facts.actual_transitions)==61
assert facts.input_real_weight==facts.scheduled_real_weight==D("29333.91")
assert facts.generated_virtual_weight==D(400) and facts.borrowed_future_weight==D("22860.88")
assert outcome.report.derived_resource_fingerprint==facts.facts_fingerprint
assert outcome.report.audited_evaluation_fingerprint==fingerprint(outcome.audited_evaluation)
assert runtime.candidate_check_count==100000 and runtime.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
assert fingerprint(state)==before_audit
assert not hasattr(outcome,"release")
print(json.dumps(dict(actual_initial_search_then_independent_audit=True,audit_passed=outcome.report.passed,audit_seconds=time.monotonic()-audit_started,report_fingerprint=outcome.report.report_fingerprint,facts_fingerprint=facts.facts_fingerprint,evaluation_fingerprint=outcome.report.audited_evaluation_fingerprint,assignments=len(facts.assignments),virtuals=len(facts.virtual_generations),partitions=len(facts.split_partitions),actual_transitions=len(facts.actual_transitions),real_weight=str(facts.scheduled_real_weight),virtual_weight=str(facts.generated_virtual_weight),borrowed_weight=str(facts.borrowed_future_weight),quality=[str(v) for v in outcome.audited_evaluation.quality_key],public_release_implemented=False,full_gqga4_quality_gate_passed=False)))
'
```

### compile

```bash
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```

## 源码与专项测试身份

| 文件 | SHA-256 |
|---|---|
| `core/final_audit.py` | `db5c2995a5d91f9874cabe39c8dc2db0f8b5b1ab77ea58c9ca2a29c9f8e9df94` |
| `core/resource_facts.py` | `5f3f4aeedcc38c39b47c3e89609e6e877fb641c3b1562930426d98025c826abd` |
| `test_final_audit_without_cache.py` | `4d7826aa9054f6134532c571d4fcf204e1b437c729c4c5a96d38bed233bdac8f` |
| `test_plan_derived_facts.py` | `4dafabdd589c9ce8607faf0ec5d492516b6abe7bff6e10ca56464b7ca4ea3317` |
| `test_audited_core_release.py` | `249833484240e568a5a85e6f7a1b2a73f341376ea9ddafbe58d4bf3f3b4cd101` |

专项共 99 项：独立审计 71 项；资源事实与审计材料边界合计 28 项。后者使用真正的 GQGA4 拆单及重放结果，完整从初始方案连续执行的额外探测另行记录。

## 验证记录

前置模型/架构 105 项（0.32 秒）通过。代理自测用于开发，不替代以下正式验证。

| 检查 | 共享工作树结果 | 退出码 |
|---|---|---|
| 专项 | 99 项，35.14 秒 | 0 |
| 聚焦 | 756 项，183.85 秒 | 0 |
| 仓内累计 | 2232 项，184.20 秒 | 0 |
| 静态及格式 | 全部通过，90 个文件符合格式 | 0 |
| 残留保护 | 16 个稳定残留保持；IDE 忽略边界不变 | 0 |
| 实际连续执行与审计 | 首轮、两次拆单、唯一重放、独立审计全部断言通过 | 0 |

### 实际 GQGA4 独立审计

- 首轮和拆单/重放轨迹与功能 17 完全相同；仍为 100000 次候选检查、1039 次完整候选评价、41 次接受。审计不增加计数，也不改变搜索状态。
- 重算评价与搜索评价一致；`COMPLETED / passed=True`，无结构、授权或评价一致性诊断。
- 553 条分配明细、20 个虚拟节点、61 个实际过渡材、2 个同计划期拆单分区、0 个未来归还分区。
- 输入与最终真实重均为 29333.91 吨，虚拟重 400 吨，借用重 22860.88 吨；保持最终质量 `(0,0,2,204.42,22,400)`。
- 方案指纹 `fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498`。
- 审计报告指纹 `727ca5b348357742b5d1d49926c13b5b1de4a885eeac509328b8097d8e322225`。
- 资源事实指纹 `0c9ed8ba25b3f6293d4a479b329aaace94b63347379df8a3a0de4cac2d121951`。
- 独立评价指纹 `2358236bf600aaeeca0cd0e779737abd711a75182075c8e341a02c1a63862fd4`。
- 本次独立审计 0.0788 秒，首轮加拆单/重放 62.1384 秒；都是本次分段诊断耗时，不是应用全流程性能门槛样本。

干净导出将并行运行互不依赖的测试进程，命令与断言不变；其测试耗时只用于验证记录，不作为排程性能比较。编译在测试后仅对干净导出执行。补入首轮导出结果后重新导出最终暂存树复测，再提交。

### 首轮干净暂存树复测

暂存树 `a2101a39171191d509ae6f68da0d51872738206e`，导出 `/tmp/apsgo-audit-initial-9uqdYw`。精确八文件与上述生产/测试 SHA 均核对；静态、格式、残留及编译退出 0。

| 检查 | 干净导出结果 | 退出码 |
|---|---|---|
| 专项 | 99 项，34.66 秒 | 0 |
| 聚焦 | 756 项，181.01 秒 | 0 |
| 仓内累计 | 2232 项，182.92 秒 | 0 |
| 实际连续执行与独立审计 | 候选、评价、接受、拆单、轨迹及全部方案/审计/事实指纹同共享树 | 0 |

本轮审计分段耗时 0.0761 秒；独立审计不消耗候选、不修改结果、不中途签发发布。补入本节后再导出最终暂存树执行同一命令集合；该最终树和正式提交身份由 Git 及下一项进入记录引用，不在自身内容里自引用。

## 验收边界

正式 GQGA4 要求欠重为零，不能把通用审计允许欠重误称为完整基准验收通过。本项不实施核心总控、发布签发、应用入口或全流程 20 次性能验收。
