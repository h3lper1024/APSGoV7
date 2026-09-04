# 功能 20：唯一应用入口、结果组装与契约自检

## 基线和范围

- 基线 `1bd229038320b2787a32a5180e9546694065039a`，树 `c7dc78c4d2f748623c66aa1ff1d2484842206ecc`，分支 `codex/solverpy-path-cover-clean`。
- macOS Darwin 27.0.0 arm64 / zsh；Conda `apsgo_v6_3.10.18`；设计 v0.14 第 13.5、25.3/25.4、26、31、32.7 节，计划 v0.52 第 8.21 节。
- 精确 17 文件：三个新应用模块、公开结果、三个层级包导出、三个新应用测试、唯一入口架构测试、既有标准化器及其测试、既有公开结果测试、本文/计划/AGENTS。新路径均核验不存在，已有路径属于跟踪树，开始残留检查退出 0；无关 `docs/design/assets/` 保留且不暂存。
- 功能 19 最终树专项 133（0.79 秒）、聚焦 889（180.88 秒）、累计 2365（183.17 秒）及静态、残留、编译、实际核心运行均通过；核心实际约 64.3165 秒，最终两条欠重链仍不满足正式门槛。

## 复用边界

Ponytail Lite：直接复用加载、标准化、核心入口、不可变资源事实、预算及公开结果；不新增准备对象、时钟框架或资源台账。只补安全错误聚合、草稿同对象映射、第二层映射核验和最终签发。取消及硬截止在密封后再次核对。

开发元数据使用明确算法版本与 `unversioned` 构建标识，生产不读取 Git；真实提交由本文基线和 Git 历史追溯。规则、候选顺序、质量键、原始参考输入和正式门槛不改变。

## 实现及复核

- 唯一应用入口串接既有规则加载、输入标准化、核心求解、草稿组装、第二层自检和签发；从入口时刻到最终结果构造后使用同一预算。没有读取本机 Git、输入文件或旧 V6 的生产旁路。
- 组装复用核心方案、评价、资源事实和诊断快照；第二层只核对请求绑定、五类事实映射、重量闭环、来源分区及三方拆单计数，不重新评价规则、重新授权或派生台账。
- 草稿与公开身份排除观测耗时，实际内容绑定到两层报告；取消、硬截止、错误类型、映射损坏和未完成审计均不能发布。核心搜索评价与最终审计评价允许既有语义等价的记录排序差异，未强制整个对象字面相等。
- 独立反例发现并修正内部步骤错误返回类型导致异常整理再次抛错的问题；所有六个步骤先检查返回类型。另修正异常结果构造期间收到取消时的最终状态：构造后只再检查取消一次，不重试坏时钟。
- 新/扩展专项 332 项：结果组装 49、第二层自检 61、应用服务 55、输入标准化 85、公开结果 63、唯一入口 19。累计相对功能 19 增加 202 项。

### 冻结代码和测试

