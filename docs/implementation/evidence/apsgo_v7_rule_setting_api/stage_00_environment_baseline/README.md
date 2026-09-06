# V7 规则设置接口：接口宿主与数据库现状证据

## 1. 结论

2026-09-06 已完成只读核验，并取得用户确认：

- 在 APSGOV7 内新增独立的 V7 FastAPI 服务，不修改或复用 V3 规则服务。
- V7 服务复用 C# 现有 `BACKEND_ALGORITHM_URL`，默认仅监听 `127.0.0.1:8001`。
- 使用 V7 独立 SQLite 文件，不复用 V3 `aps_rule_dsl.sqlite3`。
- 首期限定本机访问，不新增登录鉴权；审计主体记录服务端进程身份，不把客户端 `Environment.UserName` 视为可信身份。
- HTTP 与 SQLite 适配放入独立 `apsgo_v7_service` 包；`apsgo_scheduler` 继续保持无 Web、无数据库依赖。

## 2. 核验基线

当前核验平台为 macOS 27.0（Darwin 27.0.0 arm64），回归解释器为 Python 3.10.18。

| 仓库 | 分支 | 提交 | 状态 |
|---|---|---|---|
| `/Users/miles/dev/dev-py/APSGOV7` | `codex/rule-setting-api-integration` | `6df40a1` | 干净 |
| `/Users/miles/dev/dev-py/apsgo-v3` | `APSGoV3_day_schedule` | `e5bdcdf` | 本次只读 |
| `/Users/miles/dev/dev-cs/aps-code-0806` | `master` | `d207cc2` | 仅有用户未跟踪的根 `.gitignore` |

C# 根 `.gitignore` 不属于本专项，本轮及后续均不得覆盖、删除或纳入提交。

## 3. HTTP 服务现状

- APSGOV7 `pyproject.toml` 的生产依赖为空，生产源码只有 `apsgo_scheduler.api/core/app`，没有 FastAPI、路由或服务器入口。
- 当前工作区唯一已经实现 `/api/v1` 路由的服务是 `/Users/miles/dev/dev-py/apsgo-v3`：
  - `app.py:131-147` 创建 FastAPI 并注册路由；
  - `api.py:50-58` 汇总并注册规划、规则等路由；
  - `app.py:172-179` 默认监听 `0.0.0.0:8008`。
- C# `SchedApp/App.config:95-96` 同时配置：
  - V3 服务 `http://127.0.0.1:8008`；
  - 算法服务 `http://127.0.0.1:8001`。
- `/Users/miles/dev` 内未发现 `8001` 服务端源码；现场核验时 `8000/8001/8008` 均无监听进程。

因此不能把 V3 `8008` 服务误称为 V7 宿主，也不能宣称已有 `8001` 服务可以直接改造。

## 4. 数据库现状

- V7 没有数据库代码、数据库依赖或迁移工具。
- V3 规则仓储默认使用 SQLite：
  - `rules_engine/db/factory.py:10-25` 默认选择 SQLite；
  - `rules_engine/db/sqlite_repository.py:24-50` 默认文件为 `data/aps_rule_dsl.sqlite3`；
  - MySQL 只是环境变量选择的可选实现。
- V3 没有 ORM 或统一迁移框架，SQLite 使用标准库 `sqlite3` 和仓储内嵌建表语句。
- V3 的草稿创建、规则替换、页面设置保存和启用是多个独立事务，不能满足 V7 一次保存并启用的原子性要求。

V7 因此建立独立 SQLite schema，并以单个 `BEGIN IMMEDIATE` 事务完成版本、规则、编译快照与活动指针写入。

## 5. 身份与权限现状

- V3 FastAPI 未发现 `Depends`、Bearer、OAuth 或其他服务端身份入口。
- C# 规则页面把 `Environment.UserName` 写入请求，V3 后端直接采用，只能算客户端自报文本。
- C# `App.config` 中 membership/role provider 的 `serviceUri` 为空，没有接入规则 HTTP 客户端。

