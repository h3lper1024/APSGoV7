# 阶段 9：正式规则数据库迁移与服务恢复

## 1. 完成边界

- 执行日期：2026-09-07。
- 实施前代码提交：`377ebc28baeca1d304baa621d20f7c84f96bbbb3`；分支
  `codex/rule-setting-api-integration`。
- 平台：macOS / Darwin arm64，zsh；Python 使用 Conda 环境 `aps_3.10.18`。
- 用户确认服务可直接停止、当前无人访问，并指定备份目录
  `/Users/miles/dev/dev-py/APSGOV7-bak`；迁移前确认端口 8001 无监听，正式数据库无进程持有。
- 本阶段只迁移正式 V7 规则数据库、补正已有虚拟原型并恢复 V7 服务，不修改 V3 源库、C# 数据库、
  求解算法、规则业务参数或搜索预算。
- 本阶段没有再次执行完整 GQGA4 求解。300 秒策略的完整功能复测仍以阶段 8 的 `run_06` 为准，
  不将本次规则 GET 冒烟写成求解验收。
- 未执行 Windows Debug/Release 构建、WinForms Designer、真实页面点击、真实回写或第二台电脑访问；
  这些项目仍未验收。

## 2. 迁移前保护

### 2.1 目标和来源

| 对象 | 绝对路径 | 迁移前状态 |
|---|---|---|
| 正式 V7 规则库 | `/Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3` | schema v1；活动版本 1；1 个规则集、1 个版本、17 条规则定义 |
| V3 软硬钢字典源库 | `/Users/miles/dev/dev-py/apsgo-v3/data/aps_rule_dsl.sqlite3` | 主文件 SHA-256 `2c4e44c4b4c2060cb54890217ea7097164b4c88e25a452796a931883df4cce7e` |
| 迁移前备份 | `/Users/miles/dev/dev-py/APSGOV7-bak/apsgo_v7_rules_before_schema_v2_20260907_201925.sqlite3` | 迁移命令通过 SQLite Backup API 创建；权限 `0600` |

正式 V7 库迁移前 SHA-256 为
`af15c62d5920dee93da0bc1dda9ab519d04829c142532025d43f7837737342d0`，
`PRAGMA integrity_check` 为 `ok`，`PRAGMA user_version` 为 1。备份目录权限为 `0700`。

备份 SHA-256 为
`bc9088995333c79c8cddb626d9951af1283861d278319259784f5f86baaa8bf6`。
它与迁移前主文件的裸字节哈希不同，是 SQLite Backup API 生成的一致性逻辑快照；后续通过表级核对和
恢复演练证明其内容与迁移前逻辑对象一致，不能仅凭两个文件的裸字节不同判定备份失败。

### 2.2 迁移输入

迁移器从 V3 数据库的一致性快照读取 GQGA4 字典；该快照 SHA-256 为
`f4164be3f87239562adeb76e8672b7f16d92d2a40192e7b8d895f5d0e4325d1f`。输入共 230 条，
全部启用，其中软钢 131 条、硬钢 99 条；字典指纹为
`d292d5efb53ee541f4d3900b2295ababfecffb1912fd0bafe4cb8bbb7b90ec14`。

## 3. 正式迁移和原型补正

### 3.1 schema v1 升级和字典迁入

执行命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m apsgo_v7_service.migrate_gqga4_grade_dictionary \
  --save-operation-id 33600667-81e3-4d98-b26f-00958c63c532 \
  --expected-active-version-id 1 \
  --database-path /Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3 \
  --v3-database-path /Users/miles/dev/dev-py/apsgo-v3/data/aps_rule_dsl.sqlite3 \
  --backup-path /Users/miles/dev/dev-py/APSGOV7-bak/apsgo_v7_rules_before_schema_v2_20260907_201925.sqlite3
