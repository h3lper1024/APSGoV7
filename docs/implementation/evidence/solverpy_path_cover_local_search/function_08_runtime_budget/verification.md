# 功能 8：共享求解预算与取消状态

- 实施前提交：`9114a01773e401372825380e23304d0f9dce4116`，分支 `codex/solverpy-path-cover-clean`。
- 平台：Darwin 27.0.0 arm64、zsh；Conda `apsgo_v6_3.10.18` / Python 3.10.18。
- 权威设计 v0.13，SHA256 `7c7f1985b48cc3f8de19931eb179fe96d21ae34844067dc909ce5cf1712c1891`；实施计划 v0.40 第 8.9 节。
- 六文件提交：一个生产模块、两份测试、AGENTS、实施计划及本记录。原始五输入、目标规则/策略、质量与性能门槛、参考脚本及外部 V3 不修改。

## 完成边界

共享预算从服务传入的原始单调时钟起点计算搜索和最终截止时间，不在工厂调用时重启计时。数量、取消、搜索截止按既定顺序检查；只在许可成功后累加候选检查数。零计数探测不消耗额度，零总额度不会阻止仅构造初始方案所需的探测，但第一个有计数的候选被拒绝。

初始停止原因为空；预算不会自行宣布自然完成。搜索一旦停止不能因时钟回退或后续零计数探测重启。数量/搜索截止后可以在预留时间内收尾；取消或最终硬截止、输入/系统错误后不能恢复为允许收尾。取消探针必须返回布尔值，异常透传给上层，不伪装成搜索完成。已有公开结果契约继续拒绝空停止原因及取消/硬截止后的释放。

只有预算协议与安全点组合测试；尚未接入真实候选枚举、完整搜索或发布流程。候选许可后发生取消时，测试确认已消费检查数保留、完整评价未调用；这不是完整搜索集成验收。使用标准库可调用时钟做确定性测试，不新增时钟框架、依赖或实际休眠。

## 共享树验证

| 范围 | 结果 |
|---|---|
| 预算及取消专项 | 89 项，0.13 秒 |
| 加公开结果状态矩阵 | 137 项，0.17 秒 |
| 仓内累计 | 1583 项，1.81 秒 |
| Ruff / 格式 | 59 文件通过 |
| 残留保护 | 通过，16 个稳定残留；IDE 不跟踪、不导出 |

均退出 0。两份测试及生产模块由不同执行者交叉复核，无未关闭实质缺陷。开发期测试首次收集时预算模块尚未落盘而失败，模块完成后重新执行通过；无规则、断言门槛或已有测试被放宽。

```bash
# 专项
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py -q
# 聚焦
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_runtime_budget.py tests/core/test_cancellation_points.py tests/api/test_result_status_matrix.py -q
# 仓内累计
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --check
```

## 原始参考计数对照

只读执行原始 `SearchBudget`，与目标预算在总额度 0、1、4、20 下进行共 22 次许可调用。所有允许/拒绝及累加计数相同，拒绝不增量；执行前后参考源码 SHA256 均为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。没有运行搜索或生成排程结果。

目标初始停止原因为 None，原脚本预写 search_complete；这是设计已冻结的修正。取消和独立收尾截止属于目标安全控制，不能声称与没有这些能力的参考脚本完全等价；由可控时钟金样验证。参考探针时间预算足够长，不以真实等待验证时间边界。

| 额度 | 许可输入 / 结果 / 累计数 |
|---|---|
| 0 | 0 / 允许 / 0；1 / 拒绝 / 0 |
| 1 | 0 / 允许 / 0；1 / 允许 / 1；0 / 允许 / 1；1 / 拒绝 / 1 |
| 4 | 0、1、0、1、2、0 允许，累计 4；再请求 4 拒绝、仍为 4 |
| 20 | 0、1、0、1、2、0、4、8 允许，累计 16；再请求 16 拒绝、仍为 16 |

从仓根或干净导出根执行以下命令：

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import hashlib,json,runpy,sys,time
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0,"src")
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import SolverPolicy,SearchStopReason,CONSTRUCTION_ORDER_KEY,NUMERIC_SEMANTICS_KEY
manifest=json.loads(Path("tests/baselines/gqga4/reference_manifest.json").read_text())
source=Path(manifest["script"]["path"])
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
r=runpy.run_path(str(source),run_name="reference_budget_probe")
rows=[]
for limit in (0,1,4,20):
 policy=SolverPolicy(1,D(3600),D(10),limit,CONSTRUCTION_ORDER_KEY,NUMERIC_SEMANTICS_KEY,D(40))
 target=SolveRuntimeBudget.from_policy(policy,time.monotonic())
 reference=r["SearchBudget"](target.search_deadline_monotonic,limit)
 assert target.stop_reason is None and reference.stop_reason=="search_complete"
 trace=[]
 for count in (0,1,0,1,2,0,4,8,16):
  actual=target.permit(count); expected=reference.permit(count)
  assert actual==expected and target.candidate_check_count==reference.candidate_checks
  trace.append([count,actual,target.candidate_check_count])
  if not actual:
   assert target.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
   assert reference.stop_reason=="candidate_check_budget_exhausted"
   break
 rows.append({"limit":limit,"trace":trace})
assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest["script"]["sha256"]
print(json.dumps({"script_sha256":manifest["script"]["sha256"],"cases":rows,"search_executed":False,"unexpected_count_differences":0}))
'
```

## 干净导出与提交

初验树 `d259a6c58e187505e1385add2aa2b0c34db58f70`，导出 `/tmp/apsgo-budget-initial-IgOP0n`。专项 89 项（0.11 秒）、聚焦 137 项（0.16 秒）、仓内累计 1583 项（1.76 秒）均通过；59 文件静态/格式、干净导出残留保护、上述四额度 22 次参考许可对照均退出 0。

仅在导出执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools`，退出 0。补齐本记录后再导出最终暂存树，按同样集合复测，提交前核对共享树保护、六文件暂存白名单及提交树身份。Git 差异检查在共享工作区执行，不在没有 Git 元数据的导出目录执行。提交标识由 Git 历史记录，不在本文件自引用。

本项不是 GQGA4 主求解或质量/性能验收；后续功能 9 为规则连接缓存与稳定构图。
