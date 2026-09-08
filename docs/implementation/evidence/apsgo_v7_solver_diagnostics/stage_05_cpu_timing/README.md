# 阶段 5：进程 CPU 计时接入与回归记录

## 身份、范围与状态

- 实施前提交：`0ce9c2efcd6c7be34d7f1ea915f024f8d08d025b`；分支 `codex/rule-setting-api-integration`，开始时工作树干净。
- 本机环境：macOS ARM64、Darwin 27.0.0；Python 3.10.18，解释器 `/Users/miles/anaconda3/envs/aps_3.10.18/bin/python`。
- 执行依据：[实施计划第 8 节](../../../apsgo_v7_solver_diagnostics_implementation_plan.md)。沿用现有诊断模块，不新增依赖、配置、采集线程或函数级剖析。
- 生产修改仅 `src/apsgo_v7_service/app.py`、`diagnostics.py`；测试扩展既有两个诊断测试文件。求解算法、规则、预算、核心结果、HTTP 契约、正式数据库和 C# 未改。
- 测试使用临时数据库和进程内 HTTP 客户端，不启动用户的现场服务。本项没有重新执行真实 531 单或 20 对性能验收；Windows 实包留到阶段 6。

## 已实现

1. 请求进入时同时建立经过时间和 CPU 起点，并读取一次逻辑处理器数量；原求解预算仍使用原时钟。
2. 求解日志增加累计 `process_cpu_seconds`；结束日志和摘要增加 `cpu_core_equivalent`、`cpu_count`、`machine_cpu_percent_estimate`。未知值为 `null`/`-`，不猜核数、不把失败采样当作 0。
3. 原始差值先计算比值，再保留六位小数；摘要沿用精确 JSON，原经过时间精度和业务身份不变。进程 CPU 包含其他线程，阶段分析用同一对日志的 CPU 差 / 经过时间差。
4. 保留取消后的工作线程收尾、重复请求隔离及原摘要写入顺序。摘要采样不含自身完整写盘；其后的共享结束日志再记录收尾，两者不强求相等。
5. 复核发现前置请求错误分支原来直接调用外部日志处理器；现在两条显式日志复用小型安全输出函数。处理器抛异常时，直接向标准错误输出原日志及已保存的原异常追踪，不改变本应返回的 400/503 等 HTTP 错误，也不引入新日志框架。

## 实际验证

以下运行均设置 `PYTHONDONTWRITEBYTECODE=1`，pytest 使用 `-p no:cacheprovider`。

| 验证范围 | 结果 | 耗时 / 退出码 |
|---|---|---|
| 两个诊断专项文件 | 54 项通过 | 2.56 秒 / 0 |
| `tests/service tests/release tests/architecture` | 471 项通过 | 20.73 秒 / 0 |
| 原生 Ruff 检查四个源码/测试改动文件 | 全部通过 | 退出 0 |
| 共享树累计六个测试目录 | 3463 项通过 | 199.49 秒 / 0 |
| 精确暂存树导出累计及残留检查 | 提交前执行；最终数量、耗时及结果见本提交正文 | 不以专项代替累计 |

专项命令：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/service/test_solver_diagnostics.py tests/service/test_diagnostic_lifecycle.py
```

累计命令（分别在共享树、精确暂存树的干净导出目录执行）：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service tests/release
```

覆盖 CPU 小于/等于/大于经过时间、零分母、未知核数、非有限/倒退/失败采样、缺失起点不能后来补建、多日志处理器不改共享记录；真实 HTTP 覆盖成功、不可发布、输入失败、绑定冲突、响应映射失败、取消、重复标识、规则查询并行、429 与前置 400/422/413/415/503/500。受控时钟验证请求正文/解析至关闭日志、摘要、最终结束记录的计时顺序；诊断开关和输出故障不改变稳定响应、轨迹及计数。

本机只测计时函数的小样本：先休眠 0.03 秒，另一次执行 `sum(i * i for i in range(200000))`。两次各自取新起点，实际输出如下：

```text
sleep_sample elapsed_seconds=0.034167 process_cpu_seconds=0.000022 cpu_core_equivalent=0.000644 cpu_count=12 machine_cpu_percent_estimate=0.005366
compute_sample elapsed_seconds=0.007357 process_cpu_seconds=0.007323 cpu_core_equivalent=0.995311 cpu_count=12 machine_cpu_percent_estimate=8.294258
```

这只验证本机接口可以区分等待与实际计算，不是 Windows 性能测量或算法提速证据；日志里的处理器数量 12 来自本机，不是用户 Windows 虚拟机的数量。

共享目录残留检查仍退出 1，错误与实施前相同：8 个既有 V6 历史稳定文件缺失及其集合校验。没有恢复这些旧文件、改检查脚本或冻结清单；提交按项目既有规则另做干净导出检查，结果记录在提交正文。

## 阶段 6：Windows 待验收入口

本机为 macOS，现有 `release/build_exe.ps1` 明确要求 Windows x64；未执行 Windows 构建或替代性跨平台打包。按新提交在 Windows 仓库根目录执行（每条成功后再运行下一条）：

```bat
release\build_exe.bat -CheckOnly
release\build_exe.bat -Clean
```

`-Clean` 只用于构建目录，执行前应保留所需旧构建产物。不要用新包覆盖原现场配置或规则数据库；按原发布说明使用完整发布目录和测试库/配置副本。保持已经确认的实际输入、规则和预算，先验证小请求，再运行 GQGA4；保存新 `release_manifest.json`、本次运行目录和共享日志中的 `month_solve_worker_finished`。

审核时重点看累计经过时间、进程 CPU 时间、平均使用核数以及同一阶段的检查次数；旧诊断包没有 CPU 字段，不补造历史 CPU 数据。阶段 6 尚未完成，不能把本地测试写成 Windows 已通过或已确定现场慢因。
