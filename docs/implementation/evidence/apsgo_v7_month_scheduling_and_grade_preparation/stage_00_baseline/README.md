# 阶段 0：原始输入与接入基线

## 1. 完成边界

本阶段只读核验 V7、V3、C# 和两个 SQLite 数据库，不修改生产代码、规则、字典或业务数据库。字典证据只保存摘要与规范化内容哈希，不保存 230 行 JSON/CSV 副本。下一阶段可依据本目录脚本实现纯内存字典模型。

| 项目 | 现场结果 |
|---|---|
| 系统 / Python | Darwin 27.0.0 arm64；Conda `aps_3.10.18` / Python 3.10.18 |
| V7 基线 | `codex/rule-setting-api-integration@f44f371`；本阶段开始前干净 |
| V3 基线 | `APSGoV3_day_schedule@e5bdcdfd3dd1ed880037d28159bfe8b6bef5d15f`；干净 |
| C# 基线 | `codex/v7-rule-client-integration@4b7668c70935282a04222d06e73d6a3bbc478e41` |
| V7 目标库 | `/Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3`，schema v1，活动版本 1 |
| V3 源库 | `/Users/miles/dev/dev-py/apsgo-v3/data/aps_rule_dsl.sqlite3`，只读完整性 `ok` |
| Windows 验证 | 未执行；当前 macOS 无 MSBuild、.NET Framework 4.7.2 和 DevExpress 运行环境 |

C# 工作树原有 `App.config`、`ApsgoV7RuleApiClient.cs`、`licenses.licx` 修改和未跟踪 `.gitignore` 均属用户，本阶段未编辑。`PipelineV7ApiBaseUrl` 当前值为 `http://192.168.4.42:8001`，后续实施必须保留。

## 2. 字典与 531 单核验

| 检查 | 结果 |
|---|---:|
| V3 字典全库 | 288 行：GQGA4 230，GQGA5 58 |
| GQGA4 有效字典 | 230 行全部启用：软钢 131、硬钢 99 |
| 空牌号 / 规范化重复 | 0 / 0 |
| GQGA4 字典规范化投影 SHA-256 | `42e398452fa9c785b41e3fc7d528d5a1a7a0553f57d2792d9e87c0b560f15614` |
| 输入订单 | 531 行 / 531 节点 / 29333.91 吨 |
| V3 生产拼接结果 | 软钢 525、硬钢 4、空 2 |
| 与冻结问题逐单比较 | 531/531 一致，差异 0 |
| 逐单比较规范化 SHA-256 | `460ddb273fd36e9d8fb50c3feedaef460aa4457fef0fcfc425f19eac607ed017` |
| 实际过渡材 | 61 条，61 条均由字典命中软钢 |

未命中的两条为 `0030124824-000050`、`0030125170-000010`，牌号均为 `HC220YD+Z-GL`。本阶段不把它们改成 `HC220YD+Z`，也不推断软硬分类。

只读核验命令：

```bash
PYTHONDONTWRITEBYTECODE=1 \
/Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
docs/implementation/evidence/apsgo_v7_month_scheduling_and_grade_preparation/stage_00_baseline/verify_backend_v3_baseline.py \
  --v3-root /Users/miles/dev/dev-py/apsgo-v3 \
  --v3-database /Users/miles/dev/dev-py/apsgo-v3/data/aps_rule_dsl.sqlite3 \
  --optimization-problem tests/baselines/gqga4/inputs/optimization_problem.json \
  --input-orders tests/baselines/gqga4/inputs/input_orders.csv
```

实际退出码为 0，输出 `status: pass`。脚本同时冻结以下输入 SHA-256：

- V3 数据库：`2c4e44c4b4c2060cb54890217ea7097164b4c88e25a452796a931883df4cce7e`
- `input_orders.csv`：`d178cc19ac6ef2807962beb110333f8702c8e2eeec153f9b0f94236913060941`
- `optimization_problem.json`：`8edb7f4110384af14d58e9bdc7caec490f3de053401173db952854275966abdc`
- V3 仓储、字典拼接、加载器和字段映射：由脚本内 `EXPECTED_HASHES` 逐项核验。

## 3. C# 月计划入口

现场调用链确认：

1. GQGA4 命令要求至少选择一行，选中项只用于取得 `Version` 等页面上下文。
2. 实际求解读取相同 `Version + ProductLine + Step=配置规则` 的全部记录，不按所选 ID 缩小范围。
3. 旧服务再保留 `CoatingShortage > 0` 的订单，并按预设大辊数字顺序形成期间。
4. C# 页面产线值为 `四镀锌`，HTTP 规则身份才是 `GQGA4`；后续不能把本地查询条件误改成 `GQGA4`。

受跟踪 `SchedApp/Data/SchedDatas.db` 只读快照：

| 项目 | 结果 |
|---|---:|
| 文件 SHA-256 / 大小 | `6a6e60febeeb93dde829a1d743262aeb971e468110489c6dbbed431cc980e309` / 90,439,680 bytes |
| `四镀锌/配置规则` | 2362 行、5 个版本；正重量输入中虚拟材 0、重复来源 0、预拆片 0 |
| 531 单版本 | `20260805100613`；配置规则 531 行 |
| 对应历史求解结果 | 616 行：真实 566、虚拟 50；34 个父订单形成 69 个片段，单父单最多 3 片 |

历史求解结果全部具有非空 `OrderSplitNumber`，因此后续不能用该字段是否为空判断“发生拆单”，必须依据同一来源产生多个节点及拆单谱系判断。当前 Debug 数据库 SHA-256 为 `40b779e764cd7b7281bac1dbd3bed5e0ec4f8e6ab726fd731d3e1ee2e69250de`，与受跟踪源库不同，只能作为运行副本候选，不能冒充实际生产库。

## 4. 输入和告警边界

- [请求契约样例](month_solve_request_contract_sample.json) 取自两条冻结真实订单，只含原始字段，不含 `soft_hard_class` 或材料角色。
- 当前旧客户端会传入 `IsVirtual/VirtualBelongs`，但没有拒绝、重复来源校验；V7 新服务必须在请求前统一拒绝已有虚拟输入和重复来源。
- 历史求解支持一份真实来源返回多个拆片，并能创建生成型虚拟输出；该能力需要保留。
- `SchedRecord.Warnings` 实体未声明长度；当前 SQLite 列为 `varchar(255)`。SQLite 临时内存验证可往返 4096 字符，但实际 SqlSugar 长 JSON、事务、图表和告警计数须在阶段 7 的临时数据库及 Windows 页面验证。
- 实际运行数据库由可执行程序目录下 `Data/SchedDatas.db` 决定，构建又会复制源数据库；正式联调前必须先识别并备份真正运行实例。

## 5. 未提前完成的事项

- 未生成 V7 字典表、未迁移真实 V7 数据库。
- 未实现 V7 月计划 HTTP 接口或 C# 专属求解服务。
- 未执行 Windows 构建、Designer、真实页面、长告警 JSON 和数据库回滚验证。
- 未重跑求解质量或性能；阶段 0 只冻结数据准备与接入事实。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| 本目录只读基线脚本 | 退出码 0，`status=pass`，约 0.48 秒 |
| 请求契约样例解析与派生字段排除 | 通过，退出码 0 |
| `tests/architecture` | 74 项通过，0.47 秒 |
| 仓内累计 `tests/architecture tests/api tests/app tests/core tests/service` | 3190 项通过，181.59 秒 |
| `git diff --check` | 通过 |

提交级精确树还须通过干净导出残留门禁和同范围 3190 项累计测试，实际树身份及耗时记录在本阶段提交正文中。
