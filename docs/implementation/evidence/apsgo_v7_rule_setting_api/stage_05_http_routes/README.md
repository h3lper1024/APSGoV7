# V7 规则设置接口：阶段 5 HTTP 路由验证

## 1. 结论

阶段 5 已接入 GQGA4 月计划规则设置的两条固定 HTTP 接口：

- `GET /api/v1/rule-sets/GQGA4/default/month/getActiveRules`
- `POST /api/v1/rule-sets/GQGA4/default/month/setActiveRules`

POST 从原始 UTF-8 字节进入既有精确 JSON 解析器，GET/POST 的数据库服务均在线程池工作线程中完整执行；没有通过 Pydantic 或 `float` 重建数值。统一错误响应、输入上限、仅回环启动和日志脱敏已经测试锁定。

本阶段不初始化正式数据库、不绑定求解请求，也不修改 C# 页面。

## 2. 实施基线与范围

| 项目 | 实际值 |
|---|---|
| 平台 | macOS 27.0，Darwin 27.0.0 arm64 |
| Python | 3.10.18 |
| FastAPI | 0.128.8 |
| Starlette | 0.52.1（FastAPI 传递依赖） |
| Uvicorn | 0.40.0 |
| HTTPX | 0.28.1（仅开发测试） |
| SQLite | 3.53.4，标准库 `sqlite3` |
| 分支 | `codex/rule-setting-api-integration` |
| 实施前提交 | `eae53c0b879b07a7a17170b34af2fefe28253fcf` |
| 数据库范围 | pytest 临时文件和一次自动清理的临时回环验收库；未创建或修改正式数据库 |

## 3. 输入与运行边界

| 边界 | 冻结值 | 验证 |
|---|---:|---|
| HTTP 请求体 | `262144` 字节 | 流式累计实际字节；伪小 `Content-Length` 仍拒绝超限正文，恰好等于上限进入 JSON 解析。 |
| 任一 JSON 字符串 | `4096` 个 Unicode 字符 | `4096` 通过、`4097` 返回 `400`；非 UTF-8 和孤立 Unicode 代理字符也在写库前拒绝。 |
| 任一 JSON 数组 | `128` 项 | `128` 通过、`129` 返回 `400`。 |
| 监听地址 | 仅 `127.0.0.1` | `0.0.0.0`、局域网地址、IPv6 回环别名、`localhost` 和其他 IPv4 回环地址均在调用 Uvicorn 前拒绝。 |
| 默认端口 | `8001` | 控制台命令与 `python -m` 两种受控启动方式调用同一入口。 |

应用创建不读取、不创建数据库；受控启动会先解析并冻结数据库路径，空白环境变量在 Uvicorn 启动前拒绝。缺少数据库返回脱敏 `503`；不兼容的数据库结构返回脱敏 `500`。自动 OpenAPI、Swagger、ReDoc 和尾斜杠重定向均关闭，运行时只暴露两条业务路径。

## 4. 错误与审计证据

| 场景 | 结果 |
|---|---|
| JSON、内容类型、顶层结构或输入上限错误 | `400`，保留可定位诊断。 |
| 规则集或活动版本不存在 | `404`，不自动建库或生成默认配置。 |
| 活动版本变化或同操作标识被用于不同内容 | `409`，返回期望版本和当前版本。 |
| 规则缺失、重复、未知或参数不合法 | `422 rule_set_validation_failed`，保留字段级诊断。 |
| 历史快照、数据库结构或内部写入不一致 | 脱敏 `500`，不返回异常、SQL、路径或堆栈。 |
| 数据库文件缺失、权限、锁或打开失败 | 脱敏 `503`。 |
| 错误 HTTP 方法 | `405 method_not_allowed`，保留 `Allow` 响应头并使用统一错误结构。 |

业务日志记录固定规则集身份、规范化操作标识、旧/当前版本、指纹、服务端审计主体和状态码，不记录请求正文、备注、数据库路径、SQL 或内部异常文本。Uvicorn 访问日志关闭。

## 5. HTTP 闭环

真实 Uvicorn 回环进程使用临时 SQLite 完成：

1. GET 读取版本 1。
2. 将链重最小值改为精确十进制 `700.125`。
3. POST 创建并启用版本 2。
4. 再次 GET，响应与 POST 活动视图逐字段相同。

结果为 17 条规则、7 项评分、27 个虚拟材料原型，新指纹为 `2453ce78e96f4cc65ef62e4fe632ddee35fee8d94f0e5e6f425b7b788eadd658`。专项自动化还验证高精度小数及 `1e10000` 在 HTTP、事务和数据库回读之间保持精确。

## 6. 验证结果

| 范围 | 命令 | 结果 |
|---|---|---|
| HTTP 专项 | `PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python -m pytest -p no:cacheprovider tests/service/test_rule_http_api.py -q` | 42 项通过，退出码 0。 |
| 阶段聚焦 | 同一解释器运行规则管理契约、HTTP、事务、存储和单项架构门禁测试 | 180 项通过，退出码 0。 |
| 真实回环 | Uvicorn `127.0.0.1:8001` + 标准库 `urllib.request` 执行 GET→POST→GET | 版本 1→2，17/7/27 与指纹一致，退出码 0。 |
| 共享树累计回归 | `tests/architecture tests/api tests/app tests/core tests/service` | 3143 项通过，172.72 秒，退出码 0。 |
| 干净导出残留与累计回归 | 精确暂存树导出后运行残留检查及 `tests/architecture tests/api tests/app tests/core tests/service` | `clean_export` 通过；3143 项通过，退出码 0。 |
| 干净导出编译与构建 | 同一导出依次运行 `python -m compileall -q src`、`python -m build --no-isolation` | 编译、源码包和 wheel 构建通过，退出码 0。 |
| wheel 内容 | 读取 wheel 的文件表、`METADATA` 和 `entry_points.txt` | 包含 `apsgo_v7_service/app.py`、FastAPI/Uvicorn 运行依赖及 `apsgo-v7-rule-service = apsgo_v7_service.app:main`。 |

本阶段采用一个 HTTP 模块并复用既有解析、序列化和事务服务；未引入通用路由层、请求 DTO、ORM、连接池或缓存。

## 7. 下一阶段

下一阶段是“初始化 GQGA4 活动版本”：只建立一次性且可重复安全执行的初始化入口，将包内生产种子编译、写入并完整回读；不得通过 HTTP 请求自动初始化，也不得提前绑定求解器或修改 C# 页面。