| 文件 | SHA-256 |
|---|---|
| `src/apsgo_scheduler/app/result_assembler.py` | `b3af18153d2f37b1a5090daa8142fe458c938fc73c1306e90aef651613ea66b7` |
| `src/apsgo_scheduler/app/contract_audit.py` | `0471c557d5cf8f728456eefcc8799d46f23115a21e9d8f15a6977b55b01cb82f` |
| `src/apsgo_scheduler/app/service.py` | `4345d7bb3db83c9683931ae9f7eb03cd46b72bac7d2c8650972823181699690f` |
| `src/apsgo_scheduler/api/result.py` | `73b7dc5f5d51936d4bb3afdcbd451b422161fc8f56245f9b1f9a64bf8e721002` |
| `src/apsgo_scheduler/app/__init__.py` | `09c00e292b82e35959e24f1f34f7daf3c20db8b539eefdda696a0685a6924a14` |
| `src/apsgo_scheduler/api/__init__.py` | `a4f159942bef352aedbdfae112863d75c87d3c66b1aede57cf66b18adecdc895` |
| `src/apsgo_scheduler/core/__init__.py` | `00ce7d57c7a019ce3ee326e2ce81a56f7503a2c1ba70d990fd52b8430a8c4bb2` |
| `tests/app/test_result_assembler.py` | `fe130be5d0160680a1f5611c23f350611d6343bfd8b8313b34ff188456182165` |
| `tests/app/test_result_contract_audit.py` | `73446732a32539b3640dc35af3f3dc68bff781213f26f76c87e5f00a778c9289` |
| `tests/app/test_solve_request_service.py` | `5c27bcf1480b9a165a0f2ee1d26fb2da089c02616469fc8a2b7d76ba32cf3b28` |
| `tests/architecture/test_single_public_solver_entry.py` | `32126f0f6eb483532f5fa1111d4040a1bf2083654727af373a030b9e075b3b95` |
| `src/apsgo_scheduler/app/input_normalizer.py` | `bfa18162f2e80c9cf63fb518d31b52f8c99f9c14bee79ac268e2f78a510d7445` |
| `tests/app/test_input_normalizer.py` | `a4fa210b15d60994e7d718e2bb3b4cb7a5c2517d4d6ab0f7a54a850ad1bb56a5` |
| `tests/api/test_result_status_matrix.py` | `6532584d0f5c4b2fd67cdb38f524113de71d9d05f823b892651101a81e846805` |

## 真实公开入口执行

使用冻结五输入映射、正式规则与策略，调用真正 `app.solve_request()`，从入口开始真实单调时钟；非固定时钟、非模拟核心。完整构图/匹配/初始方案/搜索/拆单/重放/双审计/签发均执行。原请求执行前后身份不变。

| 项目 | 实际值 |
|---|---|
| 构图检查 / 允许边 | 140715 / 32536 |
| 匹配边 / 初始路径 / 初始链 | 514 / 17 / 31 |
| 候选检查 / 完整候选评价 / 接受动作 | 100000 / 1039 / 41 |
| 拆单 / 同期 / 未来借入归还 | 2 / 2 / 0 |
| 六级质量 | (0, 0, 2, 204.42, 22, 400) |
| 真实重量 / 虚拟重量 | 29333.91 / 400 吨 |
| 最终状态 | 允许欠重偏差发布，需业务确认；候选额度耗尽 |
| 两层审计 | 均完成、通过；无接口诊断 |
| 本次共享树服务耗时 | 62.952076667 秒 |
| 入口至第二层自检完成 | 62.74783999999636 秒 |

方案、轨迹、评价、资源事实与核心审计均保持功能 17～19 的已验证身份。第二次拆分依据拆前实际期判为同期，不把未来来源直接当作未来拆分模式。**两条欠重链 / 204.42 吨仍不满足正式 GQGA4 零欠重门槛**；允许偏差发布不等于基准验收通过，单次服务耗时也不是独立进程成对性能样本。

完整运行输出（耗时只作本次观测）：

