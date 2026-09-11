# 交期优化阶段 0：前置核对记录

日期：2026-09-11。实施前提交：`73e93f2d3336120d7154c59af7639f727875a9ae`。分支：`codex/delivery-objective-optimization`。当前平台：macOS / Darwin ARM64。

**状态：已开始前置核对，阶段 0 尚未完成。** 用户已授权持续实施，但虚拟材工时来源和首个客户端还需要确认。本轮不创建不完整的正式 v2 请求，不用零工时占位，不开始阶段 1。

## 1. 当前仓库及保护范围

- 起始工作树干净。上轮数据库修改已由用户在 `73e93f2` 提交；本轮不改、不暂存数据库。
- 相比旧数值加速参考提交 `491e901757d98eb4101d17c3803cdedc98623c9d`，当前 `src/apsgo_scheduler/core` 没有 Git 差异。该检查只说明核心源码相同，不代替新接口或整项回归。
- 当前 YAML：种子 590531，候选上限 200000，总时限 310 秒，收尾预留 10 秒，整链配对余量 40 吨，最多 2 个虚拟桥接节点。没有修改这些值。
- 当前 GQGA4 活动规则版本为 14，指纹为 `a36b987b6e5e03eb43e03c5b8381f744e21707a03c24b9391e2600f6d0dd6027`。只读查询使用 SQLite `mode=ro`，不经过可能初始化数据库的服务启动流程。

## 2. 现有输入、输出与缺口

### 2.1 原订单时间来源

用户此前提供的 `/Users/miles/Desktop/GQGA4_test_new.xlsx`，工作表 `Sheet`：

| 内容 | 位置 / 结果 |
|---|---|
| 原订单号与拆分号 | J、K 列；以 J 原订单号关联，不按行号匹配 |
| 宽度 / 厚度 / 本次欠交重量 | AG、AI、AW 列 |
| 真实/虚拟标记 | AX 列；533 条真实片段、35 条虚拟材，另 1 条汇总行不作为订单 |
| 交期 | AY 列；全部 531 个原订单有合法日期：5 月 31 日、6 月 5/10/15/30 日，年份均为 2026 |
| 炉区速度 | CH 列；全部真实片段有正值，同源拆片的速度和交期一致 |
| 显示用连镀欠交用时 | CI 列；不以显示位数作为新版累计时间输入 |

按原订单号与下述旧请求对照：原订单共 531 个，无遗漏、无额外原订单；逐单宽度、厚度及拆片合计重量全部一致，真实重量总计 29333.91 吨。这里只核对身份、宽厚、重量、日期和速度，没有声称所有 135 列或钢种/温区全字段一致。

这是排程结果表，不是未经排程的原始请求。后续应以冻结请求保留原订单身份、重量和顺序，通过原订单号补时间来源；不能把 533 个拆片直接作为 533 个新原订单送入求解，也不能按该表的排程顺序重排参考输入。当前未生成这种派生请求。

### 2.2 可复用的旧七级参考

目录：`diagnostics/numpy_numba_virtual_bridge/stage_04_latest/`。引用的是已存在的历史样本，本轮没有重跑。

- 原请求：531 个真实原订单、27 个虚拟原型；期序为 `BR_00000001、BR_00000002、BR_00000003、BR_00000006`，不推断为所有任务固定四期。
- 原策略与当前上述 310/10/200000、种子等设置一致。
- 原规则版本 6，指纹 `508ffd1176f74fdf6505e1c5dfdfadaadecf723d03f8fa7c359b40e145d15ef0`。与当前活动版本 14 逐字段比较，编译规则仅 `version` 和 `fingerprint` 不同；规则参数、目标声明、允许偏差均相同，原型列表也相同。保留两份版本身份，不改历史快照。
- 原完整输出：质量 `(0,0,0,0,11726,660,23)`，200000 次候选检查、3697 次完整评价、54 次接受；停止原因 `candidate_limit_reached`，保存的核心及应用审计均通过。
- Excel 中虚拟材有 35 条，旧参考为 660 吨（原型每个 20 吨，即 33 条），因此不能把两者说成同一完整排程结果。它们仅在已核对的真实输入字段上对应；后续使用旧参考方案作对照、Excel 提供时间来源，两者分工明确。

### 2.3 仍缺的两项

