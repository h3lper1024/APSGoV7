# 交期目标后端完整对照

## 1. 输入与执行边界

- 生产代码：`8777993`，分支 `codex/delivery-objective-optimization`，macOS ARM64；Conda `aps_3.10.18`。
- 唯一求解入口仍为 `apsgo_scheduler.app.service.solve_request()`。没有调用 HTTP、修改前端、写正式数据库或部署服务。
- 原请求：`diagnostics/numpy_numba_virtual_bridge/stage_04_latest/prepared_request.json`；来源与哈希见阶段 0。531 原订单、29333.91 吨、27 原型，保持原订单顺序和物理字段。
- 真实交期、炉区速度取 `/Users/miles/Desktop/GQGA4_test_new.xlsx`，SHA-256 `a9321ef3abcc307b0a96e5af7174ae22f60b61871892c489e911e5c337c09ff0`；仅按原订单号提取原始属性，533 真实片段归并回 531 单，不采用工作簿排程、虚拟节点或两位显示用时。
- 起点 `2026-06-01T00:00:00+08:00`，交期当天 24 点截止，虚拟材统一 100 米/分钟；全部节点占时，拆单按最后片段完成、原订单重量计分。
- 同种子 590531、候选上限 200000、总时间 310 秒/收尾保留 10 秒。未因新结果放宽门槛；原 180 秒/20 对性能门仍保留，不在此宣称通过。
- 旧七级与新九级分别从同一原始请求求解。旧七级本次最终方案与原冻结方案逐字段相同，原评分 `(0,0,0,0,11726,660,23)` 不变。

## 2. 实际命令

先只读提取表格（bundled Python，无服务依赖新增）：

```sh
/Users/miles/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B \
  tools/extract_delivery_timing_source.py \
  --input /Users/miles/Desktop/GQGA4_test_new.xlsx \
  --output diagnostics/delivery_objective/timing_source.json
```

以下命令分别用 `old / old_01`、`delivery / delivery_01`、`delivery / delivery_02` 运行；输出目录必须不存在，不覆盖旧证据。

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/run_delivery_comparison.py \
  --prepared-request diagnostics/numpy_numba_virtual_bridge/stage_04_latest/prepared_request.json \
  --timing-source diagnostics/delivery_objective/timing_source.json \
  --start 2026-06-01T00:00:00+08:00 --virtual-speed 100 \
  --variant delivery --output-dir diagnostics/delivery_objective/delivery_01
```

每个目录保存 `input_binding.json`、`prepared_request.json`、带时间日志 `execution.log`、完整方案/评价/轨迹/双审计 `measurement.json` 与原订单/节点时间 `delivery_report.json`。大文件在忽略目录保留，仓内只提交索引及汇总工具。

## 3. 首轮观测

| 指标 | 旧七级 | 新九级首轮 |
|---|---:|---:|
| 禁止违规 | 0 | 0 |
| 欠重链数 / 缺口吨 | 0 / 0 | 1 / 41.01 |
| 本月新增晚交原订单数 | 24 | 11 |
| 本月新增晚交原订单吨位 | 844.82 | 288.89 |
| 累计等待与延期吨·小时 | 1900251.423623 | 1297489.840268 |
| 含旧欠单的总晚交原订单数 | 120 | 107 |
| 含旧欠单的总晚交吨位 | 6230.85 | 5674.92 |
| 旧欠单最后完成 | 6月26日 06:16:42 | 6月26日 07:21:26 |
| 最大实际延期小时 | 606.278364 | 607.357187 |
| 相邻链首尾宽差总和 mm | 11726 | 12151 |
| 虚拟材吨位 | 660 | 540 |
| 非空链数 | 23 | 23 |
| 候选检查 | 200000 | 164646 |
| 完整评价 | 3697 | 49844 |
| 接受动作 | 54 | 341 |
| 经过时间秒 | 33.957486 | 207.902233 |
| 实际进程 CPU 秒 | 33.640159 | 207.217377 |
| 停止原因 | 候选上限 | 自然结束 |
| 双审计 | 通过 | 通过 |

起排时旧欠单固定 96 单、5386.03 吨。新目标减少本月晚交，但旧欠单最后完成晚约 1.08 小时，最大实际延期也略增，不隐去这一取舍。

**验收结论：BEST_EFFORT，不能称整体比旧方案更优。**新方案存在 `initial-000030` 链重 658.99 吨，低于 700 的下限；这是唯一允许发布的偏差，未放过任何禁止违规，却未达到 GQGA4 零欠重验收门。新九级前四项已劣于旧方案，交期改善不能抵消更高优先级的欠重；不得据此替换正式版本。

## 4. 耗时和路径证据

首轮原三个结构/填充动作的接受数量均保持 9 / 12 / 13，候选计数也相同。显著变化从同期间整链移位开始：

| 首轮整链移位 | 旧七级 | 新九级 |
|---|---:|---:|
| 完整评价次数 | 420 | 27977 |
| 接受次数 | 0 | 207 |
| 经过时间秒 | 0.884068 | 103.837575 |

新目标允许很多交期改善的合法移位，每次接受后重新枚举，并执行完整评价。总评价量由 3697 增至 49844，耗时增加不是“多了两个数值相加”或 CPU 核数问题；本轮进程 CPU 接近经过时间。

新首轮局部搜索 119.472411 秒；受控拆单及唯一回放 84.453557 秒，其中回放 77.780364 秒。两类拆单各一次；回放结束仍有 41.01 吨欠重，现有零违规入口正确跳过后置序列精修（日志 `candidate_has_violations`）。不是时限截断，不是预算耗尽；单纯增大限时不会让这条已自然结束的路径继续修复。

当前证据表明搜索路径被交期目标改变后进入了带欠重的局部终点，尚不能仅据此断言某一个订单移动就是欠重根因。后续应讨论是否把纯交期移位推迟到合规修复后、并保留修复质量基准；这涉及流程设计修订，不在本轮擅自改变已确认骨架或通过放宽门槛解决。

## 5. 重复运行与完整回归

新九级重复运行 208.188363 秒、CPU 207.325044 秒，退出 0（表示允许偏差发布，不表示零欠重门通过）。最终有序方案、完整评价、轨迹、候选/完整评价/接受计数、停止原因及交期报告与首轮全部精确一致，均自然结束。

按原订单比较，278 单提前、253 单更晚；`diagnostics/delivery_objective/comparison.json` 保留 531 单逐项变化、旧欠单每日完成吨位、三个样本全部指标和确定性检查。生成命令：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/summarize_delivery_comparison.py \
  --old diagnostics/delivery_objective/old_01 \
  --new diagnostics/delivery_objective/delivery_01 \
  --repeat diagnostics/delivery_objective/delivery_02 \
  --output diagnostics/delivery_objective/comparison.json
```

三次求解和汇总命令均退出 0；汇总明确写 `acceptance=BEST_EFFORT`，不能用程序退出成功掩盖质量门失败。最终精确树累计回归在阶段 7 单独记录。