```json
{
  "actual_public_service": true,
  "real_budget_clock": true,
  "status": "publishable_with_allowed_deviation",
  "stop_reason": "candidate_limit_reached",
  "confirmation_required": true,
  "quality": [
    "0",
    "0.0",
    "2",
    "204.42",
    "22",
    "400.0"
  ],
  "counters": {
    "graph_edge_check_count": 140715,
    "graph_allowed_edge_count": 32536,
    "matching_edge_count": 514,
    "path_count": 17,
    "initial_chain_count": 31,
    "candidate_check_count": 100000,
    "complete_candidate_evaluation_count": 1039,
    "accepted_move_count": 41,
    "accepted_split_count": 2,
    "accepted_same_period_split_count": 2,
    "accepted_future_borrow_return_count": 0,
    "final_chain_count": 22
  },
  "request_fingerprint": "3de804de704a5c15b8d2ff89a91c571029d5d45a7a90fbdb0ab961c233c0e791",
  "plan_fingerprint": "fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498",
  "core_audit_fingerprint": "727ca5b348357742b5d1d49926c13b5b1de4a885eeac509328b8097d8e322225",
  "core_release_fingerprint": "fb032769a08db3ddd5c1b842d45c45abc5bc690866636b7b28748f9369d782a0",
  "resource_fingerprint": "0c9ed8ba25b3f6293d4a479b329aaace94b63347379df8a3a0de4cac2d121951",
  "trace_fingerprint": "11f3ce5755b71fd71631c2ffec506fa04b9ed2241cf448d1e69f82e0ba794585",
  "result_audit_fingerprint": "df8ac5ebf357d949c33771efc5ad2abb6efef4d04e3a1873609055bb76111d14",
  "public_release_fingerprint": "7c050397613a657d2f07346c2a16b9c5498687fcdd0b2e7f92fdbec5ef6e5a88",
  "result_fingerprint": "2dbd6043de68462477b0927db63f5797a7bce6a395bfa016571daee5d368a391",
  "run_fingerprint": "f53463b37126f3f968a93d8f627192d21a496f6c9e60dabbf42fb9389ce11324",
  "stage_seconds": {
    "input_validation": "0.017679624987067655",
    "construction_graph": "2.1560869160166476",
    "minimum_path_cover": "0.47186199997668155",
    "initial_solution": "0.18572079099249095",
    "local_search": "38.36745674998383",
    "controlled_split_and_replay": "21.072964499995578",
    "core_audit": "0.05416324999532662",
    "request_preparation": "0.000056832999689504504",
    "rule_loading": "0.0006002080044709146",
    "input_normalization": "0.028819833998568356",
    "core_solve": "62.45717145802337",
    "result_assembly": "0.06533433301956393",
    "service_to_result_audit_seconds": "62.74783999999636",
    "result_contract_audit": "0.1785489579779096",
    "result_sealing": "0.12699058302678168"
  },
  "service_elapsed_seconds": 62.952076667017536,
  "formal_gqga4_quality_gate_passed": false,
  "formal_paired_performance_sample": false
}
```

## 验证状态

| 检查 | 最新共享树 |
|---|---|
| 专项 | 332 通过 / 2.15 秒 |
| 聚焦 | 915 通过 / 36.24 秒 |
| 仓内累计 | 2567 通过 / 180.30 秒 |
| 静态 / 格式 | 101 文件通过 |
| 残留保护 | 通过，稳定受保护残留无变化 |
| 实际公开入口 | 62.952076667 秒，所有断言通过 |

以上退出码全部为 0；较早共享树 331/914/2566 的运行早于异常取消修复，已被本表替代，不作为最终代码验证。独立测试进程并行，测试总耗时不充当算法性能样本。下一步对精确 17 文件暂存树进行干净导出、同范围回归、实际入口及编译；证据补齐后再次对最终树验证才提交。

初验暂存树 `8f2564a8527459defbbce0e68a6751834a5bc010`，干净导出 `/tmp/apsgo-service-initial-qvIAw3`：专项 332 项（2.27 秒）、聚焦 915 项（37.06 秒）、仓内累计 2567 项（186.70 秒）、101 文件静态/格式、残留保护、编译及真实应用入口均退出 0。真实入口 64.886565958 秒，所有语义身份与共享树相同，双层审计通过；单次运行不计正式性能样本。

本记录补齐后重新导出最终暂存树，重复全部相同检查；通过后提交。最终树与提交身份由 Git 历史和下一项记录标识，不在本文自引用。

### 实际命令

