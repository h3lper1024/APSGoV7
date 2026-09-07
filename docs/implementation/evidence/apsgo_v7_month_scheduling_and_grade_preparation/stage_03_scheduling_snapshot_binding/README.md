# 阶段 3：绑定求解输入与数据准备报告

## 1. 完成边界

- 实施基线：`107b34b`。
- 平台：macOS / Darwin 27.0.0 arm64；Python 3.10.18，Conda 环境 `aps_3.10.18`。
- 本阶段只改服务层完整快照读取与既有求解入口绑定，不新增 HTTP 请求/响应对象，不改算法、
  规则公式、评分、预算或质量门槛。
- 正式 `data/apsgo_v7_rules.sqlite3` 仍为 schema v1，未写入；C# 用户工作树未编辑。

## 2. 实现结果

1. `get_active_gqga4_scheduling_snapshot()` 在一个 `RuleStore.transaction()` 中读取同一个活动版本的
   编译规则、虚拟原型和软硬钢字典；公开规则查询仍只返回原 `ActiveRulesResponse`，HTTP JSON
   契约不增加整份字典。
2. `bind_gqga4_scheduling_task()` 在构造核心 `SchedulingRequest` 前统一调用
   `prepare_orders_with_grade_dictionary()`；不再要求 HTTP 或其他服务调用方预填软硬分类，原
   `SchedulingTaskInput` 保持不可变。
3. 命中字典的订单写入 `rule_attributes.soft_hard_class`；未命中写入 `None` 并进入成功准备报告，
   继续使用现有同热轧牌号兜底规则。已有非空值一致时通过，不一致时在启动求解前返回聚合定位诊断。
4. `BoundSchedulingTask` 与 `BoundSchedulingResult` 保留字典指纹和完整准备报告；关联指纹覆盖活动
   版本、规则指纹、字典指纹、报告指纹和最终请求指纹。核心 `RunManifest` 不承担数据库版本职责。
5. 任务绑定返回前数据库读取已经结束；之后唯一调用现有 `solve_request()`，搜索、规则缓存和最终
   无缓存审计均只使用绑定请求。

## 3. 自动化验证

| 范围 | 结果 |
|---|---|
| `tests/service/test_scheduling_rule_binding.py` | 7 项通过 |
| 完整服务层 `tests/service` | 203 项通过 |
| 架构门禁 `tests/architecture` | 74 项通过 |
| 仓内累计 `tests/architecture tests/api tests/app tests/core tests/service` | 3250 项通过，184.44 秒 |
| 干净导出、`compileall`、sdist/wheel | 精确暂存树验证后记录在提交正文 |

覆盖内容包括：原输入不变、命中补齐、缺失报告与现有兜底求解、已有值一致/冲突、错误快照不启动
求解、绑定后禁止数据库访问仍可完成双审计、任务 A 绑定后保存版本 B 的快照隔离，以及任务/结果
的规则、字典、报告和请求身份篡改拒绝。

## 4. 结论与下一步

求解任务现在可以证明“使用哪个活动规则版本、哪份字典、产生了什么准备结果”，且搜索过程不再
依赖数据库。下一阶段实现月计划 HTTP 原始订单请求与发布结果转换；本阶段不提前接入路由。
