# 阶段 0：对照输入与测量入口

2026-09-08；实施前 `3c81a698fbea48e223e3b532dcde976a25379f02`，分支 `codex/solver-performance-optimization`。用户已授权按专项计划持续实施。本阶段仅新增诊断工具及测试，不改生产代码。

## 已冻结的对照

- 源码为上述提交的干净导出 `/tmp/apsgo-v7-perf-stage0-baseline.yzgYhD`；生产文件集合 SHA-256 为 `dc617aa5a5061e8eca9acf80713b7d9b586e93510136c1c8acf70d63d8c14579`，与本阶段工作树的生产源码相同。导出没有 Git 元数据，工具不会借用上层目录提交；导出提交由本记录和实际命令绑定。
- 原诊断包保存在忽略目录 `diagnostics/performance_optimization/frozen/0f7d1fe8-f384-4c78-a5ac-77912cb0c82a.7z`；SHA-256 仍为 `af5675deb746116b1971666ef6fdbf3a7ea0ffe7a54c4ad673970e82887302fe`。
- 最终基线目录为 `diagnostics/performance_optimization/stage_00_baseline_run_02/`，包含原始绑定输入、输入/代码/工具身份及完整首轮方案/轨迹。
- `prepared_request.json` SHA-256：`29ed173cbfec0d4b6130da87e8513f0a3a1b3b4c9c50f3b501b2551c2026b383`。
- `input_identity.json` SHA-256：`f2d66b952240bcb021056c48c12cd535c0cace858dc4dd150bbaadede11a0bbf`。
- `measurement.json` SHA-256：`32b64410266600df6dc073763c38011c8a267bcef92bf1a02bb3b2548a226a02`。
- 实测工具 SHA-256：`860d831a7123464e1cc21d508bc7a60958202d0a1562c4296eaaedb08b0826f2`，对应本阶段提交的 `tools/profile_solver_search.py`。

订单和完整方案不纳入本次 Git 提交；上述目录及原始上传包均保留。临时目录不是唯一恢复入口：后续可从本阶段提交导出工具/源码，以保留的绑定输入重放。输入丢失时从所列原始 7z 重建并核验哈希，不根据旧结果反推订单。

## 实际复现结果

| 项目 | 结果 |
|---|---|
| 平台 / Python | macOS ARM64 / Conda `aps_3.10.18` / Python 3.10.18 |
| 策略 | 原 900 秒总时限、30 秒收尾、200000 次候选、种子 590531；没有覆盖策略 |
| 首轮经过 / CPU 时间 | 102.523736 / 102.462803 秒；未开启 `cProfile` |
| 整链 / 真实节点 / 补重 / 调序 | 80.899988 / 0.873957 / 14.037874 / 6.711812 秒 |
| 检查 / 完整评价 / 采纳 | 64226 / 2263 / 34 |
| 首轮质量 | `(2, 170.3, 2, 354.87, 11186, 480, 22)`；不是可发布最终解 |
| 首轮停止 | `local_search_complete`，没有墙钟截断 |
| 最终边缓存累计命中 / 未命中 / 条目 | 1354923 / 176002 / 176002；包含构图前缀，不混称首轮增量 |
| 请求指纹 | `f998fdf6f44795326324d6739e94a664841218acd77a3ef9436924be6da85581` |
| 问题指纹 | `ab8bb3ad7588a9d5431ac1b5d3baf6370d78238f57b5382367aac3c6e196720e` |
| 首轮方案指纹 | `0ca1ec1d350776d29556039599d5febf277a04de78b17174ae7c03133f59ced8` |
| 首轮采纳轨迹指纹 | `09b386d2b5b4032380b742c77e9079ddfa2916228cfdf16c2333e7c07c632053` |

首轮计数、停止原因、方案/轨迹/问题指纹与此前 `baseline_verified.json` 一致；两次本阶段运行的完整 `first_search` 数据也相同。首次观测 `stage_00_baseline_run_01/` 为 102.606091 秒，保留其当时工具副本；输入冻结保护及导入格式收紧后重新运行得到最终 `run_02`，不替换原样本。两次均只到首轮，不包含拆单、最终审计或发布，不能用首轮质量判断最终求解失败。

完整 `first_search` 对象经既有 `dumps_exact_json()` 编码后的 SHA-256 为 `f1edd543c6f915331dc1b897e760add883d771749b47bc3e9279192be7bbcd5e`，覆盖初始/首轮方案、轨迹、计数及缓存统计，不包含时间字段；后续固定工作量对照使用此边界。

## 工具边界与命令

工具复用公共 `solve_request()`、已有 JSON 精确序列化和源码身份辅助函数。读取源文件一次，校验、归档及实际请求使用同一份字节；校验请求与规则指纹，拒绝已有输出目录。无数据库连接、规则重新绑定或 HTTP 服务启动。

以下命令从 V7 根目录运行，新运行必须换一个不存在的输出目录：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/profile_solver_search.py \
  --prepared-request diagnostics/performance_optimization/stage_00_baseline_run_02/prepared_request.json \
  --output-dir diagnostics/performance_optimization/manual_first_01 \
  --scope first
```

- `--scope check`：只校验及冻结输入，不求解。
- `--scope first`：进入原公开流程，只截取首轮，明确没有审计或发布。
- `--scope full`：执行完整公开流程，另存实际 `result`，保留双审计。退出码 0 仅表示观测执行完成，业务是否可发布必须读取结果。
- `--profile`：仅分析首轮，另存 `.pstats` 及函数报告；不能把该次耗时作为普通计时样本。
- `--total-time-limit-seconds`：只在确需固定工作量时显式使用，原策略和测量策略分别留档；不修改 YAML。当前基线未使用此参数。

实际最终运行使用基线导出的工具路径及绝对输入/输出路径，并由 `/usr/bin/caffeinate -i env PYTHONDONTWRITEBYTECODE=1 ...` 包裹以防空闲休眠；原始 900 秒配置已足够完成首轮，没有人为延长。计时不包含进程启动/import、输入复制或报告写盘，完整公开调用时间另有字段，不能称为端到端 HTTP 时间。

## 验证及修正

- 开工干净导出残留检查：`clean_export` 通过，退出码 0。
- 工具聚焦：7 项通过，0.45 秒；覆盖精确还原、错误身份、首轮截取、完整双审计/原始结果指纹一致、参数显式覆盖、禁止覆盖结果及源文件变化保护。
- 首次聚焦收集失败为指纹辅助函数导入位置错误（退出 2）；已改为复用 `app.rule_set_loader`。独立审查另发现重复读取输入的证据绑定风险，已修正并补测试。
- Ruff、`git diff --check` 通过；累计共享树及精确暂存树的实际数量、时间和退出码见本阶段提交正文。
- YAML 及正式 SQLite 的哈希仍与计划第 2.1 节相同。未运行完整真实排程、Windows EXE 或正式性能验收。

下一步为阶段 1“同次桥接节点复用”，生产修改限定 `VirtualFactory.bridge()`，不把本阶段基线观测当作优化已实现。
