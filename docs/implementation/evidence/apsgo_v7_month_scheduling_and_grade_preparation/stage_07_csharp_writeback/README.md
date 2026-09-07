# 阶段 7：C# GQGA4 月计划求解服务与原子回写

## 1. 完成边界

- C# 分支：`codex/v7-rule-client-integration`。
- C# 提交：`7546dc05e41ca64a504bb49ae441eca7a6a59538`（`#feat 接入GQGA4月计划V7求解回写`）。
- C# 精确暂存树：`1579b381a4522ce03566f3abea6fe6694ab68385`，与上述提交树一致。
- 验证平台：macOS / Darwin 27.0.0 arm64；Python 3.10.18，Conda 环境 `aps_3.10.18`。
- 本阶段只切换 GQGA4 月计划求解与写回路径；V3 客户端、V3 求解服务及其他产线调用链未改。
- `SchedApp/Properties/licenses.licx` 和根 `.gitignore` 未进入 C# 提交。

## 2. 实现结果

1. `CalcRollPosCommandGQGA4RequestSolution` 在后台执行。选中记录只用于共享入口定位版本，命令按 `Version/ProductLine/Step=配置规则` 读取整批数据，并在内存中按 `RollSeq/Id` 稳定排序；只有服务完成事务提交并正常返回后，`ChangesSchedRecords` 才置为 `true`。
2. `ApsgoV7SchedRecordSolveService` 先即时读取活动规则版本，再构造月计划请求并调用 V7 求解接口。待排重量只取正数 `CoatingShortage`；预设大辊期取 `RollPos` 第一段并按数值升序生成 `BR_00000001` 格式期目录；重复来源和正重量虚拟输入整批拒绝。
3. 回写前在内存中校验可发布状态、服务端审计、链序和节点序、真实/虚拟来源、软硬钢值域、拆片重量与完整谱系。同一拆单分区的共同谱系必须一致，且拆成 `n` 片时必须恰有 `n-1` 个关联隔离虚拟材。
4. 取消令牌在完整结果构造后、写事务开始前最后检查。事务开始后不联网且不中途取消；旧结果删除、新结果插入、数量和 `Warnings` 逐字回读均成功后提交，任一异常回滚并继续抛给共享页面框架。
5. 共享页面入口在读取 `selectedIds.First()` 前拦截空选择；成功且记录确有变化时才推进当前步骤并刷新。仅 GQGA4/四镀锌强制解析 `Warnings` 对象，根级 `V7Metadata` 不计作报警；其他产线继续沿用原非空文本计数。

## 3. 可重复验证

在 V7 工程根目录运行：

```bash
cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_month_scheduling_and_grade_preparation/stage_07_csharp_writeback/verify_csharp_writeback_contract.py \
  --csharp-root /Users/miles/dev/dev-cs/aps-code-0806

caffeinate -dimsu \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest \
  -p no:cacheprovider \
  tests/architecture tests/api tests/app tests/core tests/service
```

实际结果：

| 检查项 | 结果 |
|---|---|
| C# 服务、命令、项目文件、汇总和空选择静态契约 | 125 项通过 |
| C# 业务库字段声明 | `SchedRecord.Warnings` 为 `varchar(255)` |
| 临时副本写入载荷 | 1054 个字符、2522 个 UTF-8 字节 |
| 关闭并重新打开临时副本后的回读 | 与原字符串逐字一致 |
| 临时副本 `PRAGMA integrity_check` | `ok` |
| C# 源数据库 SHA-256（验证前/后） | 均为 `6a6e60febeeb93dde829a1d743262aeb971e468110489c6dbbed431cc980e309`，源库未改 |
| V7 共享树累计回归 | `tests/architecture tests/api tests/app tests/core tests/service` 共 3295 项通过，181.27 秒 |
| 首轮精确暂存树导出累计回归 | 同范围 3295 项通过，184.59 秒；残留门禁及本页其他检查同时通过 |

验证脚本只以 `shutil.copy2()` 创建临时数据库副本，更新其中一条既有记录并回读；临时目录退出后自动删除。SQLite 不强制 `varchar(255)` 的长度，因此本结果证明当前 SQLite 数据链可以无损保存该长文本，但不外推到其他数据库产品或不同表结构。

## 4. 未关闭门禁与下一步

当前 macOS 没有 .NET Framework 4.7.2、MSBuild、DevExpress 20.1.3 和 WinForms Designer 运行环境。**Windows Debug/Release 构建、Designer、真实 GQGA4 页面端到端操作、失败回滚实测及其他产线仍走 V3 的运行冒烟均未验证。**

下一项是阶段 8：使用原始 531 单完成真实 V7 HTTP 求解、双审计、C# 写回、人工查询与统计图表联调；在 Windows 门禁关闭前，本专项不能标记整体完成。
