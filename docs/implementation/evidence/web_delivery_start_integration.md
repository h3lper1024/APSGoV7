# Web 开始时间接入验证记录

## 范围与运行身份

- 2026-09-17，macOS，Conda `aps_3.10.18`；实施前提交 `5d41729`。
- 接通服务接口和 Web，不改核心搜索、九项目标排序、40 万候选、300 秒搜索配置。
- 正式数据库与用户运行的 8001 服务未改动；完整测试使用正式库的在线备份副本，
  在副本创建交期规则版本 15，保留旧版本 14 和此前历史。
- 真实 531 单来源为既有 `combined_01/prepared_request.json`；逐单交期、炉区/工艺速度
  来自 `diagnostics/delivery_objective/timing_source.json`，重量/宽厚逐单核对。
- 请求通过 FastAPI 的测试客户端进入实际 HTTP 路由及完整求解，不是外部 TCP 网络测试，
  也不是浏览器调用正式服务。前端浏览器交互另行使用拦截响应验证。

## 完整 531 单结果

原始输出：`diagnostics/web_delivery_start_integration/full_01/`（本地诊断目录，不纳入 Git）。
请求 `d86e289f-d031-4ba9-98f9-f86ac6abba3f`；开始时间为北京时间 2026-06-01 00:00。

| 项目 | 实际结果 |
|---|---|
| HTTP / 求解状态 | 200 / success，可发布 |
| 终止原因 | `search_time_limit_reached`，不是额度跑满或自然结束 |
| 候选检查 / 完整候选评价 / 接受 | 382925 / 73145 / 486 |
| 小辊期 / 节点 | 22 / 551（533 个真实片段、18 个虚拟节点） |
| 两类拆单 | 同期间 1 次，未来借入归还 1 次 |
| 核心审核 / 应用审核 | 均通过 |
| 禁止违规 / 欠重链数 / 欠重缺口 | 均为 0 |
| 旧欠全部完成 | 2026-06-23 13:13:24.885 +08:00 |
| 旧欠订单 | 96 单，5386.03 吨 |
| 非旧欠新增晚交 | 24 单，1182.86 吨 |
| 方案生产结束 | 2026-06-27 13:29:24.065 +08:00 |
| 链间宽差 / 虚拟重量 | 11606 mm / 360 吨 |
| 服务至结果审核 / HTTP 诊断经过时间 | 301.069351 / 301.924584 秒 |
| 进程 CPU 时间 | 301.778139 秒 |

九级值依次为 `(0, 0, 0, 0, 541.223611…小时, 1181514.034911…吨小时,
11606, 360, 22)`。本次属于接入验证，不作为历史 40 万次质量对照、性能改善或最优性证明；
时限确实截断搜索，旧欠与晚交的历史优化问题不在本项关闭。

完整响应中：节点顺序、交期报告顺序及最晚日期顺序一致；551 个节点的
`completion_at`、`current_process_latest_at`、`latest_dates.coating` 全部逐一相等。
诊断记录 `exception=null`、`write_failures=[]`。

可复现命令（新输出目录，不覆盖本次产物）：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/verify_web_delivery_integration.py \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --timing-source diagnostics/delivery_objective/timing_source.json \
  --output-dir diagnostics/web_delivery_start_integration/full_02
```

本次工具先完成求解、写出响应并通过业务断言，最后写简要 `summary.json` 时，因普通 JSON
解码产生 float、而精确编码器拒绝 float，退出 1。仅修正工具为 Decimal 解码及计时转换，
不修改服务求解结果、不为报告序列化重跑 531 单。随后独立回读完整响应与服务诊断，
重新核验状态、九级值、逐节点日期和双审核；以上耗时来自已经保存的服务诊断，
不伪称工具末尾汇总成功。完整原始文件摘要：

- request.json：`c8025f502cbc0864f0c382dd945262649012c55dbfc94d2d0be99be0bdd3462e`
- response.json：`c425d88da5ff35dd18861fc0fc23449e9aefbf76a6a3c9788cddb6f9aca7ddcb`

## 集中回归与提交边界

- 后端范围：`tests/service tests/architecture tests/app/test_delivery_preparation.py
  tests/app/test_delivery_second_preparation.py`。首次 485 通过、2 个故障注入桩因新增
  序列化关键字参数未更新而失败；修正测试桩后集中复验，不改变原异常断言。
- 新增 HTTP 缺字段测试曾误期望 422，按既有契约纠正为 400；缺交期/非法时间/缺有效速度
  仍为可定位诊断，不静默使用固定开始时间。修正后共享树 488 项通过，156.62 秒；
  开始时间从 6 月 1 日改到 6 月 3 日的小例也验证了欠交分类和实际评分随之改变。
  最后精确树同范围结果见本项提交正文。
- 前端共享工作树 42 项测试通过；精确暂存树排除用户原有新增测试，36 项通过。
  必要构建和浏览器检查同样在暂存树完成；具体记录见前端提交正文。
- 浏览器时区设为纽约，提交保持显式北京时间；空时间禁止提交、取消不请求、求解中
  时间/按钮不可改、Escape 不关闭；第四步回填同结果日期，无第二次日期 HTTP 请求。
- 提交前导出精确暂存树复验。共享残留检查仅为已有 V6 残留缺失问题，不删除或重建它们；
  干净导出检查必须通过。不把用户前端变更代为提交。

## 部署状态

2026-09-17 用户确认仅备份数据库并启用九项目标，服务启动及测试由用户执行。
基线 `bc54444`，工作树操作前干净；8001 操作前无监听，本任务未启停服务。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m apsgo_v7_service.enable_delivery_rules \
  --database-path /Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3 \
  --backup-path /Users/miles/dev/dev-py/APSGOV7-bak/apsgo_v7_rules_before_delivery_v14_20260917_045558.sqlite3 \
  --expected-active-version-id 14
```

命令退出 0，实际活动版本为 15；备份的活动版本为 14。仅进行数据库回读核验：
原版本 1～14 的版本记录完整保留，已有 17 条规则定义及虚拟材原型保持，新增启用交期规则，
九项目标与计划顺序一致。正式库和备份的 SQLite 完整性检查均为 `ok`。
本轮不运行单元测试、浏览器测试或求解；正式环境运行效果待用户手动验证。
