# 阶段 1：建立发布输入与数据库验证证据

## 1. 范围

| 项目 | 结果 |
|---|---|
| 日期 | 2026-09-08 |
| 实施前提交 | `656f9dc70b23b96fb214fe8513f021f5a8e5ac6a` |
| 新增生产辅助 | `release/verify_release.py` |
| 新增测试 | `tests/release/test_release_packaging.py` |
| 未修改 | 生产服务、正式数据库、YAML、规则、求解算法、HTTP 契约 |

## 2. 实现结果

`source` 模式只接受 Git 工作树根目录，要求固定 YAML 和数据库均受跟踪、工作树干净且不存在
SQLite `-journal`、`-shm`、`-wal` 辅助文件。它验证配置仍以相对路径指向受控数据库，并输出
提交、可空分支、配置摘要、SQLite schema、活动版本、规则/原型/字典数量和两个业务指纹。
JSON 中只出现仓库相对路径，不泄露用户名或本机绝对目录。

`package` 模式接受已组装的 `APSGoV7` 目录，验证固定顶层结构、`_internal`、配置模板、种子、
清单文件集合与逐文件 SHA-256，并拒绝现场 YAML、运行数据库、辅助文件、符号链接和清单外文件。
种子还必须由现有规则加载链验证，其逻辑身份与清单完全一致。

为同时满足“生产服务代码零修改”和“源数据库强制只读”，验证器以
`mode=ro&immutable=1` 打开已确认无辅助文件的冻结 SQLite，启用并检查 `query_only`，再在同一
连接上复用 `RuleStore` 的完整 schema 校验和现有活动排程快照读取。验证器不调用初始化、迁移、
保存、恢复或 SQLite Backup API。

## 3. 专项验证

```sh
PYTHONDONTWRITEBYTECODE=1 \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider tests/release/test_release_packaging.py
```

结果：21 项通过，耗时 3.71 秒。覆盖：

- 当时验证的合法分离提交状态源码树及相对 JSON 输出；后续门禁修正另覆盖普通分支；
- 脏工作树、未跟踪数据库、绝对或越界数据库配置；
- 三类 SQLite 辅助文件、错误 schema、损坏文件、缺少活动版本及存储指纹不一致；
- 合法模拟包、普通文件摘要篡改、运行库或额外文件混入、种子身份/内容损坏和符号链接。

必需 Conda 环境中执行 `python -m ruff` 时退出码为 1，原因为该环境未安装 `ruff`；没有静默
切换解释器或安装依赖。随后使用同一 Python 完成 `py_compile`，最终 `compileall` 和累计回归
在精确暂存树的干净导出中执行，其结果记录在本阶段提交正文。

## 4. 状态边界

- 当前只建立验证器，没有增加 PyInstaller、构建脚本、启动/停止脚本或真实发布清单。
- 当前 macOS ARM64 未生成 Windows EXE，不能替代 Windows x64 构建与运行验证。
- `package` 模式使用模拟文件验证契约；真实目录需由后续阶段组装后再次验证。
