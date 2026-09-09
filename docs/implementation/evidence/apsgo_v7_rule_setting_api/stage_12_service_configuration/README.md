# 阶段 12：统一服务运行配置（YAML 补正）

## 1. 当前状态

- 状态：用户纠正后的阶段 12 YAML 配置补正已完成并验证。Windows 构建、Designer 和真实页面交互仍属于整个规则设置专项的独立待验门禁。
- 日期：2026-09-07。
- 平台：macOS，Darwin 27.0.0 arm64。
- 分支：`codex/rule-setting-api-integration`。
- YAML 补正前提交：`63011498a5367b2e082aa7ba92639dd7e592c3e6`。
- 补正前 JSON 方案的已执行结果仅作为历史记录保留，不作为 YAML 补正验收；本节分别记录 YAML 补正后的实际结果。

## 2. 实施范围

本阶段只统一 V7 Python 服务的运行配置：

- 将补正前 JSON 入口改为受 Git 跟踪的 `config/apsgo_v7_service.yaml`。
- 配置 `database_path`、`database_timeout_seconds`、`listen_host` 和 `listen_port`。
- 增加生产依赖 `PyYAML>=6.0,<7`，使用安全 YAML 加载器严格解析配置，并让初始化器与规则服务支持 `--config <配置文件路径>`。
- 删除当前运行入口对 `APSGO_V7_RULE_DB_PATH` 的读取。
- 将阶段 11 可重复后端回环脚本改为使用临时配置文件，继续隔离默认数据库。
- 保留历史规则恢复命令的显式绝对 `--database-path` 安全边界。
- 同步设计、实施计划、开发入口和当前工作约定。

本补正不修改 SQLite schema、活动规则内容、规则指纹、求解算法、业务 HTTP 请求/响应 JSON、数据库编译 JSON 或 C# 工程，也不改写早期阶段的真实入口和结果。

## 3. 配置契约

受跟踪的默认配置内容为：

```yaml
database_path: ../data/apsgo_v7_rules.sqlite3
database_timeout_seconds: 5.0
listen_host: 127.0.0.1
listen_port: 8001
```

| 配置项 | 契约 |
|---|---|
| `database_path` | 必须是非空持久文件路径；相对值按配置文件所在目录解析为绝对路径，不按进程当前工作目录解析；拒绝 `:memory:` 和既有目录。 |
| `database_timeout_seconds` | 必须是有限且大于零的数值。 |
| `listen_host` | 必须精确为 `127.0.0.1`；阶段 12 不放宽网络暴露边界。 |
| `listen_port` | 必须是 `1`～`65535` 的整数。 |

配置根必须是 YAML 映射，且四项键必须恰好各出现一次。配置文件缺失、不可读、不是 UTF-8、YAML 非法、根节点不是映射、缺键、重复键、未知键或值非法时，初始化器和服务必须在数据库操作或 Uvicorn 启动前失败。命令启动时只读取并冻结一次配置，不热重载，也不对配置值执行环境变量替换。YAML 只用于服务运行配置，不替代业务 HTTP 和数据库编译快照使用的 JSON。

## 4. 命令边界

初始化器和服务使用同一配置：

```bash
apsgo-v7-initialize-gqga4-rules --config config/apsgo_v7_service.yaml
apsgo-v7-rule-service --config config/apsgo_v7_service.yaml
```

对应的 `python -m apsgo_v7_service.initialize_gqga4_rules` 与
`python -m apsgo_v7_service.app` 入口接受相同的 `--config` 参数。

历史规则恢复不读取服务配置，仍要求显式指定已存在数据库的绝对路径：

```bash
apsgo-v7-restore-gqga4-rule-version \
  --database-path /absolute/path/to/apsgo_v7_rules.sqlite3 \
  --source-version-id 12 \
  --save-operation-id 11111111-1111-4111-8111-111111111111 \
  --expected-active-version-id 15
```

C# 客户端继续读取 `SchedApp/App.config` 中既有的 `BACKEND_ALGORITHM_URL`；Python 配置不复制或替代该前端地址键。

## 5. 文件与数据保护

