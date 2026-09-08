# 阶段 3：建立启停与规则数据库保护——源码实施证据

## 1. 范围

| 项目 | 结果 |
|---|---|
| 日期 | 2026-09-08 |
| 实施前提交 | `d2d4f9d47eca4a7b88c71175f4f88e6c0f416a52` |
| 当前平台 | macOS ARM64，zsh；不能替代 Windows BAT/PowerShell 和 EXE 验证 |
| Python | Conda `aps_3.10.18`，Python 3.10.18 |
| 本项新增 | SQLite 种子验证、构建期间漂移保护、受跟踪启停模板及回归 |
| 明确未修改 | 生产服务、正式数据库、YAML、规则、求解算法、评分和 HTTP 契约 |

## 2. 实现结果

- `release/build_exe.ps1` 复用现有 `backup_sqlite_database()` 生成一致性种子，不直接复制可能正在使用的 SQLite 主文件。
- `release/verify_release.py seed` 强制只读校验源库与种子的 schema、活动快照、规则/字典指纹和全库逻辑内容摘要；拒绝 SQLite sidecar、损坏库、符号链接、硬链接和同一文件。
- 正式构建在 PyInstaller 结束且附属文件组装后，再次验证源 Git/YAML/数据库身份，并对包内种子再校验；长时构建期间任一受跟踪输入漂移都使构建失败。
- 启动脚本只在现场 YAML 或运行库各自缺失时初始化该文件，通过同目录唯一临时文件加不覆盖原子改名避免半文件和并发覆盖；已有文件不比较、不覆盖、不合并。
- 停止脚本先按进程名缩小范围，再按当前发布目录 EXE 的规范绝对路径精确匹配；已自行退出的竞态按成功处理，仍在运行的目标最多等待 15 秒后报错。
- 启停脚本不设置 `listen_host`、`listen_port`、数据库路径或求解预算，运行参数仍只从现有 YAML 读取。

## 3. 受跟踪真实源库的临时种子对照

| 检查项 | 源库 | 发布种子 |
|---|---|---|
| 文件 SHA-256 | `8ed693ac2a1c2be5866c7b3ba8596eab58cbd9db6619a4be61c6c6af175b5288` | `97f6a80d3296448fc4fb788f6ecbe1c0c73f9d84973e13b363481943621b1588` |
| 逻辑内容 SHA-256 | `af1b7cc3d3b59793b35fc27af3b89bc4a210dfd378c394af22c5dcbb6e999ad4` | `af1b7cc3d3b59793b35fc27af3b89bc4a210dfd378c394af22c5dcbb6e999ad4` |
| SQLite | `quick_check=ok`，schema 2 | `quick_check=ok`，schema 2 |
| 活动快照 | 版本 5，基于版本 4 | 版本 5，基于版本 4 |
| 内容数量 | 17 条规则（16 启用）、27 个虚拟原型、230 条字典 | 完全一致 |
| 规则指纹 | `841c7c61895ddda0f2fb25ccc1f18fe7e3843e14da472bb816f4253d09a33028` | 完全一致 |
| 字典指纹 | `d292d5efb53ee541f4d3900b2295ababfecffb1912fd0bafe4cb8bbb7b90ec14` | 完全一致 |

SQLite Backup API 可重写物理页布局，因此两个文件摘要不同是允许的；全库逻辑内容摘要及业务身份必须相同。本次种子位于仓库外 `/tmp` 临时目录，不是 Windows 正式构建产物，不进入发布包或 Git 提交。源库校验前后 SHA-256 均为 `8ed693ac2a1c2be5866c7b3ba8596eab58cbd9db6619a4be61c6c6af175b5288`，且仍无 `-journal`、`-shm`、`-wal` 文件。

## 4. 源码侧验证

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n aps_3.10.18 \
  python -m pytest -p no:cacheprovider tests/release/test_release_packaging.py

PYTHONDONTWRITEBYTECODE=1 conda run -n aps_3.10.18 \
  python -m pytest -p no:cacheprovider \
  tests/architecture tests/api tests/app tests/core tests/service tests/release
```

发布专项退出码 0，37 项通过，Pytest 报告耗时 5.61 秒；覆盖种子生成与全历史对照、链接/损坏/sidecar/超时失败、构建前后身份复核、原子初始化、已有文件保护和精确停止文本契约。共享工作树累计回归退出码 0，3335 项通过，Pytest 报告耗时 192.86 秒。精确暂存树将按项目门禁导出后再执行同一累计范围、残留检查和 `compileall -q src release`，最终结果记录在本阶段 Git 提交正文。

临时种子对照使用绝对源/目标路径调用 `backup_sqlite_database()`，随后执行：

```sh
stage3_seed_dir=$(mktemp -d /tmp/apsgo-v7-stage3-seed.XXXXXX)
stage3_seed_path="$stage3_seed_dir/apsgo_v7_rules_seed.sqlite3"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n aps_3.10.18 \
  python -c 'import sys; from apsgo_v7_service.migrate_gqga4_grade_dictionary import backup_sqlite_database; backup_sqlite_database(sys.argv[1], sys.argv[2], timeout_seconds=5.0)' \
  /Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3 \
  "$stage3_seed_path"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n aps_3.10.18 \
  python release/verify_release.py seed \
  --source-database /Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3 \
  --seed-database "$stage3_seed_path" \
  --timeout-seconds 5.0
```

备份与 `seed` 验证退出码均为 0，合计墙钟时间 3.1 秒，完整身份如第 3 节。

首轮聚焦回归曾有两项测试/实现契约问题：服务超时值使用原生 `float` 不符合精确 JSON 编码边界；旧静态断言仍期待 `if not exist + copy`，与已实现的原子初始化不一致。分别改为精确小数输出和新契约断言后通过。后续复核又发现并修正了硬链接同文件识别、进程自行退出竞态和长时构建输入漂移三个边界。

真实种子手工对照第一次传入了相对源库路径，被现有备份函数的“必须使用绝对路径”契约拒绝；正式 PowerShell 构建从一开始便传递绝对路径。按相同正式参数用绝对路径重跑后，得到第 3 节结果。

共享工作树的残留检查退出码为 1，仅报告旧 V6 冻结清单要求但当前已不存在的 `.claude/settings.local.json`、两个旧 `dist/apsgo-*` 产物和五个 `src/apsgo.egg-info/*` 文件。本阶段不伪造这些历史残留，精确暂存树的干净导出检查仍是正式提交门禁。一次共享树 `compileall` 误调用产生的 `release/__pycache__` 已删除，不作为合规验证证据；正式 `compileall` 只在干净导出中执行。

## 5. 未关闭门禁

当前平台无法真实解析和运行 Windows BAT/PowerShell，也未生成 Windows EXE。以下必须在 Windows x64、本阶段源码提交的 detached worktree 上完成：

- 带空格路径的第一次启动与精确停止；
- 同一目录并发首次初始化，不产生半文件或覆盖；
- 已有现场 YAML/运行库在重新解压、启动和升级中保持不变；
- 保存并启用规则后停止、重启，活动版本持久化；
- 不同目录的两个实例同时运行，停止脚本只停止自身实例。

因此当前只能称“阶段 3 源码实现完成”，不能称“启停和数据库保护已在 Windows 验收通过”。