共享树和两次干净导出运行相同命令；编译仅在导出树。原始输入、配置、门槛与外部参考文件不修改。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/app/test_result_assembler.py tests/app/test_result_contract_audit.py tests/app/test_solve_request_service.py tests/app/test_input_normalizer.py tests/api/test_result_status_matrix.py tests/architecture/test_single_public_solver_entry.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core/audit tests/core/test_solver_orchestration.py tests/core/test_solver_status_matrix.py tests/core/test_solver_determinism.py tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
git diff --check
```

完整入口探针使用同环境的 `python -c`，代码如下；不写共享树排程产物：

```python
import json,sys,time
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
sys.path.insert(0,"src")
from apsgo_scheduler.app import solve_request
from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.api.result import ResultAuditStatus
from apsgo_scheduler.core.contracts import SolverPolicy,SolveStatus,SearchStopReason,CoreAuditStatus
from tests.app.test_input_normalizer import gqga4_spec,gqga4_request
spec=gqga4_spec.__wrapped__()
request=gqga4_request.__wrapped__(spec)
policy=SolverPolicy(**json.loads(Path("tests/baselines/gqga4/gqga4_solver_policy.json").read_text(),parse_float=D))
request=replace(request,policy=policy)
before=fingerprint_public_request(request)
started=time.monotonic()
result=solve_request(request)
elapsed=time.monotonic()-started
assert fingerprint_public_request(request)==before
assert result.status is SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION,result.issues
assert result.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
assert result.release is not None and result.confirmation_required
assert result.core_audit.status is CoreAuditStatus.COMPLETED and result.core_audit.passed
assert result.audit_report.status is ResultAuditStatus.COMPLETED and result.audit_report.passed
assert not result.issues
m=result.run_manifest
assert m.request_fingerprint==before
assert m.problem_fingerprint=="cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea"
assert m.rule_set_fingerprint=="420cd13d59763c140b23664f0cb0aca0437e0680b51899d2e0fb39563b7f5365"
assert m.counters["candidate_check_count"]==100000 and m.counters["accepted_move_count"]==41
assert m.counters["complete_candidate_evaluation_count"]==1039
assert (m.counters["accepted_split_count"],m.counters["accepted_same_period_split_count"],m.counters["accepted_future_borrow_return_count"])==(2,2,0)
assert m.trace_fingerprint=="11f3ce5755b71fd71631c2ffec506fa04b9ed2241cf448d1e69f82e0ba794585"
assert result.core_audit.report_fingerprint=="727ca5b348357742b5d1d49926c13b5b1de4a885eeac509328b8097d8e322225"
assert result.audit_report.plan_fingerprint=="fa30b09955a46827d854e54c3df30760efc3c6784cfe77e3ed9eb009af59b498"
assert result.release.core_release_fingerprint=="fb032769a08db3ddd5c1b842d45c45abc5bc690866636b7b28748f9369d782a0"
assert result.release.resource_facts.facts_fingerprint=="0c9ed8ba25b3f6293d4a479b329aaace94b63347379df8a3a0de4cac2d121951"
assert result.release.evaluation.quality_key==(0,D(0),2,D("204.42"),22,D(400))
assert result.release.resource_facts.input_real_weight==result.release.resource_facts.scheduled_real_weight==D("29333.91")
assert not m.optimality_proven and m.search_was_truncated
print(json.dumps(dict(actual_public_service=True,real_budget_clock=True,status=result.status.value,stop_reason=result.stop_reason.value,confirmation_required=result.confirmation_required,quality=[str(v)for v in result.release.evaluation.quality_key],counters=dict(m.counters),request_fingerprint=m.request_fingerprint,plan_fingerprint=result.audit_report.plan_fingerprint,core_audit_fingerprint=result.core_audit.report_fingerprint,core_release_fingerprint=result.release.core_release_fingerprint,resource_fingerprint=result.release.resource_facts.facts_fingerprint,trace_fingerprint=m.trace_fingerprint,result_audit_fingerprint=result.audit_report.report_fingerprint,public_release_fingerprint=result.release.release_fingerprint,result_fingerprint=result.result_fingerprint,run_fingerprint=m.deterministic_run_fingerprint,stage_seconds={k:str(v)for k,v in m.stage_duration_seconds.items()},service_elapsed_seconds=elapsed,formal_gqga4_quality_gate_passed=False,formal_paired_performance_sample=False)))

```
