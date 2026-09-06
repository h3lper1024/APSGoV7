# 阶段 11：规则设置接口联调与验收记录

## 1. 结论

阶段 11 的**后端真实联调、历史规则受控恢复、非生产数据库备份恢复演练和最终精确树本地门禁均已完成**。Windows/.NET Framework 4.7.2 构建、WinForms Designer 打开和真实页面交互仍是唯一未关闭的外部环境门禁，因此本阶段及整个规则设置接口专项继续标记为“待 Windows 验证”，不以 C# 静态检查替代。

本阶段没有创建或修改默认 `data/apsgo_v7_rules.sqlite3`，没有连接生产数据库，没有新增 HTTP 接口、页面或数据库表，也没有重复执行已经留有完整证据的三分钟 GQGA4 求解。

## 2. 环境与范围

| 项目 | 实际值 |
|---|---|
| 平台 | macOS 27.0，Darwin 27.0.0 arm64 |
| Python | 3.10.18，Conda 环境 `apsgo_v6_3.10.18` |
| V7 分支 | `codex/rule-setting-api-integration` |
| 阶段 11 实施前提交 | `d9e90b73012d776fffa2aa1936a6deca53457262` |
| 真实回环脚本提交 | `b57f10d`，`#feat 增加规则接口真实联调验收` |
| C# 分支 / 已实现提交 | `codex/v7-rule-client-integration` / `4b7668c` |
| 数据库范围 | 自动清理的临时 SQLite；默认库主文件及其 WAL/SHM 前后均不存在，未被创建或修改 |

## 3. 验收矩阵

| 范围 | 当前证据 | 结论 |
|---|---|---|
| V7 JSON 契约 | `test_rule_management_contracts.py` | 完整集合、精确十进制、错误结构已通过。 |
| 规则编译 | `test_rule_set_compiler.py` | 17 条规则、16 条启用、1 条停用、7 级评分、指纹和非法配置已通过。 |
| SQLite 存储与事务 | `test_rule_store.py`、`test_rule_management_service.py` | 约束、回滚、活动指针、历史不可变已通过。 |
| 并发与幂等 | 上述事务测试及本阶段真实回环脚本 | 两编辑者冲突、丢响应重试、历史重放、操作标识复用冲突已通过。 |
| HTTP | `test_rule_http_api.py` 及真实 Uvicorn 子进程 | 两条精确路由、状态码、仅回环启动和默认库保护已通过。 |
| 求解绑定 | `test_scheduling_rule_binding.py` 及阶段 7 完整求解证据 | 快照隔离、损坏拒绝、搜索零数据库查询已通过。 |
| C# 页面 | 阶段 8～10 三个静态验证器 | 代码契约通过；Windows 构建和页面交互待完成。 |
| 部署与回退 | 初始化、SQLite 备份恢复、历史内容向前恢复 | 非生产演练通过；不包含生产发布。 |

## 4. 真实 HTTP 闭环

执行：

```bash
PYTHONDONTWRITEBYTECODE=1 \
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
docs/implementation/evidence/apsgo_v7_rule_setting_api/\
stage_11_integration_acceptance/verify_backend_roundtrip.py
```

脚本使用生产初始化入口和生产 `run_server()` 启动真实 `127.0.0.1` Uvicorn 子进程，只把数据库路径指向新建临时目录；HTTP 闭环完成后，同一脚本继续调用生产恢复命令并校验 SQLite 备份副本。结果 `passed=true`，覆盖：

1. 初始 GET 返回 17 条规则、16 条启用规则、7 项评分和 27 个虚拟原型。
2. 链重下限改为 `700.25` 后 POST，新版本再 GET 逐字段一致。
3. 厚度区间容差 `0.200125` 和 27 个原型重量 `20.125` 保持数组顺序与十进制精度。
4. 两编辑者旧基础版本返回 `409 active_version_conflict`，数据库不变。
5. 非法链重参数返回 `422 rule_set_validation_failed`，数据库不变。
6. 虚拟原型可保存为空集合，再恢复原 27 个原型及全部元数据。
7. 相同请求即时重放和被后续版本替代后重放均不新增版本，也不重新激活旧保存。
8. 同一操作标识对应不同内容返回 `409 operation_payload_conflict`，且该检查先于基础版本冲突。

HTTP 闭环结束时临时库为 5 个版本、85 条逐条规则，活动版本为第 5 个；历史内容恢复后为 6 个版本、102 条逐条规则，活动版本为第 6 个。备份副本保持 1 个版本、17 条逐条规则、活动版本 1；临时目录退出后已删除。默认数据库主文件、`-wal` 和 `-shm` 在成功或异常退出路径都会核对，本次前后文件数均为 0。

## 5. 历史规则恢复与数据库备份演练

新增运维入口：

```bash
apsgo-v7-restore-gqga4-rule-version \
  --database-path <既有数据库绝对路径> \
  --source-version-id <来源版本主键> \
  --save-operation-id <本次恢复的UUID> \
  --expected-active-version-id <操作前确认的活动版本主键>
```

命令必须显式指定既有数据库绝对路径，不读取隐式默认路径或自动建库。它先完整核验来源版本，再复用正常保存并启用事务创建新版本。测试锁定以下边界：

- 来源编译 JSON、指纹、逐条规则、虚拟原型、页面快照或请求摘要不一致时拒绝。
- 来源必须是历史版本；当前活动版本不能再次恢复成一个无意义的相同新版本。
- 活动版本变化时拒绝，不静默覆盖。
- 响应不确定时原样重跑同一命令可幂等返回；改变同一操作标识对应的基础版本或来源审计主体则冲突。
- 原恢复结果已被后续活动版本替代时，重放返回 `superseded_replay` 和退出码 3，不重新激活旧结果。
- 新版本的 `based_on_version_id` 指向操作前活动版本，审计主体记录来源版本；来源规则、原型、备注和历史数据库行保持不变。

