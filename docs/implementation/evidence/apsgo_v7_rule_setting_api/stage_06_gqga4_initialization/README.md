# 阶段 6：初始化 GQGA4 活动规则版本验证

## 1. 验证边界

- 实施前提交：`7f7d12f`。
- 平台：macOS 27.0、Darwin 27.0.0 arm64。
- Python：3.10.18；SQLite：3.53.4。
- 仅验证一次性 GQGA4 初始化服务、独立命令、事务与重复执行语义。
- 所有运行使用 `/tmp` 下的新建数据库；未创建或修改正式默认数据库。

## 2. 实现结果

目标身份为 `GQGA4/default/month`。首次执行：

1. 在取得 SQLite 立即写锁后，从 `apsgo_v7_service` 包内生产种子读取并规范化 17 条规则和
   27 个虚拟材料原型。
2. 通过生产编译器生成版本 1，重新计算并权威加载规则指纹。
3. 在一个业务事务内创建规则集、初始版本、17 条逐条规则、活动指针并完成完整回读。
4. 任一步失败均回滚全部业务记录；schema 可保留并重新执行。

再次执行时，只完整核验当前活动版本并返回 `already_initialized`。若当前已经是用户保存的
版本 2 或更高版本，不编译初始版本、不覆盖、不新建、不刷新审计字段，也不回退活动指针。

## 3. 独立命令

```bash
export APSGO_V7_RULE_DB_PATH=/path/to/apsgo_v7_rules.sqlite3
apsgo-v7-initialize-gqga4-rules
```

源码环境的等价入口：

```bash
PYTHONPATH=src python -m apsgo_v7_service.initialize_gqga4_rules
```

数据库路径只从 `APSGO_V7_RULE_DB_PATH` 或默认
`data/apsgo_v7_rules.sqlite3` 取得。空白覆盖值在建库前拒绝；命令没有强制覆盖、重置、删除或
从测试文件导入的选项。

## 4. 真实命令结果

同一新临时数据库连续执行两次模块命令：

| 次数 | `status` | 活动版本 | 规则 / 原型 | 指纹 |
|---:|---|---:|---:|---|
| 1 | `initialized` | 1 | 17 / 27 | `d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0` |
| 2 | `already_initialized` | 1 | 17 / 27 | `d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0` |

数据库最终为 1 个规则集、1 个版本、17 条逐条规则，活动版本标识为 1。

## 5. 自动化覆盖

- 首次初始化：17 条规则、16 条启用、指定 1 条停用、7 项评分、唯一允许偏差、27 个原型及
  预期指纹。
- 重复初始化的数据行、活动指针、审计字段和时间不变。
- 已有用户版本 2 时仅核验并保留该版本，确认没有额外编译初始版本 1。
- 已有身份但无活动版本、已有不完整活动快照均失败且不修补。
- 规则行写入、活动指针切换、提交前完整回读三处失败均整体回滚；随后可正常重试。
- 两个重叠的真实 SQLite 初始化只产生一个初始版本。
- 环境变量路径、空白路径拒绝、模块命令重复状态和默认审计主体均有验证。

## 6. 验证结果

| 范围 | 命令 | 结果 |
|---|---|---|
| 初始化专项 | `PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python -m pytest -p no:cacheprovider tests/service/test_rule_initialization.py -q` | 11 项通过，退出码 0。 |
| 阶段聚焦 | 同一解释器运行初始化、保存服务、HTTP、存储和架构门禁测试 | 144 项通过，退出码 0。 |
| 真实命令 | 新临时 SQLite 连续执行两次 `python -m apsgo_v7_service.initialize_gqga4_rules` | `initialized` → `already_initialized`；1/1/17，退出码 0。 |
| 共享树累计回归 | `tests/architecture tests/api tests/app tests/core tests/service` | 3154 项通过，171.94 秒，退出码 0。 |
| 干净导出残留与累计回归 | 精确暂存树导出后运行残留检查及累计测试 | `clean_export` 通过；3154 项通过，172.58 秒，退出码 0。 |
| 干净导出编译与构建 | 同一导出执行 `compileall`、sdist 和 wheel 构建 | 编译、源码包和 wheel 构建通过，退出码 0。 |
| wheel 与安装命令 | 核对 wheel 文件表和入口，安装至新临时虚拟环境后连续执行两次控制台命令 | 初始化模块、两个服务命令和运行依赖完整；`initialized` → `already_initialized`，数据库为 1/1/17，退出码 0。 |

## 7. 未包含范围

- 未写入正式默认数据库。
- 未增加初始化 HTTP 路由或服务启动时自动初始化。
- 未绑定排程请求，未修改求解算法或 C# 页面。
- 下一阶段是“绑定排程任务规则快照”：每个任务启动时只读取并冻结一次活动版本。
