# 阶段 0：固定 Python 基线与数值依赖可行性

实施前提交 `7fff99791ab22725401580685598b52fc1dec205`；用户已授权按专项计划持续实施。平台 macOS ARM64，Conda `aps_3.10.18` / Python3.10.18。本阶段不改生产代码、项目依赖声明、配置或数据库；安装仅用于获授权的可行性验证。

## 1. 源码与输入

从上述提交新导出到 `/tmp/apsgo-v7-numeric-baseline.TOCMRO`，原残留检查 `clean_export` 通过，稳定16、易变992、无错误。`git diff d55dadb 7fff997 -- src tools/profile_solver_search.py` 为空，生产与计划冻结基线相同。导出本身无 `.git`，工具如实记录提交为null；其来源由本导出命令及逐文件哈希绑定，不伪造工具观察值。

完整基线与两份请求分别保存在忽略目录 `diagnostics/numpy_numba_virtual_bridge/`。`verify_baseline.py` 复用原完整观测对比函数，仅忽略原有确切计时叶子，**连缓存统计也比较**；新旧业务首差异为null。最新请求除请求号与总时限外完全相同，未把310秒输入伪装为900秒；正式请求由原已测试夹具生成并做精确回读。

| 输入 | 策略：总时限/收尾/检查数 | 请求指纹 | 绑定文件 SHA-256 |
|---|---|---|---|
| 固定工作量 | 900/10/200000 | `6609ba5853a20e3bcab137e57d51b5504659c4e7c2ea9dfc333545d13ca85a3f` | `11bdd096717414a25b14e7a219b860e6e84953f17e9c02bbf8e298da9266829c` |
| 最新现场限时 | 310/10/200000 | `3a0b4ddcddeba2e00775d6d6fae4dc36d0845236d7752ed42c9008fd42bcd673` | `ce5be9057f9f9169b58f9187c8779ab5abe2ff5fab0d256ba53508e4f35333cb` |
| 原正式质量 | 180/10/200000 | `0d9ee1cfe251306bb516a12e440120923853bf9114b57df1864880ab0ffa5a35` | `5a5c58fe4fb2844dcd021a85dbcd92417a8d9bbebcd2682bffebd600b7506e15` |

三个输入均保留种子590531。YAML与SQLite哈希仍与计划第2.2节一致，未从库重新组装输入。

## 2. Python 实测

| 范围 | 经过时间/进程CPU（秒） | 检查/完整评价/接受 | 结果 |
|---|---:|---:|---|
| 首轮局部搜索 | 39.220308 / 38.990927 | 64226/2263/34 | 自然完成首轮 |
| 完整观测调用 | 86.983092 / 86.589640 | 200000/3697/54 | 候选上限停止，双审计通过 |

最终七级 `(0,0,0,0,11726,660,23)`，零禁止、零欠重，结果指纹 `1c2820ae2fd5b19273636de8b7d33453db9089c3b600067958a79325e3c127bb`；完整有序方案、评价、轨迹与上一专项 `pair_01_new` 相同。首轮与最终缓存、拆单和资源也保留精确对照。

这是一次带既有观察包装的Python基线，不是新内核收益、真实HTTP时延或正式20对性能验收。测量使用临时 `caffeinate -i` 防空闲休眠，没有更改永久电源设置，未与累计测试并行。

## 3. 依赖与真实编译探针

冻结版本：NumPy **2.2.6**（保留原安装）、Numba **0.65.1**、llvmlite **0.47.0**；未来构建采用 PyInstaller6.22.2与 hooks **2026.6**。通过PyPI二进制安装仅新增Numba/llvmlite，`pip check`通过；未安装PyInstaller/hooks，也未编译Windows包。