上述 `verify_backend_roundtrip.py` 是本次演练的可重复执行入口。实际顺序为：初始化版本 1 → 用标准库 `sqlite3.Connection.backup()` 在服务启动前备份 → 通过 HTTP 创建活动版本 2～5 → 从历史版本 1 创建恢复版本 6 → 原命令幂等重放 → 将备份复制为另一临时库并通过生产初始化入口完整回读。结果：

```text
恢复库：6 个版本、102 条规则、活动版本 6
恢复审计主体：v7-rule-restore:source-version-1
备份副本：1 个版本、17 条规则、活动版本 1
备份副本初始化检查：already_initialized
```

这证明“规则内容恢复为新版本”和“整个 SQLite 文件恢复”是两条独立路径；生产环境仍必须先确认精确数据库文件、备份点和停写窗口。

## 6. 自动验证结果

关键后端矩阵与恢复专项的实际命令为：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
-m pytest -p no:cacheprovider -q \
tests/api/test_rule_management_contracts.py \
tests/app/test_rule_set_compiler.py \
tests/service/test_rule_store.py \
tests/service/test_rule_management_service.py \
tests/service/test_rule_http_api.py \
tests/service/test_rule_initialization.py \
tests/service/test_scheduling_rule_binding.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
-m pytest -p no:cacheprovider -q \
tests/service/test_rule_version_restore.py \
tests/service/test_rule_management_service.py \
tests/service/test_rule_store.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
-m pytest -p no:cacheprovider -q tests/architecture
```

C# 源码契约检查的实际命令为：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_08_csharp_rule_client/verify_client_contract.py \
--client-root /Users/miles/dev/dev-cs/aps-code-0806

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_09_gqga4_active_rule_loading/verify_page_loading.py \
--client-root /Users/miles/dev/dev-cs/aps-code-0806

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_10_gqga4_save_and_activate/verify_save_flow.py \
--client-root /Users/miles/dev/dev-cs/aps-code-0806
```

| 验证 | 实际结果 |
|---|---|
| 规则管理关键矩阵 | 204 项通过，4.04 秒，退出码 0。 |
| 历史恢复专项与相关事务/存储 | 62 项通过，2.96 秒，退出码 0。 |
| 架构门禁 | 74 项通过，0.50 秒，退出码 0。 |
| C# 阶段 8～10 静态验证器 | 三组均返回 `status=passed`，退出码 0。 |
| 真实后端回环 | `passed=true`，退出码 0。 |
| 真实非生产回退演练 | 同一真实联调脚本完成初始化、内容恢复、幂等重放、备份恢复，退出码 0。 |
| Ruff 与 `git diff --check` | 通过。 |
| 共享候选工作树累计回归 | 3167 项通过，173.29 秒，退出码 0。 |
| 最终精确暂存树导出 | 残留门禁、3167 项累计回归、真实回环、C# 三组静态验证、`compileall`、构建及 wheel 内容检查均通过。 |

V7 共享工作目录的旧残留检查仍因迁入时主动剥离的 V6 `.claude`、旧分发包和旧 `egg-info` 而失败；没有为了通过检查重建这些旧文件。提交前对精确暂存树的全新导出运行同一检查，只有 `mode=clean_export` 且 `status=pass` 才记为正式门禁通过。

## 7. 复用的完整 GQGA4 求解证据

[阶段 7 完整求解](/Users/miles/dev/dev-py/APSGOV7/docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_07_scheduling_rule_binding/README.md)已经使用数据库初始活动版本完成 531 个来源的完整排程：七级评分 `(0, 0, 0, 0, 10912, 600, 22)`，零禁止、零欠重、来源重量守恒、两级审计通过；绑定读取 1 次，搜索期间数据库打开 0 次，总耗时 166.503257 秒。

该结果只作功能验收，不是 20 对性能验收，也不证明全局最优。阶段 11 的生产变更仅增加受控恢复命令，并把原活动版本回读主体提取为历史/活动共用校验；最终精确树已重新运行包含绑定专项在内的完整累计回归。由于最终树未改变求解器、规则或绑定语义，本阶段不重复保存一份相同的完整求解产物。

## 8. Windows 待完成门禁

当前 macOS 没有 .NET Framework 4.7.2、MSBuild、C# 编译器和 DevExpress 20.1.3 环境。必须在同一台 Windows 主机或虚拟机运行 V7 回环服务，并完成：

```powershell
nuget restore AutoSchedule.sln
msbuild AutoSchedule.sln /m /t:Build /p:Configuration=Debug /p:Platform="Any CPU"
```

随后逐项保留日志或截图：

- Designer 正常打开，100%、125%、150% DPI 无遮挡。
- 页面首次打开只有一次 GET；修改后只有一次 POST，再打开可读到新版本。
- 两窗口并发保存时后者提示冲突并保留输入；`422` 能定位并聚焦字段。
- 模拟成功响应丢失后，“重试保存”复用同一操作标识。
- 关闭虚拟材料得到空原型；再次启用得到 27 个标准原型。
- 不可无损投影的非空原型保持只读，保存不丢失。
- 只有保存版本仍为活动版本时推进排程步骤；取消修改不调用网络、不推进。
- 其他仍使用 V3 客户端的规则页面完成一次回归冒烟。

上述 Windows 项全部有证据后，才可把阶段 11 和本专项标记为完成。