1. **虚拟材速度**：当前和旧参考的 27 个原型都没有速度字段，现有 `rule_attributes` 仅有 `hot_roll_grade`。请用户明确有效米/分钟速度及来源：可以明确批准统一值，也可以按原型/规格提供；本轮不猜平均值、不取邻单、不填零。选择后才可构造严格时间输入并开始真实交期对照。
2. **首个接入客户端**：Web `/Users/miles/dev/dev-front/APSGoWeb` 或 C# `/Users/miles/dev/dev-cs/aps-code-0806`。建议先接当前 `web-integrate` 来源对应的 Web，但这只是建议，尚未选择或获得该客户端的具体修改范围。

生产开始时间继续采用设计已明确的 `2026-06-01T00:00:00+08:00`，日期截止为当天 24 点，不需要用户重复确认。目标、物理规则和预算不在本轮变更。

## 3. 已核对的文件身份

仓内路径相对 V7 根目录。下面记录现有字节身份，不代表新版交期输入已经冻结完成。

| 文件 | SHA-256 |
|---|---|
| `/Users/miles/Desktop/GQGA4_test_new.xlsx` | `a9321ef3abcc307b0a96e5af7174ae22f60b61871892c489e911e5c337c09ff0` |
| `diagnostics/numpy_numba_virtual_bridge/stage_04_latest/prepared_request.json` | `ce5be9057f9f9169b58f9187c8779ab5abe2ff5fab0d256ba53508e4f35333cb` |
| 同目录 `measurement.json` | `bdf82a82a37b762bad6b19cd4446d6bc7c1df4783b8960a92fca55581a2e3c15` |
| 同目录 `input_identity.json` | `e4cebfc9662a763f8b40801002cdc1c3e16e2c864bc24b828d5dc3f50a223360` |
| `data/apsgo_v7_rules.sqlite3` | `72a60e32fea0d2d55f883a8b743175aa5e8fd94e779388db6ae8c8c26ec2bd6a` |
| `config/apsgo_v7_service.yaml` | `dc8f113ef85689fdc5ff2fecb0579f68c6892a8d8316c764568946bf341dd4c8` |
| `tests/baselines/gqga4/quality_gate.json` | `c83c9e95b95f1918513830e328e28f6c380525ba2b698cc3a61c28c34441c4c3` |
| `tests/baselines/gqga4/performance_gate.json` | `e6a472ea83288b67e2902fd47c11cf312d174a3cd9803f91f71b8dbbed8209cb` |

## 4. 执行和验证

| 操作 | 实际方式 | 结果 / 边界 |
|---|---|---|
| 平台 / Git | `uname -s -m`、`git status --short --branch`、`git log -4 --oneline`、`git show --stat --oneline HEAD` | 均退出 0；核验当前平台、分支和用户数据库提交 |
| 历史核心比较 | `git diff --stat 491e901757d98eb4101d17c3803cdedc98623c9d HEAD -- src/apsgo_scheduler/core` | 退出 0，无差异；不能代替整仓回归 |
| 规则与输入结构核对 | Conda `aps_3.10.18` 的 Python `-B -` 只读脚本，SQLite `mode=ro` / JSON 精确字段比较 | 退出 0；版本、原型、策略、已有审计和文件身份见上文 |
| Excel 提取 / 按原订单对照 | bundled Python `-B -c`、`openpyxl.load_workbook(read_only=True, data_only=True)`；无保存 / 导出 | 退出 0；对照 531 单，核对问题 0 项；不重算 Excel 公式、不修改原表 |
| 文档检查首次 / 修正后 | Conda Python `-B -` 核对 UTF-8、引用、状态和哈希 | 首次核对脚本误用 `generated_virtual` 作持久化角色值，断言失败；源码实际值为 `virtual_sphc`。仅修正核对脚本后退出 0，3 份文档、5 处本地链接、8 个文件哈希及 33 个历史虚拟节点通过；不是算法错误或业务测试 |
| 文档与提交范围 | 提交前 `git diff --check`、精确暂存范围及 `git diff --cached --check` | 实际最终结果记录在本次提交正文；不执行重复业务测试 |

项目脚本/数据库检查使用 `/Users/miles/anaconda3/envs/aps_3.10.18/bin/python`。Excel 只读提取依表格技能使用工具提供的 `/Users/miles/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3`，不是切换生产或测试环境，不新增依赖。本轮没有运行求解器、pytest、构建、服务或 Windows EXE。

下一步：收到上述两项确认后，完成阶段 0 的时间来源和客户端范围冻结，再依计划连续进入阶段 1～2；计时与评分测试仍集中到阶段 2，不以本次资料检查冒充业务测试通过。
