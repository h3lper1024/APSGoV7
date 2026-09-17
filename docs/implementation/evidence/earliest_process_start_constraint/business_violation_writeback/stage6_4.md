# 阶段 6.4：业务违规回写集中验证与真实对照

## 范围和提交

- 平台：macOS / Darwin，Conda `aps_3.10.18`。后端分支 `codex/earliest-process-start-constraint`，本项开始 `0e5788f`；前端 `master@62f115b`。
- 6.1 `48e77e8`：核心独立正确性审核、最早开工阻断数和三种可回写状态，精确树 467 项通过，178.58 秒。
- 6.2 `0e5788f`：月计划完整订单/日期、业务明细和诊断，精确树 112 项通过，155.70 秒。
- 6.3 前端 `62f115b`：共享树及精确树各 56 项、构建和隔离浏览器通过；订单和日期均先完整校验再更新当前步骤，六节点提前及错误日期不修改任何步骤快照。
- 本项不修改生产代码、规则、九项排序、搜索动作或预算。只同步相关测试预期、验收工具和本专项文档/证据。正式库、YAML、用户 8001 服务不操作；隔离 5197 浏览器服务已关闭。

## 集中验证

最终提交 **`ade81b6`**，精确树 `1f2893bd5738727b78b4ffe8586e5a3213402951`，导出 `/tmp/apsgo-writeback-stage64-final.t0epmR`：同进程预热后，下列五目录 **4640 项全部通过，293.92 秒，退出 0**。干净导出残留检查、4 份文档的 UTF-8/围栏和 111 个本地链接检查均通过。前一精确快照为 4639 通过 / 1 个旧夹具失败、284.71 秒，下文保留定位和修订记录；不将最终通过覆盖中途失败事实。

首轮使用“先给预热测试文件，再给其父目录”的 pytest 参数时，pytest 实际只收集应用目录中的该文件，遗漏其余 735 项应用测试。因此 **3898 项通过、2 项失败，430.16 秒** 只能作为部分回归，不称五目录全量。两项失败来自 `test_solver_orchestration.py` / `test_solver_process_logging.py` 的旧业务违规状态预期；改为新状态并补充正确性通过、阻断为零和存在正式结果的断言，未改搜索或日志语义。

上述两文件和新真实对照工具测试合计 **58 项通过，1.24 秒，退出 0**。工具只读取冻结输入和实际产物，复用既有逐节点时间/重量/用时守恒核对和 CSV 导出，增加与旧启用组的完整方案、评价、九项评分、计数和轨迹比较。工具测试先出现一次测试辅助 JSON 改写丢失小数精度的问题，改用项目精确 JSON 编解码后 5 项通过；未改真实输入或评价值。

补齐应用目录后的首次精确快照验证又发现 `test_backlog_search_comparison.py` 的历史诊断小例依赖“窄钢违规必定不可发布”。保留该测试的诊断/伪造检查目的，改为实际审核后模拟收尾时间耗尽，经正常核心结果路径生成不可发布状态；不把已有 JSON 的发布标记手改为假。该文件 **30 项通过，1.13 秒，退出 0**，生产及旧比较工具均未修改。最终精确树包含此夹具修订，完整结果见提交正文；中途失败不隐去。

最终验证改为同进程单独调用 7 单公共求解预热，再由 pytest 只收集五个目录；**收集清单为 4640 项**。实际精确树运行结果记入本项提交正文，不以收集数代替通过数：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python - <<'PY'
import pytest
from apsgo_scheduler.app.service import solve_request
from tests.app.test_earliest_process_start_flow import request
warm = solve_request(request())
assert warm.release is not None
print('warmup complete; collecting all five test directories', flush=True)
raise SystemExit(pytest.main(['-p', 'no:cacheprovider', 'tests/architecture',
    'tests/api', 'tests/app', 'tests/core', 'tests/service', '-q', '--tb=short']))
PY
```

前端精确树 `c5c798df1aac6058b53a492469d46b469ba544a2` 在 `/tmp/apsgo-web-writeback-stage63.jVD1K1` 执行 `npm test`、`npm run build` 和 `tests/browser.delivery-start.mjs`；56 项通过、构建通过，保留既有大块体积提示。浏览器使用模拟接口而非现场服务，真实 HTTP 契约由后端临时库测试覆盖。普通业务违规明细、正式日期和当前步骤导出验证通过；两个拒绝场景的步骤快照全部保持。

## 真实对照方法

- 原对照只读目录：`../real_20260601/`，历史提交 `d7de53c`。本目录的 `enabled_request.json`、`enabled_http_request.json`、`lower_bounds.json` 直接复制冻结字节，不重新读取活动库。
- 531 原单、29333.91 吨；开始 `2026-06-01T00:00:00+08:00`，种子 590531，400000 候选，总时限 310 秒、收尾 10 秒，启用组冻结规则不变。
- 请求 SHA256：`d2cc549df89ec5549b4a0e1fe10ebd2bb983c4c27a02fc4d7a87f8e37eea2a9d`；下界 SHA256：`b5a34a356051c44ddeb653af21fe05e8a1c88247fa8104d6e1182fba65d23b5c`。
- 实际生产代码从 6.2 精确树 `99910e9273303ad30d224d9930809b472d46eb96` 的干净导出 `/tmp/apsgo-writeback-stage62.O14JmO` 加载。真实求解期间没有并行运行完整测试；单次耗时仅为观察值，不是性能验收。

在后端根目录执行（重新验证时用全新的输出根目录，不覆盖本证据）：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/verify_earliest_start_real_data.py run \
  --code-root /tmp/apsgo-writeback-stage62.O14JmO \
  --request docs/implementation/evidence/earliest_process_start_constraint/business_violation_writeback/enabled_request.json \
  --output docs/implementation/evidence/earliest_process_start_constraint/business_violation_writeback/enabled

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/verify_earliest_start_real_data.py summarize \
  --baseline docs/implementation/evidence/earliest_process_start_constraint/real_20260601 \
  --output docs/implementation/evidence/earliest_process_start_constraint/business_violation_writeback
```