- `config/apsgo_v7_service.yaml` 是不含凭据和本机绝对路径的默认运行配置，必须由 Git 跟踪；补正前 JSON 文件应从当前树移除。
- `data/apsgo_v7_rules.sqlite3` 及同目录匹配的 journal/WAL/SHM 文件继续由 `.gitignore` 排除，不进入提交。
- 配置迁移不重建、不重置、不覆盖活动规则数据库；初始化命令的已有身份保护保持不变。
- 旧阶段证据中关于 `APSGO_V7_RULE_DB_PATH` 的文字记录当时实现，不修改为当时已使用配置文件；从阶段 12 起，当前入口以本契约为准。

## 6. 验证矩阵

| 范围 | 检查 | 实际结果 |
|---|---|---|
| YAML 配置解析 | 默认四项、相对/绝对数据库路径、不同当前目录、UTF-8、安全 YAML 解析、重复键、精确键集合与各字段边界 | 通过；危险标签、重复键、非文本键及非法特殊浮点均有拒绝验证。 |
| 初始化器 | 显式 `--config` 首次初始化与重复执行、旧环境变量无效、SQLite 超时传递、非法 YAML 不建库 | 通过；临时安装依次返回 `initialized`、`already_initialized`，默认库重复执行返回 `already_initialized` 且哈希不变。 |
| 规则服务 | 显式 `--config`、回环地址和端口、SQLite 超时传递、非法配置在 Uvicorn 前拒绝 | 通过；wheel 安装后按 YAML 在 `127.0.0.1:8001` 启动，真实 GET 返回 200。 |
| 恢复命令 | 显式绝对数据库路径仍为唯一入口，损坏服务 YAML 配置不影响恢复命令 | 通过；入口边界未改变。 |
| 真实回环 | 临时 YAML 配置驱动初始化、HTTP GET/POST、历史恢复、备份复制和完整回读；默认数据库文件族前后不变 | 通过；临时主库 5 版本/85 条规则、恢复库 6 版本/102 条规则、备份 1 版本/17 条规则。 |
| 专项测试 | YAML 配置、存储、初始化、HTTP、恢复相关测试 | 103 项通过，用时 3.49 秒；完整 `tests/service` 144 项通过，用时 5.30 秒。 |
| 累计回归 | `tests/architecture tests/api tests/app tests/core tests/service` | 共享工作树 3191 项通过，用时 190.29 秒。 |
| 静态与导出 | `git diff --check`、干净导出残留检查、同范围累计测试、`compileall`、sdist、wheel | 首轮精确暂存树 `81ca43b44e305cc2e2e124c99c71a4d60c6c10bd` 全部通过；累计 3191 项，用时 188.35 秒。 |
| 包与命令 | wheel 文件表、PyYAML 安装依赖、两个 `--config` 控制台入口及恢复入口 | 通过；wheel 含配置模块和 `PyYAML<7,>=6.0` 元数据，三个入口均可执行。 |
| Git 边界 | YAML 默认配置进入精确暂存树；补正前 JSON 配置移除；SQLite 主文件和辅助文件均未跟踪 | 通过；默认数据库继续被忽略，补正前后 SHA-256 相同。 |

### 6.1 补正前 JSON 方案的历史验证

以下结果来自提交 `6301149` 的补正前 JSON 方案，仅用于保留已发生的阶段事实，不能作为当前 YAML 补正通过的证据。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
  -m pytest -p no:cacheprovider -q \
  tests/service/test_service_configuration.py \
  tests/service/test_rule_store.py \
  tests/service/test_rule_initialization.py \
  tests/service/test_rule_http_api.py \
  tests/service/test_rule_version_restore.py