首期 V7 服务只绑定回环地址。`created_by/activated_by` 由服务端以进程身份产生；客户端不提交可信身份。若未来需要监听非回环地址，必须在改动监听边界前另行设计鉴权。

## 6. V7 求解接入现状

- `SchedulingRequest` 已直接包含完整 `RuleSetSpec` 和虚拟材料原型：`src/apsgo_scheduler/api/request.py:148-159`。
- 唯一公开求解入口是 `src/apsgo_scheduler/app/service.py:55` 的 `solve_request()`。
- `solve_request()` 在 `service.py:207-215` 从请求加载一次规则集，随后搜索不访问数据库。
- 当前没有生产 HTTP 请求到 `SchedulingRequest` 的组装器；完整请求只在测试夹具中构造。
- 规则指纹与权威加载入口分别为 `rule_set_loader.py:78` 和 `rule_set_loader.py:87`。

后续 V7 服务在任务启动时读取一次活动编译快照并组装 `SchedulingRequest`，不得修改 `solve_request()` 的唯一入口语义。

## 7. C# 页面现状

- GQGA4 页面当前打开时依次加载 V3 表单目录和当前规则。
- “保存并启用”当前实际执行保存草稿、再启用草稿两个写请求。
- 每次保存前还会对四条规则分别调用预览接口。
- 当前错误处理会把结构化错误压缩为普通文本。
- `PipelineV3ApiClient` 仍被其他产线页面使用，不能整体删除。
- 当前 macOS 没有该 .NET Framework 4.7.2 + DevExpress 工程所需的构建环境；最终 C# 结论必须在 Windows/Visual Studio 2019 验证。

## 8. 已确认物理落点

| 内容 | 落点 |
|---|---|
| 求解领域与应用能力 | `src/apsgo_scheduler/api`、`src/apsgo_scheduler/app`、`src/apsgo_scheduler/core` |
| HTTP、SQLite、GQGA4 固定模板 | 新增 `src/apsgo_v7_service` |
| 默认服务地址 | `http://127.0.0.1:8001` |
| C# 地址来源 | 复用 `BACKEND_ALGORITHM_URL` |
| 默认数据库 | `data/apsgo_v7_rules.sqlite3`，允许通过 V7 专用环境变量覆盖路径 |
| 服务端操作者 | Python 服务进程身份 |
| 客户端身份 | 不作为可信审计来源 |

`apsgo_v7_service` 可以依赖 FastAPI 和 `apsgo_scheduler`；`apsgo_scheduler` 不得反向依赖服务包、FastAPI 或 SQLite。

## 9. 本阶段验证边界

- 已执行 Git 状态、源码、配置、端口监听、依赖和调用链只读核验。
- 未调用 API、未写数据库、未运行 Windows 窗体。
- 本阶段不代表接口、数据库或 C# 功能已实现。

## 10. 验证记录

| 范围 | 实际命令或方式 | 结果 |
|---|---|---|
| 文档格式 | `git diff --check` | 通过。 |
| 共享树累计回归 | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q` | 2947 项通过，171.60 秒，退出码 0。 |
| 预提交树残留检查 | 在暂存树 `ab712b21614881c0913ec16b879a2a6543e3dc29` 的干净导出中运行 `python tools/check_workspace_residuals.py --verify` | `clean_export` 模式通过，退出码 0。 |
| 预提交树累计回归 | 在同一干净导出中先运行累计测试、再执行构建 | 2947 项通过，169.52 秒，退出码 0。 |
| 预提交树编译与构建 | `python -m compileall -q src`；`python -m build --no-isolation` | 通过，生成 sdist 与 wheel，退出码 0。 |

两次无效尝试保留为诊断记录：项目根 `.venv` 未安装 `pytest`，首次命令在收集前退出 1；另一次干净导出误把构建放在架构测试之前，构建生成的 `src/apsgo_scheduler.egg-info` 使 1 项源码边界测试失败，其余 2946 项通过。按“先测试、后构建”重跑后全部通过，没有为这两项环境/顺序问题修改生产代码或测试断言。V7 共享目录不满足旧 V6 稳定残留清单属于既有边界，未复制旧残留；正式残留结论只取干净导出。
