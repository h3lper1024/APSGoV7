# V7 规则设置接口：阶段 3 规则版本存储验证

## 1. 结论

阶段 3 已建立 V7 独立 SQLite 规则版本存储：

- 三张逻辑表分别保存规则集身份与活动指针、不可变版本和版本内逐条规则。
- 写入必须位于单连接 `BEGIN IMMEDIATE` 事务中；任意语句或提交失败均回滚。
- 版本所属关系由复合外键保证，历史版本不能更新、删除或追加规则，活动指针不能清空、跨规则集或向旧版本倒退。
- 启动初始化与普通打开分离；普通查询只读取并核对既有 schema，不申请初始化写锁。
- 19 项真实文件型 SQLite 专项、74 项存储与架构聚焦及 3063 项共享树累计回归全部通过。

本阶段没有实现保存并启用的业务编排、请求摘要、幂等重放、HTTP 路由、正式数据初始化、求解绑定或 C# 页面。

## 2. 实施基线与范围

| 项目 | 实际值 |
|---|---|
| 平台 | macOS 27.0，Darwin 27.0.0 arm64 |
| Python | 3.10.18 |
| SQLite | 3.53.4，标准库 `sqlite3` |
| 分支 | `codex/rule-setting-api-integration` |
| 实施前提交 | `53d5dc64f8ae7636764f52d36e45d8eb3f0d1cbe` |
| 生产文件 | `src/apsgo_v7_service/rule_store.py` |
| 专项测试 | `tests/service/test_rule_store.py` |
| 数据库范围 | 只使用 pytest 临时目录中的文件；未创建或修改正式数据库 |

运行时数据库默认路径为 `data/apsgo_v7_rules.sqlite3`，唯一覆盖变量为 `APSGO_V7_RULE_DB_PATH`。覆盖值去除首尾空白，空白值拒绝；根目录 `data` 下的 SQLite 主文件及 journal/WAL/SHM 文件均不进入 Git。

## 3. Schema 版本 1

| 对象 | 作用 | 关键保护 |
|---|---|---|
| `v7_rule_set` | 保存 `product_line_code/process_code/scenario` 和当前活动版本 | 业务身份唯一且不可改、不可删除；活动版本可先为空，之后只向前切换 |
| `v7_rule_set_version` | 保存版本号、基础版本、操作标识、请求摘要、编译 JSON、指纹、原型、页面快照和审计 | 同规则集版本号/操作标识唯一；版本不可更新或删除 |
| `v7_rule_definition` | 每个版本的规则逐条保存 | 同版本规则标识/顺序唯一；已启用或历史版本不能追加、更新或删除规则 |

共建立六个命名唯一索引和八个不可变/活动指针触发器。`active_version_id` 与 `based_on_version_id` 均按“规则集标识 + 版本标识”引用版本表，不能只凭全局版本主键跨规则集关联。

schema 重入不只检查版本号、表名或列名，而是核对表、命名唯一索引和触发器的完整 SQLite 定义，并执行 `PRAGMA foreign_key_check`。测试已证明，同名但正文被弱化的触发器会导致启动拒绝。

## 4. 事务和连接边界

1. `RuleStore.initialize()` 只用于启动或部署初始化，可建立父目录、数据库文件和 schema，并支持无损重复执行。
2. `RuleStore.open()` 只打开已存在的数据库并在读事务中核对 schema；缺失文件直接失败，不静默创建空库。
3. 每个连接启用 `PRAGMA foreign_keys=ON`，采用 `isolation_level=None`；读事务为 `BEGIN`，写事务为 `BEGIN IMMEDIATE`。
4. 存储写方法在无事务或只读事务中拒绝执行，嵌套事务拒绝；条件活动指针更新未命中抛出 `RuleStoreConflict`，由事务上下文自动回滚。
5. 一个写连接持有保留锁时，第二个写连接会收到 SQLite 锁错误；另一普通连接仍可读取提交前快照，证明查询路径没有重复申请写锁。
6. SQLite 连接保持默认线程约束，后续 HTTP 层必须在创建连接的同一线程内使用并关闭，不能共享一个跨线程服务单例。

## 5. 独立复审修正

独立代码、测试和范围复审发现并关闭以下问题：

- 初版 schema 重入只核对列、索引名称和触发器名称，同列但无外键或同名空触发器可能冒充合法 schema；现改为完整对象定义签名核对并增加损坏库反例。
- 初版活动条件更新返回布尔值，调用方若漏查可能提交一个未启用版本；现未命中直接抛存储冲突，使同一事务中的新版本与规则行自动回滚。
- 初版不可变测试只覆盖当前活动版本；现先启用第二版本，再对真正的历史第一版本验证更新、删除和追加均失败。
- 初版回读只抽查少数字段；现逐项核对版本 JSON、摘要、指纹、原型、页面快照、基础版本、两组审计字段及规则元数据，并验证按 `sequence_no` 返回。
- 初始化与普通打开最初共用一次写式 schema 初始化；现拆开，避免未来每次 GET 查询无意义地争抢 SQLite 写锁。

复审收口后未发现剩余阶段 3 阻塞问题。

## 6. 已完成验证

| 范围 | 命令 | 结果 |
|---|---|---|
| SQLite 专项 | `PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python -m pytest -p no:cacheprovider tests/service/test_rule_store.py -q` | 19 项通过，退出码 0。 |
| 存储与架构聚焦 | 同一解释器运行专项及 `test_clean_room_boundaries.py`、`test_package_dependencies.py` | 74 项通过，退出码 0。 |
| 共享树累计回归 | `PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service` | 3063 项通过，168.24 秒，退出码 0。 |
| 静态格式与文本差异 | Ruff 格式/检查、弃用警告升级为错误及 `git diff --check` | 通过。 |

最终精确暂存树仍按项目约定导出到新的临时目录，先执行残留检查与同一 3063 项累计回归，再执行 `compileall`、源码包/安装包构建以及 wheel 内容检查；最终树身份和结果写入本阶段提交记录。

## 7. 回退与下一阶段

本阶段不提供生产反向迁移或自动删表函数。空的临时验收库已验证可在关闭连接后删除文件并重新初始化；已有数据的生产库回退必须保留结构并使用部署备份恢复。

下一阶段是“实现保存并启用事务”：必须在一个写事务中按顺序完成规范化请求摘要、幂等优先检查、期望活动版本检查、GQGA4 编译、版本与 17 条规则写入、条件活动指针切换和完整活动视图回读。不得绕开本阶段的冲突异常，也不得把未启用的部分版本提交为可见状态。