## 真实结果与后续边界

完整运行及汇总均退出 0。与旧启用组比较，**完整方案、完整评价（包括全部违规）、九项评分、所有计数、搜索轨迹、停止原因、完整交期报告全部一致**，方案差异列表为空。轨迹摘要仍为 `83467facc462e8039d0c5e3416f4a16848fdde0753c2e07cb6c7bdd8a15dbf29`。核心审核新增字段和对应结果/审核指纹发生预期变化，不称整个响应字节一致。

| 九项目标（按当前冻结优先级） | 旧启用组 | 本次复跑 |
|---|---:|---:|
| 1. 禁止违规数 | 6 | 6 |
| 2. 禁止严重度 | 1138193.281 | 1138193.281 |
| 3. 欠重链数 | 3 | 3 |
| 4. 欠重总缺口 / 吨 | 306.58 | 306.58 |
| 5. 旧欠全部完成 / 小时 | 574.45 | 574.45 |
| 6. 加权等待/延期 / 吨·小时 | 1392259.4124 | 1392259.4124 |
| 7. 链间宽差 / mm | 12528 | 12528 |
| 8. 虚拟材重量 / 吨 | 880 | 880 |
| 9. 小辊期数量 | 23 | 23 |

本月晚交仍为 **19 单 / 904.39 吨**，旧欠全部完成仍为 **6 月 24 日 22:26:59.961**。577 个排产节点（含拆片及虚拟材）完整保存，逐节点连续时间、来源重量及拆片工时守恒通过；未增加等待、移动时间原点或改变原单下界。

工作量：400000 次候选检查、43016 次完整候选评价、421 次接受、2 次拆单；停止原因仍为候选额度耗尽。本次公共求解墙钟 **147.883026 秒**、进程 CPU **147.586340 秒**；单独预热 **143.955997 秒**，两者合计约 291.84 秒，不把预热隐去当冷启动性能。旧组公共求解 153.755682 秒只是单次对照，本次不据此宣称稳定提速或性能验收通过。

审核事实：核心完成、`integrity_passed=true`、`writeback_blocking_violation_count=6`、综合 `passed=false`；没有结构或授权失败，搜索与独立评价一致。**状态仍为 `complete_not_publishable`，正式结果为空**。应用发布审核未运行，不能称双审核通过；返回层总体正确性需要两层都通过，因此也不能把核心正确性当作前端放行依据。

六个提前来源订单仍是 `0002002176-000110`、`0030124683-000030`、`0030125206-000010`、`0030124800-000060`、`0030124800-000050`、`0030124871-000070`，全部第 6 期借入第 1 期，合计 382 吨。逐单开始/下界/提前量见 [完整节点 CSV](enabled/nodes.csv) 和 [对照明细](comparison.json)。该 CSV 是**不可回写的诊断候选**，不是可发布结果。

结论：6.4 的回写行为和“不改变搜索”对照成立；正式集中测试结果以本项精确树提交记录为准。**整个阶段 6 的真实可回写验收仍不通过，阶段 7 不进入**。正确拦截不是已解决提前问题；下一步须另行确认针对尚未就绪借入订单的搜索修复范围，不能自动提高额度、插入等待或放宽最早开工规则。

## 工作区保护

- 阶段开始及真实对照完成时，SQLite SHA256 为 `6da52e57362cc6dc5bed561f952b79682535e415d7f9b5b6aed18723e90ee228`；YAML 为 `04cb4998cd3d8100a4f7f2ff9c62e10111ced4ae8f827df841507fa535c2f0c0`，本轮没有写入二者。
- 最终测试期间发现现场另有变化：YAML 修改时间为 2026-09-17 10:28:03，候选变为 `600000`、总时限变为 `999999`，摘要 `a8bfe67f504b2f9fe9b64779355adee076e97eb3806411dbc6e6dc87928e55b5`；数据库新观察摘要为 `90f7f6445573419bbdd7047e048e668ec5d6e7b593e9289e5081f0449335da77`。不推测写入来源、不回滚、不暂存/提交，未查询或改变活动规则。该观察不是数据库备份；新现场配置没有用于本次真实对照，不能混称已验证 60 万次结果。
- 前端既有六文件差异摘要仍为 `30a35d5f85ae20afd849821a5647d7693a795f76d5c3aaadb3baee3061b264b6`，未修改/提交这些用户文件。
- 共享残留检查仍只报告历史 8 个旧 V6 文件缺失，不恢复文件、不修改原清单；精确树干净导出检查另行执行。旧真实三组证据不改写，Ponytail 保持关闭。
