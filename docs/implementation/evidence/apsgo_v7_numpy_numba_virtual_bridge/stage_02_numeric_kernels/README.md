# 阶段 2：有界数值判边与选桥

实施前 `d5ff5838a71e9a641d056714e6b7c70b19c8e295`。生产改动仅追加在 `_bridge_numeric.py`；正式 `VirtualFactory.bridge()` 仍执行原Python代码，没有接线、日志、配置、规则、数据库或发布变更。

## 1. 实现及差分口径

四种边规则共用一个原生判定函数；每条边按原规则序全部检查，边之间才短路。相对厚度、区间首命中、epsilon、缺失值、严重度有限性及三点评分保持原式；双桥中间边计两次，严格 `<` 保留原序首个同分。

单桥、双桥共用一个编译分块函数，每块至多64个原型/有序组合；禁止的双桥首边跳过整行。只保存游标与最好索引，不建组合矩阵或第二连接缓存。原型索引、数组形状、类型、连续只读布局有边界校验；程序错误抛出，数值不安全或预算停止丢弃临时最佳。

测试沿用实际规则缓存、原桥接和原物化对象作为参照：16种启停组合、温区/宽厚epsilon、厚度区间首命中和开闭、缺失、严重度/相对容差/评分溢出；同边已禁止后遇异常仍回退，而前边禁止后不访问后边；完整单桥/双桥顺序、跨块与同分、同一原型可占两位置、64/65/129规模、取消/到时、无候选扣次及真实nopython签名。直接调用双桥验证同原型可重复，不宣称正常外层在可行单桥存在时仍进入双桥。

独立只读复核逐条对照原规则、物化、缓存和原有循环，没有发现需要修改的语义差异。阶段1未接线回归及旧精确查询/轮询测试保持；专项最终数量与共享/精确暂存树累计见本阶段提交正文。

## 2. 两次独立真实内核探针

使用本目录 `probe_kernels.py`，直接调用本次生产内核。129个原型宽度按1100/1300/1150/1250重复，原锚点1000→1400；四种边规则启用。每次单桥完整扫描均无桥，双桥最佳为目录索引 `(2,3)`，胜出物化后的全部字段与原桥接相同。每轮单桥加双桥262个块，零候选扣次。

| 观察 | 独立进程1 | 独立进程2 |
|---|---:|---:|
| 首次扫描：经过/CPU，秒 | 1.652684 / 1.635808 | 1.760753 / 1.656260 |
| 首块含JIT：经过/CPU，秒 | 1.651265 / 1.634390 | 1.759161 / 1.654669 |
| 同进程复调：经过/CPU，秒 | 0.001396 / 0.001396 | 0.001474 / 0.001475 |
| 首块之外最长块：微秒 | 22.833 | 25.625 |

两进程调用前均无编译签名，调用后均有真实nopython签名。`cache=False`、`fastmath=False`、`parallel=False`、`boundscheck=True`；本机macOS ARM64，Python3.10.18、NumPy2.2.6、Numba0.65.1、llvmlite0.47.0。

测量没有与累计测试并行；测试夹具使用固定预算时钟，这里只测真实经过/CPU时间，不验证生产编译时间扣除。扫描计时不含导入和数组准备，块计时含观察包装开销；最长块不是硬实时上限，不承诺任意规则区间数/硬件相同。**这些不是完整求解收益，也不是Windows实包结果。** 阶段3再测试真实入口的编译预算和取消，阶段4再比较完整排程。

## 3. 命令、身份与后续

```bash
cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/core/search/test_virtual_bridge_numeric.py tests/core/search/test_virtual_material_factory.py tests/architecture -q
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python docs/implementation/evidence/apsgo_v7_numpy_numba_virtual_bridge/stage_02_numeric_kernels/probe_kernels.py --output diagnostics/numpy_numba_virtual_bridge/stage_02_probe_process_1.json
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python docs/implementation/evidence/apsgo_v7_numpy_numba_virtual_bridge/stage_02_numeric_kernels/probe_kernels.py --output diagnostics/numpy_numba_virtual_bridge/stage_02_probe_process_2.json
```

两次探针退出0。重跑须用本阶段精确源码导出和新输出文件；脚本核验未接线 `virtual_material.py` 哈希，拒绝把后续已接线版本当原Python参照，也拒绝覆盖或 `-O` 关闭断言。阶段提交/精确树绑定两生产文件、探针和测试辅助源码，不只绑定运行环境。

| 原始输出（忽略目录下） | SHA-256 |
|---|---|
| `diagnostics/numpy_numba_virtual_bridge/stage_02_probe_process_1.json` | `193f055babfdca7be283b1a9c7e8716a65479dec3ee546aabe179da195bd449c` |
| `diagnostics/numpy_numba_virtual_bridge/stage_02_probe_process_2.json` | `fc01fb8c2b1755d9975bdfd54593728d1a66f0fce0c0af350a7d7586ef650356` |

探针时内核源码SHA `eeff8b4300f933e1701ace7210334e94e3ab836bcc92d020768cd9b523eb1398`，原桥接源码SHA `db91df4d5612b489bf99c01a7df1ba32e3f24b71cf93ae3d7161d1c644b7b98a`。配置、SQLite哈希保持计划原值。最终共享/精确暂存树同范围累计、原残留clean_export、Ruff和diff检查按计划第6节执行，数量、耗时、退出码与暂存树身份见阶段提交正文；不复跑阶段0全量基线充当本阶段收益。

独立提交后继续阶段3：接入唯一桥接入口并保留原Python回退。