```

结果为 101 项通过。随后完整 `tests/service` 为 142 项通过；共享工作树累计范围为
3189 项通过，用时 188.56 秒。

阶段 11 真实 Uvicorn 脚本已改用临时配置文件重新执行并通过：闭环结束时临时主库为
5 个版本、85 条规则，历史恢复库为 6 个版本、102 条规则，备份副本保持
1 个版本、17 条规则；临时数据库已删除。由于本机默认数据库已按用户要求初始化，脚本观测到
默认数据库文件数前后均为 1，并确认文件族内容未变化。

补正前 JSON 默认配置实际解析到
`/Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3`。无参和显式
`--config config/apsgo_v7_service.json` 两种初始化均返回 `already_initialized`；执行前后数据库
SHA-256 均为 `af15c62d5920dee93da0bc1dda9ab519d04829c142532025d43f7837737342d0`。
活动版本为 1，包含 17 条规则、16 条启用规则、7 项质量目标和 27 个虚拟材料原型，规则集指纹为
`d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0`。

Ruff 代码检查通过，13 个本阶段相关 Python 文件的格式检查通过，`git diff --check` 通过。
共享工作区残留检查仍因冻结清单要求已经不存在的旧 V6 `.claude`、旧 `dist/apsgo-*` 和
`src/apsgo.egg-info` 文件而失败；本阶段未重建这些残留，正式门禁仍以最终暂存树干净导出为准。

两轮精确暂存树导出后的残留检查均以 `mode=clean_export`、`status=pass` 结束；对应累计
均为 3189 项通过，分别用时 185.94 秒和 185.68 秒，`compileall`、sdist 和 wheel 构建均通过。wheel 文件表包含
`apsgo_v7_service/configuration.py` 以及初始化、服务、恢复三个控制台入口；安装到新的临时
虚拟环境后，初始化连续返回 `initialized`、`already_initialized`，服务按配置启动在
`127.0.0.1:8001`，真实 GET 回读版本 1、17 条规则、27 个原型和预期指纹后正常关闭。

补正前 JSON 方案据此完成验证。上述数字不得复用为 YAML 验收结果；YAML 补正的独立实测结果见下一节。Windows 构建、Designer 和真实页面交互仍是整个规则设置专项的独立未关闭门禁。

### 6.2 YAML 补正最终验证

配置、存储、初始化、HTTP 与恢复的聚焦命令共 103 项通过，用时 3.49 秒；完整
`tests/service` 为 144 项通过，用时 5.30 秒。共享累计首次运行暴露架构依赖白名单尚未允许
`yaml`，结果为 1 项失败、3190 项通过；生产依赖已经在 `pyproject.toml` 声明，因此同步将
`yaml` 加入服务包依赖门禁及其自测。修正后共享累计 3191 项通过，用时 190.29 秒。

真实阶段 11 回环脚本已由临时 YAML 配置驱动并以退出码 0 完成：临时主库为 5 个版本、
85 条规则，历史恢复库为 6 个版本、102 条规则，备份副本为 1 个版本、17 条规则，临时文件
已清理。共享默认数据库执行前后 SHA-256 均为
`af15c62d5920dee93da0bc1dda9ab519d04829c142532025d43f7837737342d0`；重复初始化返回
`already_initialized`，活动版本仍为 1、17 条规则、16 条启用规则、7 项质量目标、27 个虚拟
原型及预期规则集指纹。

首轮精确暂存树为 `81ca43b44e305cc2e2e124c99c71a4d60c6c10bd`。其干净导出残留门禁以
`mode=clean_export`、`status=pass` 结束，累计 3191 项通过，用时 188.35 秒；`compileall`、
sdist 和 wheel 构建均通过。wheel 文件表包含 `apsgo_v7_service/configuration.py`，依赖元数据
包含 `PyYAML<7,>=6.0`，初始化、服务和恢复三个控制台入口齐全。

wheel 安装到新的临时虚拟环境后，初始化连续返回 `initialized`、`already_initialized`；服务
按 YAML 配置启动于 `127.0.0.1:8001`，真实 GET 返回 200，并回读版本 1、17 条规则、27 个
虚拟原型和指纹
`d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0` 后正常关闭。
受跟踪树已移除补正前 JSON、纳入默认 YAML；共享及导出中的 SQLite 文件族仍被忽略且未进入
提交。

## 7. 历史证据衔接

以下记录继续保留各阶段当时的真实入口和结果：

- [阶段 0：接口宿主与数据库现状](../stage_00_environment_baseline/README.md)
- [阶段 3：规则版本存储](../stage_03_rule_version_storage/README.md)
- [阶段 5：HTTP 路由](../stage_05_http_routes/README.md)
- [阶段 6：GQGA4 初始化](../stage_06_gqga4_initialization/README.md)
- [阶段 11：联调与验收](../stage_11_integration_acceptance/README.md)

这些历史文件中的环境变量说明不再是当前启动方式；阶段 12 仅增加后续取代关系，不改写旧验证发生时的事实。