- [Numba固定版本依赖](https://raw.githubusercontent.com/numba/numba/0.65.1/setup.py)和[兼容表](https://raw.githubusercontent.com/numba/numba/0.65.1/docs/source/user/installing.rst)与PyPI元数据一致支持当前Python、NumPy和llvmlite组合。选补丁版而非最新LLVM版本线，是保守选型，不代表已证明更稳定。
- [Numba](https://pypi.org/project/numba/0.65.1/#files)、[llvmlite](https://pypi.org/project/llvmlite/0.47.0/#files)、[NumPy](https://pypi.org/project/numpy/2.2.6/#files)均有未撤回的CPython3.10 macOS ARM64和Windows x64 wheel，实际URL HEAD返回200。本组合macOS下限由Numba wheel的12.0决定；Windows运行条件仍待实际包核验。
- [PyInstaller依赖声明](https://raw.githubusercontent.com/pyinstaller/pyinstaller/v6.22.2/pyproject.toml)要求hooks≥2026.6，冻结[2026.6](https://pypi.org/project/pyinstaller-hooks-contrib/2026.6/#files)。该wheel已由只读核验下载到内存校验哈希 `fd13b8ac126b35361175edacd41a0d97080b75dd5f4b594ecefefff969509dd3`，有Numba隐式导入和llvmlite动态库收集规则；阶段5还须真实构建。

`probe_numba.py` 是四行数据的独立小循环，不导入求解器；实际执行 `cache=False/fastmath=False/parallel=False/boundscheck=True`。两次独立进程均在调用前无签名、调用后有nopython签名，Python对照、同分首个、缺失掩码、修改数值/阈值后立即生效和恢复后不重复编译均通过。

| 独立进程 | 首次内核调用含编译：经过/CPU（秒） | 探针整体含依赖导入（秒） | 三次复调经过时间 |
|---|---:|---:|---|
| 1 | 0.275383 / 0.207122 | 0.569865 | 约1.4～4.2微秒 |
| 2 | 0.181187 / 0.180686 | 0.325088 | 约1.3～5.2微秒 |

微秒复调只是可调用性观察；样本太小，不计算速度倍数，不能代替正式桥接首次编译或Windows冷启。探针整体不含进程启动前的成本。64组合块初值保持，不依据这四行探针修改正式预算。

## 4. 复现命令

以下为实际命令，均使用指定Conda解释器；再次运行须换新输出目录/文件。输入核验与探针退出0，真实完整求解退出0。

```bash
cd /tmp/apsgo-v7-numeric-baseline.TOCMRO
/usr/bin/caffeinate -i env PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/profile_solver_search.py \
  --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/unchanged_chain_evaluation_reuse/frozen/20260908_205419_4a73iqo7/prepared_request.json \
  --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/numpy_numba_virtual_bridge/stage_00_full_baseline --scope full

cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/profile_solver_search.py \
  --prepared-request /tmp/apsgo-v7-diagnostic-b5abc4aa.4h2DzH/20260908_222833_8krp2ivs/prepared_request.json \
  --output-dir diagnostics/numpy_numba_virtual_bridge/stage_00_latest_check --scope check

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python docs/implementation/evidence/apsgo_v7_numpy_numba_virtual_bridge/stage_00_baseline/verify_baseline.py \
  --baseline diagnostics/numpy_numba_virtual_bridge/stage_00_full_baseline/measurement.json \
  --reference diagnostics/unchanged_chain_evaluation_reuse/stage_03_pairs/pair_01_new/measurement.json \
  --latest diagnostics/numpy_numba_virtual_bridge/stage_00_latest_check/prepared_request.json \
  --output diagnostics/numpy_numba_virtual_bridge/stage_00_verified_inputs

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pip install --only-binary=:all: --index-url https://pypi.org/simple \
  --report diagnostics/numpy_numba_virtual_bridge/install_result.json numpy==2.2.6 numba==0.65.1 llvmlite==0.47.0

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python docs/implementation/evidence/apsgo_v7_numpy_numba_virtual_bridge/stage_00_baseline/probe_numba.py \
  --output diagnostics/numpy_numba_virtual_bridge/stage_00_probe_process_1.json
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python docs/implementation/evidence/apsgo_v7_numpy_numba_virtual_bridge/stage_00_baseline/probe_numba.py \
  --output diagnostics/numpy_numba_virtual_bridge/stage_00_probe_process_2.json
```

## 5. 证据身份与边界

以下产物相对于 `diagnostics/numpy_numba_virtual_bridge/`，大型原始输出和客户订单不提交Git。

| 文件 | SHA-256 |
|---|---|
| `stage_00_full_baseline/measurement.json` | `efa1cf39613ba8d2351d8631cd59667d93ed08ab87ffff2a79172ab1c3d14a99` |
| `stage_00_verified_inputs/verification.json` | `fd375f9b47bb979c7e6bffcdffebe197a2c1d1c100b399b233844869b53bc63b` |
| `stage_00_probe_process_1.json` | `1270f8d85935c13fc760717112b46b18310d17a7fc996e9c5f5c423fc10946d4` |
| `stage_00_probe_process_2.json` | `07dc269541f21421be2ce5f072f79dd34b6ca22f93e83b5d9f42bd8278ee2495` |
| `install_result.json` | `e685eae593d7a0ed7632d8ca0366e15b3d2d7507150cdcfa0381facf2c3efdc8` |

探针源码SHA `f2714afe806754392bdc16f95b721ae1693d6ef8a2d29455b235c651e2935bdf`；输入核验工具SHA `5da932e77832ec3a812efb1f642372a0c058b62fe98e5f77b571625ca4f8a980`。后者首次执行因原比较工具的同目录导入需要 `tools/` 搜索路径而失败，未创建输出；补齐已有工具惯例后重跑通过，未修改生产。

工具Ruff检查及环境依赖检查通过；探针有内嵌可执行断言与拒绝覆盖检查。共享/精确暂存树累计与干净导出验证按计划第6节执行，实际数量/耗时记提交正文。阶段0提交后继续阶段1；当前不存在生产数值内核，不宣称优化或Windows验收完成。