```

命令返回 `status=migrated`，将数据库升级为 schema v2，并基于版本 1 建立活动版本 2：

- 保存操作标识：`33600667-81e3-4d98-b26f-00958c63c532`；
- 规则集指纹：`28b65b6aa28a9a773d525abcae8e3de74e73886f20876ed7a16a7becfbadfa6c`；
- 230 条字典写入版本 2，分类数量和字典指纹与迁移输入一致；
- 版本 1 的规则、原型、编辑快照和备注未修改；
- 此步结束时正式库 SHA-256 为
  `e1281f582b7f2631ff26104b8fe08584859014fff7481ae0637c33fcaf30a40f`。

### 3.2 虚拟原型向前补正

字典迁入后，复用现有 `get_active_gqga4_rules()` 和 `set_active_gqga4_rules()` 保存并启用事务，
基于活动版本 2 建立版本 3。保存操作标识为
`8e1e1fda-0e46-42bc-943f-446b58e56b51`，审计主体为
`v7-stage09-production-migration`。

本次补正对 27 个既有虚拟原型分别执行以下唯一业务变化：

```text
原 rule_attributes + hot_roll_grade=SPHC
```

这里是合并属性，不是用默认原型覆盖原记录。原型标识、牌号、重量、宽度、厚度、温区及其他已有
属性逐字段保留；`soft_hard_class` 继续为空，因为生成型虚拟材不参与订单软硬钢字典拼接。规则、
七级质量声明、允许的最终偏差代码和备注均未改变。

## 4. 迁移后历史版本审计

正式库最终状态：

| 检查 | 结果 |
|---|---|
| SQLite 结构 | `PRAGMA user_version=2` |
| 数据完整性 | `PRAGMA integrity_check=ok`；`PRAGMA foreign_key_check` 返回 0 行 |
| 规则集 | `GQGA4 / default / month`，活动版本 3 |
| 规则 | 每个版本均为 17 条，16 条启用、1 条停用；三版定义逐字段相同 |
| 质量声明 | 7 项 |
| 虚拟原型 | 每个版本均为 27 个；版本 3 的 27 个原型均有 `hot_roll_grade=SPHC` |
| 软硬钢字典 | 版本 1 为 0 条；版本 2、3 各 230 条，均为软钢 131、硬钢 99，明细相同 |
| 最终文件 SHA-256 | `8659e4a63f496e40ce33baa39531e068f307cae82eb6d48cf101c47206860de5` |

不可变版本链为：

| 版本 | 基于版本 | 保存操作标识 | 规则集指纹 | 变化 |
|---:|---:|---|---|---|
| 1 | 无 | `bootstrap:gqga4:v1` | `d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0` | 迁移前历史版本，保持不变 |
| 2 | 1 | `33600667-81e3-4d98-b26f-00958c63c532` | `28b65b6aa28a9a773d525abcae8e3de74e73886f20876ed7a16a7becfbadfa6c` | 继承规则和原型，增加 230 条字典 |
| 3 | 2 | `8e1e1fda-0e46-42bc-943f-446b58e56b51` | `fd313421b74603e10a942d60f05fffc1b58d41f41677b1579ad14c8ec1863607` | 仅合并虚拟原型热轧牌号属性 |

交叉核对结果：

- 版本 1 和版本 2 的 `editor_snapshot_json`、27 个虚拟原型及备注逐字相同；编译结果只因版本身份和
  指纹变化而不同。
- 版本 2 和版本 3 的规则定义、质量声明、允许偏差代码、备注及 230 条字典逐字段相同。
- 版本 2 和版本 3 的 27 个虚拟原型，除 `rule_attributes` 合并
  `hot_roll_grade=SPHC` 外没有其他差异；三个版本的原型都没有写入 `soft_hard_class`。
- 历史版本 1、2 仍可审计，没有原地更新或删除历史行。

## 5. 备份恢复演练

演练使用 SQLite Backup API 将正式备份恢复到独立临时文件
`/tmp/apsgo-v7-backup-restore.g7C1Si/restored.sqlite3`，没有覆盖正式库。结果如下：

| 检查 | 结果 |
|---|---|
| 恢复文件 SHA-256 | `bc9088995333c79c8cddb626d9951af1283861d278319259784f5f86baaa8bf6`，与备份相同 |
| SQLite 完整性 | `integrity_check=ok` |
| 恢复版本 | schema v1；活动版本 1；1 个版本、17 条规则、27 个原型 |
| 字典表 | 不存在，符合迁移前 schema v1 |
| 逻辑一致性 | schema、迁移前三张规则表的逐行哈希及规则逻辑对象均与备份一致 |

临时恢复文件在验证后删除，永久恢复点仍是权限 `0600` 的备份文件。若正式库需要回滚，必须先停
服务、另存当前 schema v2 数据库，再从该备份恢复到正式路径并重新设置 `0600` 权限。当前 V7
服务只接受 schema v2，因此回到 schema v1 时还必须同时切回兼容 schema v1 的应用版本，或重新
执行本迁移；不能把“文件恢复成功”等同于“当前应用可直接启动”。

停服并确认正式库没有打开句柄后，可用下面的标准库脚本执行文件级恢复。脚本先把回滚前的
schema v2 库另存为第二恢复点；任一目标文件已存在或发现 SQLite 边车时都会停止，不静默覆盖：

```bash
cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python - <<'PY'
import os
import sqlite3
from pathlib import Path

target = Path('/Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3')
backup = Path('/Users/miles/dev/dev-py/APSGOV7-bak/apsgo_v7_rules_before_schema_v2_20260907_201925.sqlite3')
safety = Path('/Users/miles/dev/dev-py/APSGOV7-bak/apsgo_v7_rules_before_rollback.sqlite3')
restored = target.with_suffix('.restore.tmp')
assert target.is_file() and backup.is_file()
assert not safety.exists() and not restored.exists()
assert all(not Path(f'{target}{suffix}').exists() for suffix in ('-wal', '-shm', '-journal'))
with sqlite3.connect(target) as source, sqlite3.connect(safety) as destination:
    source.backup(destination)
with sqlite3.connect(f'file:{backup}?mode=ro', uri=True) as source, sqlite3.connect(restored) as destination:
    source.backup(destination)
