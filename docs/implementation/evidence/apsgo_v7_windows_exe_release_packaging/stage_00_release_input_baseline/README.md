# 阶段 0：固定发布输入与数据库跟踪边界证据

## 1. 范围与环境

| 项目 | 结果 |
|---|---|
| 日期 | 2026-09-08 |
| 实施前提交 | `codex/rule-setting-api-integration@90c78d2f460fd24add0f078508fdbb14cbc486dd` |
| 平台 | macOS ARM64，zsh |
| Python | Conda `aps_3.10.18`，Python 3.10.18 |
| 本阶段写入 | `.gitignore`、当前 README、实施计划、项目约定和本证据 |
| 明确未写入 | 数据库、YAML、生产代码、业务规则、求解算法、EXE 和发布目录 |

## 2. 发布输入快照

| 检查项 | 结果 |
|---|---|
| 数据库 Git 索引 | `100644 67ae76d61bf0ce186f63779fd7f41bbb3cee8dfc 0 data/apsgo_v7_rules.sqlite3` |
| 源数据库 SHA-256 | `8ed693ac2a1c2be5866c7b3ba8596eab58cbd9db6619a4be61c6c6af175b5288` |
| SQLite | `quick_check=ok`；外键违规 0；schema 2 |
| 活动快照 | 版本 5，基于版本 4；17 条规则、16 条启用 |
| 其他活动数据 | 27 个虚拟原型；230 条软硬钢字典 |
| 规则指纹 | `841c7c61895ddda0f2fb25ccc1f18fe7e3843e14da472bb816f4253d09a33028` |
| 字典指纹 | `d292d5efb53ee541f4d3900b2295ababfecffb1912fd0bafe4cb8bbb7b90ec14` |
| 服务配置 | `0.0.0.0:8001`；总时限 310 秒；收尾 10 秒；候选检查 200000 次 |

生产加载检查在临时目录中通过既有 `backup_sqlite_database()` 生成一致性快照，再使用现有
`RuleStore` 和活动规则加载器读取。SQLite Backup API 生成的文件允许采用不同的物理页布局，
所以临时备份的文件 SHA-256
`97f6a80d3296448fc4fb788f6ecbe1c0c73f9d84973e13b363481943621b1588`
不要求等于源文件 SHA-256；schema、活动版本、规则、原型、字典及业务指纹全部一致。
源数据库检查前后 SHA-256 相同。

## 3. 实际检查

```sh
git status --short --branch
git ls-files -s -- data/apsgo_v7_rules.sqlite3
shasum -a 256 data/apsgo_v7_rules.sqlite3
find data -maxdepth 1 -type f \
  \( -name '*.sqlite3-journal' -o -name '*.sqlite3-shm' -o -name '*.sqlite3-wal' \)
git check-ignore -v release/build/probe release/dist/APSGoV7/probe \
  data/probe.sqlite3-journal data/probe.sqlite3-shm data/probe.sqlite3-wal
PYTHONPATH=src /Users/miles/anaconda3/envs/aps_3.10.18/bin/python <只读数据库与配置检查程序>
```

结果：主数据库已受跟踪且工作树字节与 HEAD 一致；`data/` 中没有三类运行时辅助文件；
`release/build/`、`release/dist/` 和三类辅助文件均被预期规则忽略。从 `/tmp` 加载绝对 YAML
路径时，配置仍解析到仓库基准库，没有依赖当前工作目录。

首轮临时诊断有两次非业务失败并已保留事实：第一次遗漏 `PYTHONPATH=src`，因此无法导入项目包；
第二次将返回字段误写成不存在的 `sha256`，实际字段是 `database_sha256`。两次都只触及临时目录，
修正后的同一检查通过，源数据库 SHA-256 始终不变。

## 4. 验收边界

- 当前 macOS 只完成发布输入和源码侧门禁，不能证明 Windows EXE 可以构建或运行。
- 共享工作区执行 `python tools/check_workspace_residuals.py --verify` 后退出码为 1，仅报告旧 V6
  冻结清单要求、但当前已不存在或不再跟踪的 `.claude/settings.local.json`、两个旧 `dist/apsgo-*`
  产物及五个 `src/apsgo.egg-info/*` 文件。本阶段不伪造这些历史残留，以精确暂存树的干净导出
  检查为正式提交门禁。
- 最终精确暂存树累计测试、残留检查和退出码记录在本阶段 Git 提交正文中。
