# APSGo V7

APSGo V7 包含规则驱动的路径覆盖与确定性局部搜索求解器，以及独立的
`apsgo_v7_service` 规则管理与月计划求解服务。服务使用 FastAPI、Uvicorn 和独立 SQLite，
当前只支持 `GQGA4/default/month`，并通过可配置监听地址提供两条规则接口和一条求解接口。

详细设计和实施状态见：

- [月计划规则设置接口详细设计](docs/design/apsgo_v7_rule_setting_api_design.md)
- [月计划规则设置接口实施计划](docs/implementation/apsgo_v7_rule_setting_api_implementation_plan.md)
- [月计划求解接入与软硬钢数据准备详细设计](docs/design/apsgo_v7_month_scheduling_and_grade_preparation_design.md)
- [月计划求解接入与软硬钢数据准备实施计划](docs/implementation/apsgo_v7_month_scheduling_and_grade_preparation_implementation_plan.md)
- [链间宽差优化实施计划](docs/implementation/apsgo_v7_inter_chain_width_optimization_implementation_plan.md)

## 服务运行配置

默认配置文件为受 Git 跟踪的 `config/apsgo_v7_service.yaml`，由生产依赖 PyYAML 安全解析：

```yaml
database_path: ../data/apsgo_v7_rules.sqlite3
database_timeout_seconds: 5.0
listen_host: 0.0.0.0
listen_port: 8001
monthly_solve:
  seed: 590531
  total_time_limit_seconds: 310
  finalization_reserve_seconds: 10
  candidate_check_limit: 200000
  whole_chain_pair_scan_slack_weight: 40
  maximum_virtual_bridge_nodes: 2
```

根级五项和 `monthly_solve` 内六项配置必须完整且无未知项。相对 `database_path` 以配置文件所在目录为基准，因此上述值
解析到仓库根 `data/apsgo_v7_rules.sqlite3`，不受启动命令当前目录变化影响。配置文件缺失、
不可读、不是 UTF-8、YAML 非法、键重复或任一值非法时，命令在数据库操作或服务器启动前失败。
V7 不再读取 `APSGO_V7_RULE_DB_PATH`。

当前总时限 310 秒由 **300 秒搜索时间 + 10 秒收尾预留**组成；候选检查上限仍为 200000 次，先到达任一上限即停止搜索。

`listen_host` 允许 `127.0.0.1`（仅本机访问）或 `0.0.0.0`（监听全部 IPv4 网卡）。其他电脑
访问时，C# 客户端中的 `PipelineV7ApiBaseUrl` 必须填写服务器实际局域网 IP，例如
`http://192.168.1.20:8001`；客户端地址不能填写 `0.0.0.0`。
运行期 SQLite 主文件及 journal/WAL/SHM 文件由 Git 忽略，不得提交。C# 客户端地址继续使用
`SchedApp/App.config` 中的 `PipelineV7ApiBaseUrl`，不由该 Python 配置文件替代。

## 初始化与启动

Python 3.10 及以上，先安装项目：

```sh
python -m pip install -e '.[dev]'
```

首次部署先用显式绝对路径提供既有 V3 SQLite 源库，初始化活动规则、虚拟原型和软硬钢字典：

```sh
apsgo-v7-initialize-gqga4-rules \
  --config config/apsgo_v7_service.yaml \
  --source-database-path /absolute/path/to/aps_rule_dsl.sqlite3
```

源库会在创建目标库前以只读方式完整验证。目标库已初始化后，重复执行无需再提供源库，只核验并
返回已有活动版本，不覆盖后续版本：

```sh
apsgo-v7-initialize-gqga4-rules --config config/apsgo_v7_service.yaml
```

随后启动规则服务：

```sh
apsgo-v7-rule-service --config config/apsgo_v7_service.yaml
```

源码模块入口分别为：

```sh
PYTHONPATH=src python -m apsgo_v7_service.initialize_gqga4_rules \
  --config config/apsgo_v7_service.yaml \
  --source-database-path /absolute/path/to/aps_rule_dsl.sqlite3
PYTHONPATH=src python -m apsgo_v7_service.app \
  --config config/apsgo_v7_service.yaml
```

服务启动不会自动建库或补种子；目标数据库缺失时应先执行初始化命令。

## 旧规则库字典迁移

已有 schema v1 规则库使用显式迁移命令建立向前新版本，并先创建 SQLite 一致性备份：

```sh
apsgo-v7-migrate-gqga4-grade-dictionary \
  --database-path /absolute/path/to/apsgo_v7_rules.sqlite3 \
  --v3-database-path /absolute/path/to/aps_rule_dsl.sqlite3 \
  --backup-path /absolute/path/to/apsgo_v7_rules.pre-grade-migration.sqlite3 \
  --save-operation-id 11111111-1111-4111-8111-111111111111 \
  --expected-active-version-id 15
```

该命令不回填历史版本；迁移后的新版本保留目标库操作前的活动规则与虚拟原型，并附上经核验的
当前字典。输出中的 `source_database_sha256` 是 SQLite Backup API 生成、且实际用于读取字典的
一致性快照 SHA-256；即使 V3 源库处于 WAL 模式，也不会把主 `.sqlite3` 文件的裸哈希误当成
实际导入内容的身份。失败后以相同参数重试时，只复用 schema v1 且全部规则表内容与当前目标
完全一致的既有备份；其他同名文件不会被覆盖。

## 历史规则恢复

历史规则恢复不读取服务配置，必须由运维人员显式指定既有数据库绝对路径：

```sh
apsgo-v7-restore-gqga4-rule-version \
  --database-path /absolute/path/to/apsgo_v7_rules.sqlite3 \
  --source-version-id 12 \
  --save-operation-id 11111111-1111-4111-8111-111111111111 \
  --expected-active-version-id 15
```

恢复会把已核验的历史内容保存为向前的新版本，不修改或重新激活历史行。

## 开发验证

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider \
  tests/architecture tests/api tests/app tests/core tests/service
python tools/check_workspace_residuals.py --verify
```

共享工作区存在其他分支残留时不要在其中安装或构建；构建、`compileall` 和最终累计验证应在
精确暂存树的干净导出中执行。