with sqlite3.connect(f'file:{restored}?mode=ro', uri=True) as database:
    assert database.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert database.execute('PRAGMA user_version').fetchone()[0] == 1
os.chmod(safety, 0o600)
os.chmod(restored, 0o600)
os.replace(restored, target)
PY
```

## 6. 服务恢复与 GET 冒烟

使用受跟踪 YAML 和指定 Conda 环境启动：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m apsgo_v7_service.app --config config/apsgo_v7_service.yaml
```

进程实际监听 `TCP *:8001`，即配置 `listen_host: 0.0.0.0` 已生效。随后分别调用：

```text
http://127.0.0.1:8001/api/v1/rule-sets/GQGA4/default/month/getActiveRules
http://192.168.4.42:8001/api/v1/rule-sets/GQGA4/default/month/getActiveRules
```

两个地址均返回 HTTP 200，响应均为 11905 字节，SHA-256 均为
`54af35352259f780b1a773adc98c48e7c708654befa42e9128ab9ac0249c036a`，字节完全一致。回读内容为：

- 活动版本 3，基于版本 2；
- 规则集指纹 `fd313421b74603e10a942d60f05fffc1b58d41f41677b1579ad14c8ec1863607`；
- 17 条规则、16 条启用、7 项质量声明；
- 27 个虚拟原型全部包含 `hot_roll_grade=SPHC`，没有原型写入 `soft_hard_class`。

`192.168.4.42` 冒烟是同一台 macOS 主机通过自身局域网地址完成，只证明服务已绑定所有 IPv4 接口
且该地址可在本机回读；不能替代第二台电脑的网络、防火墙和 C# 客户端实测。

## 7. 搜索预算保持

本阶段没有修改 `config/apsgo_v7_service.yaml`。服务重启时继续加载：

| 配置 | 值 | 口径 |
|---|---:|---|
| `total_time_limit_seconds` | 310 | 求解全流程总时限 |
| `finalization_reserve_seconds` | 10 | 最终审计和结果整理预留 |
| 实际搜索时限 | 300 秒 | 310 减 10 |
| `candidate_check_limit` | 200000 | 候选检查上限 |

阶段 8 的 `run_06` 已在 222.596230 秒达到 200000 次候选检查上限并通过功能门槛；本阶段只确认
迁移后的服务继续读取相同配置，没有再次执行完整求解，也不新增或变更性能验收结论。

## 8. 工作区门禁和未完成项

首次在共享工作树运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/check_workspace_residuals.py --verify
```

结果为 `status=fail`。失败原因是冻结清单仍要求已经不存在的旧 V6 稳定残留，包括
`.claude/settings.local.json`、旧 `dist/apsgo-0.1.0.dev0` 构建包和旧
`src/apsgo.egg-info` 元数据；不是本次数据库迁移产生的新文件或数据错误。本阶段不为通过检查而
重建、提交或伪造这些历史残留，后续提交验证以精确暂存树的全新干净导出残留门禁为准。

本阶段实际验证结果：

| 范围 | 结果 |
|---|---|
| 迁移、规则生命周期、初始化、任务绑定、配置和月计划 HTTP 聚焦回归 | `130 passed in 5.10s` |
| 共享工作树累计回归 | `3298 passed in 179.57s` |
| 首轮精确暂存树 | `afd73f95812e480b2f8bb908be34c50e7d2e54d9` |
| 首轮干净导出残留和文档检查 | 通过；7 份变更文档均为 UTF-8 无 BOM、围栏成对、相对链接存在 |
| 首轮干净导出累计回归 | `3298 passed in 181.16s` |
| 首轮干净导出编译和构建 | `compileall` 通过；wheel 构建成功，SHA-256 `7bf92d03573699828a44ad02fd474156c612dcb720ee3ac7d195a233cf2a7b1a` |
| 差异检查 | `git diff --check` 通过 |

最终证据文本写回后重新导出最终暂存树，按相同残留、文档、累计回归、编译和构建范围复验；
最终树及提交身份以 Git 历史和提交正文为准，不在提交内容中自引用。

仍未完成：

1. Windows Debug/Release 构建及 WinForms Designer 打开验证。
2. 第二台电脑通过局域网访问 `192.168.4.42:8001`。
3. C# 真实页面发起完整求解、成功回写及失败回滚验证。
4. 既定 20 对性能样本；本阶段不以一次 GET 或阶段 8 的单次功能复测代替它。

## 9. 结论

正式 V7 规则库已从 schema v1 向前迁移到 schema v2，230 条 GQGA4 软硬钢字典和 27 个虚拟
原型热轧牌号已通过两个不可变向前版本落地，历史版本、规则和用户原型字段均保留。迁移前备份已
完成独立恢复演练，服务已使用活动版本 3 恢复 `0.0.0.0:8001` 监听，并通过本机回环地址和本机
局域网地址的规则 GET 冒烟。Windows 页面、第二台电脑和完整 C# 回写仍须单独验收。
