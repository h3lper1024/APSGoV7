# 阶段 7：正式规则库备份与最早开工规则启用

## 范围与结果

- 时间：2026-09-17 10:46:46（北京时间）；实施前 `codex/earliest-process-start-constraint@5027884`，macOS / Darwin，Conda `aps_3.10.18`。
- 用户本轮明确要求检查并同步提前开工规则库。本项仅同步 `GQGA4/default/month` 的活动规则，不修复搜索、不调整目标/预算、不启动或重启服务。
- 当前 YAML 解析到 `/Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3`。版本 25 原有 18 条规则，包含九项交期目标，但缺少最早开工规则。
- SQLite 备份核验后，通过既有 `set_active_gqga4_rules()` 在同一事务内保存并启用版本 **26**，基于版本 **25**；未修改数据库结构或任何历史版本。
- 新增 `earliest_process_start` / `EarliestProcessStartRule` / 当前工序最早开工，方案级、启用、版本 `1`、参数 `{}`。合计 19 条规则、18 条启用；原来的连续逆宽规则仍停用。
- 最近回写门槛调整本身不新增规则参数；“启用后提前开工必须清零，其他业务违规可回写”由现有代码执行。此次补齐的是正式库此前缺失的规则配置。

## 备份与身份

备份文件：`/Users/miles/dev/dev-py/APSGOV7-bak/apsgo_v7_rules_before_earliest_start_v25_20260917_104646_b7db7bfb.sqlite3`。

| 项目 | 值 |
|---|---|
| 同步前数据库 SHA256 | `90f7f6445573419bbdd7047e048e668ec5d6e7b593e9289e5081f0449335da77` |
| SQLite 备份 SHA256 | `e75065d8453685d14de9aa275e87fa2cabfb2c83c00cdb53a988e32ac6d20439` |
| 同步后数据库 SHA256 | `5c8d6235b2e9870615657f97e70a0df97990f88041ff3aeb3c3ca81976249d9d` |
| YAML SHA256，前后相同 | `a8bfe67f504b2f9fe9b64779355adee076e97eb3806411dbc6e6dc87928e55b5` |
| 旧规则指纹 | `3612ba668d79867d9d7bb8b12f23ce9c49ecf799b202460b3f2923d16472b12c` |
| 新规则指纹 | `9b1a395fd394b926d408eb762e51d8da2b86b6b6bf37ca9752a3308b90d88c22` |
| 保存操作标识 | `b7db7bfb-bb10-4665-b856-847d803a9af6` |
| 保存与启用主体 | `v7-earliest-start-upgrade` |
| 数据库激活时间 | `2026-09-17T02:46:46.626262+00:00` |

备份通过 `sqlite3.Connection.backup()` 从源库读取，先独占创建新备份文件，避免覆盖旧备份；备份与源文件的物理字节摘要不同不作为失败，实际核对生产活动快照、完整性和原历史行。同步前后的摘要为本次观察值，不用于覆盖后续现场变更。

## 操作与验证

使用 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/miles/anaconda3/envs/aps_3.10.18/bin/python` 执行一次性操作，顺序如下；预检和正式操作均退出 0：

1. `load_service_configuration()` 解析目标；只读查询活动版本、规则、完整性和摘要。
2. `get_active_gqga4_scheduling_snapshot()` 加载经生产校验的版本 25。按原序保留全部 `EditableRuleInput`，只追加新规则，以 `compile_gqga4_rule_set()` 预编译并断言旧规则、九项目标和允许偏差集合不变。
3. SQLite 完整备份；校验备份完整性、外键和生产活动快照与操作前完全相同。
4. `SetActiveRulesRequest` 使用上述唯一保存操作标识、`expected_active_version_id=25`、保留的全部规则及原虚拟材，通过现有事务保存启用。版本冲突会中止，不覆盖并发修改；字典由既有事务完整继承。
5. 生产接口回读新活动版本，并执行下表全部断言。

| 检查 | 实际结果 |
|---|---|
| 备份/正式库 `PRAGMA integrity_check` | 均为 `ok` |
| 备份/正式库 `PRAGMA foreign_key_check` | 均无错误 |
| 数据库结构 | `sqlite_master` 定义与备份逐项一致 |
| 活动版本 | 26，基于 25；新保存且处于启用状态，非幂等重放 |
| 原 18 条规则 | 包含类型、名称、顺序、启停及参数，逐项完全相同 |
| 九项目标及允许偏差集合 | 完全相同 |
| 虚拟原型 | 27 个，逐项完全相同 |
| 软硬钢字典 | 230 条，逐项完全相同；指纹 `d292d5efb53ee541f4d3900b2295ababfecffb1912fd0bafe4cb8bbb7b90ec14` |
| 历史版本/规则/字典 | 备份中全部原行仍存在于新库，未修改或删除 |
| `v7_rule_set_version` | 25→26，新增 1 行 |
| `v7_rule_definition` | 436→455，新增当前版本的 19 行；业务上只新增 1 条规则 |
| `v7_grade_dictionary_entry` | 5520→5750，新增当前版本继承的 230 行 |
| 规则集活动指针 | 从 25 切至 26，并更新激活时间 |
| YAML | 未改；保留现场 600000 候选、999999 秒时限及其余配置 |

原九项顺序保持：禁止违规数、禁止严重度、欠重链数、欠重缺口、旧欠全部完成时间、交期等待/延期吨小时、链间宽差、虚拟重量、非空链数。

本次未改源码，不重复完整业务回归或 GQGA4 求解；文档提交仅含本记录、实施计划和 `AGENTS.md`。正式 SQLite/YAML 的既有及本轮现场差异不纳入提交；其他发布目录、Windows 数据库及已打包程序均未同步。

## 使用与恢复

- 重新加载前端活动规则后再求解，使用版本 26；旧版本 25 的请求会按既有版本冲突保护拒绝。启用时要求 `v7-month-solve-v3` 请求及每个真实单的 `earliest_start_at`，不能用空值替代。
- 后端每次新求解读取活动规则快照；本次未操作用户服务。运行中的旧任务不会被这次启用改写；运行程序是否已加载最新代码及现场排程效果，仍由用户验证。
- 若仅需撤销本次新增规则，现有历史恢复工具可将版本 25 的内容保存为一个新的活动版本，不删除版本 26。仅在当前活动版本仍为 26 且再次获准回退时执行：

```bash
cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
/Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m apsgo_v7_service.restore_gqga4_rule_version \
  --source-version-id 25 \
  --expected-active-version-id 26 \
  --save-operation-id 7ea3a5f0-18a4-41e9-9e38-b074a84a8740 \
  --database-path /Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3
```

此恢复命令仅作为可执行交接，**本次未执行**。若活动版本已变化，先重新核对，不改期望版本硬覆盖新配置，也不直接用备份覆盖正在运行的数据库。

规则已启用不等于结果可回写：阶段 6 冻结 40 万次仍有 6 单提前的问题未修复，现场 60 万次配置未在本轮复测。只要最终仍有提前开工，必须继续阻止回写；其他业务违规放行及正确性审核边界不变。
