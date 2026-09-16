# 阶段 7：完整数值评价内核

## 7.1 契约与迁移参考（实施前 a03fdf5）

当前授权持续实施 7.1～7.4，完成串行接线后停止，不进入阶段 8。Ponytail 已关闭。本机为 macOS arm64，全部运行使用 Conda `aps_3.10.18`。

迁移参考冻结在 `tests/core/numeric_migration_reference_rules.py` 和 `numeric_migration_reference_evaluation.py`：直接保留实施前整数计算函数，借用生产边界类型，不是旧 Decimal 口径，也不允许生产导入。24 个固定小方案的全部规则明细、资源、时钟及九级评分一致。

真实窗口通过 `tools/verify_numeric_kernel_migration.py` 正常执行冻结请求前置流程取得，不新增恢复接口、不重置预算。`diagnostics/numpy_numeric_search_and_calculation/stage7_reference_window_01/window.json` 已保存 256 个实际后置精修候选：记录候选序号、方案代次/身份、任务/规则身份、结构描述、资源序号和质量。工具每次重新执行原前缀，因此搜索内部游标也由原流程产生，而不是从不完整快照猜测恢复。

### 数值契约与覆盖表

规则覆盖沿用阶段 0/2 的全部 20 类编译支持，不限 GQGA4 正式启用组合。内核只接收只读数值数组和原生标量；规则配置对象、字符串身份、回调、对象数组留在边界。

| 规则/事实 | 输入列/参数 | 汇总输出 | 明细输出/验证 |
|---|---|---|---|
| 5 类边规则 | 宽厚、缺值、温区、角色、软硬/热轧编码；区间开闭/首命中与容差 | 禁止数/严重度/规则命中数 | 规则、原因、链及相邻位置；阶段 2 全规则边界 + 迁移差分 |
| 高表面/窄钢/同规格/虚拟连续 | 派生匹配列、分组、重量/数量阈值 | 最大段及禁止汇总 | 每段起止、严重度，空表面断段 |
| 链重/逆宽计数/连续逆宽/跨虚拟端点 | 链节点偏移、重量、宽度/缺值、角色、阈值 | 欠重数/逐链两位缺口、禁止汇总 | 按原规则顺序与主体位置保留 |
| 延后期/虚拟比例/填充/链间宽差 | 来源/排产期、链重、真实/虚拟重、首尾宽 | 禁止及指标汇总 | 比例分子分母、逐个延后节点、跨期边界 |
| 构造优先级 | 派生优先级数值列 | 节点指标合计，不进评分 | 同规则指标编号 |
| 交期/资源 | 原单映射、节点工时、原单交期/旧欠、重量 | 原单完成、旧欠清空秒、吨秒负担、真实/虚拟/借用重 | 原单末片完成、毫秒时钟；两类拆单/桥接沿用资源测试 |
| 受控拆分资格 | 角色、窄钢、片重/数量、模式和目标期 | 不额外进入方案评分 | 保留既有授权事务；完整候选评价覆盖授权后片段 |

固定返回状态区分成功、结构无效、数值错误、取消和明细容量不足；不得把错误当普通拒绝。质量输出固定九项，按既有声明重排。链缓存保存原始数值汇总、事实和规则命中数；仅可信未变链可复用。明细缓冲列包含规则、原因/指标、链/位置、严重度/数值及处置；容量不足由外层扩容重算，不能截断。

### 验证

- `PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/core/test_numeric_kernel_migration.py`：1 项通过，内含 24 个完整方案。
- 同环境执行 `tools/verify_numeric_kernel_migration.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --output-dir diagnostics/numpy_numeric_search_and_calculation/stage7_reference_window_01`：退出 0，256 个真实完整候选逐项一致。
- 本项未改生产求解器，不宣称内核或性能完成。共享残留仍为已登记的 8 项缺失；正式配置/数据库和 4 份外部改动文件保持不动。

## 7.2 权威编译内核（实施前 a3e9e8a）

新增私有 `_numeric_kernel.py`：编译规则表只准备一次；同一组原语处理边、链、方案规则、资源、原单末片完成时钟和九级评分。Numba 实际以无 Python 对象模式运行，测试断言 `evaluate_kernel.nopython_signatures` 非空。输入全部为原生只读列/标量，返回固定数值数组；NumPy/Numba 架构例外只加入这个精确私有模块。

内核原生循环中显式检查整数加、减、乘、绝对值越界；四舍五入使用商和余数，欠重先除精确倍率，避免无意义的中间乘积溢出。无效结构、数值错误、取消、容量不足使用不同状态。汇总与明细调用同原语，明细不足只报告所需容量，不丢弃违规。完整九项按原声明顺序输出，不额外裁决。

必要验证：

- 小型迁移测试扩展至 8 项：24 个候选排列/分链、24 个多规则排列、连续虚拟端点/延后期、5 种软硬缺值策略、相对厚度首命中及开闭边界、半入舍入、整数越界与状态。
- 小样例逐项核对全部评分、逐链事实、时钟数组、违规顺序/定位/严重度和全部指标分子分母；汇总与明细完全一致。
- `tools/verify_numeric_kernel_migration.py ... --native` 正常运行前缀并再次核对 256 个真实后置精修候选，退出 0；保存于 `diagnostics/numpy_numeric_search_and_calculation/stage7_native_window_01/window.json`。首次编译包含在诊断运行内，不据此宣称性能收益。
- 未接入生产，下一项 7.3 处理数值汇总适配、接受后明细映射和未变链缓存。

## 7.3 汇总与延迟明细（实施前 3c13b78）

新增数值汇总入口与明细边界映射，不改变原搜索调用。普通汇总只返回内核数值数组；测试把完整评价、规则结果、违规和指标构造器全部替换为失败桩，仍可完成覆盖候选评价。未变链匹配在编译内核内按稳定链标识、期和节点数组执行，保留原始链事实、评分、规则命中数和明细计数；可信只追加任务可复用，其他任务全量重算。

明细缓冲从 16 条违规/32 条指标开始；不足时返回真实所需容量，由边界扩容重跑同一原语。接受前核对评分、事实、规则命中、时钟及各项计数；之后才创建原有明细类型。方案级延后规则虽然有链内主体位置，仍归方案汇总，避免污染未变链缓存。独立审核将使用无缓存明细入口。

专项检查含拒绝零对象失败桩、只重算 2 条变化链、完全相同方案复用 0 条、方案延后指标不重复及全部明细顺序差分。最终共享/精确树结果记录在本项提交正文；生产切换和全量回归仍属 7.4。

## 7.4 生产串行接线（实施前 ea93072）

首轮搜索、动态桥接/填充、两类拆分及唯一重放、后置结构精修和普通连接材回收已统一接入数值汇总；首改善接受后才调用同一原语生成明细，并核对全部数值数组。构造阶段和桥接/拆分预检也只取数值禁止轮廓；纯链调序预览复用相同的原单时钟和链间宽差原语。独立审核仍从冻结事实重建任务和最终方案，不读取搜索缓存。

原 Python 规则公式和完整评价组装已退出生产文件，测试参考独立保留；公开输入/输出及旧数值标准入口未改变。生产没有 Python 规则评价回退，没有新增算法、线程或配置。未变链的测试计数改为读取原生内核的实际扫描数，不再通过已不存在的 Python 链循环计数。

### 完整同标准对照

参考使用 7.1 精确导出（生产源码等同 a03fdf5），当前使用 7.4 生产源码。全部使用同一冻结请求、种子 590531、400000 次额度、9999 秒搜索＋10 秒收尾。新版同进程运行两次，第一份包含首次编译成本，第二份复用已编译内核；不是两种求解模式或修改预算。

| 项目 | 改造前 | 新版首次运行 | 新版同进程第二次 |
|---|---:|---:|---:|
| 完整经过时间 | 233.390746 秒 | 154.426290 秒 | 138.965682 秒 |
| 进程 CPU 时间 | 232.287252 秒 | 154.094291 秒 | 138.558913 秒 |
| 候选额度消费 | 400000 | 400000 | 400000 |
| 完整候选评价 | 78775 | 78775 | 78775 |
| 接受次数 | 486 | 486 | 486 |
| 同期间拆单/未来借入归还 | 1 / 1 | 1 / 1 | 1 / 1 |
| 禁止/欠重/链数/虚拟重量 | 0 / 0 / 22 / 360 吨 | 完全一致 | 完全一致 |
| 核心/应用审计 | 均通过 | 均通过 | 均通过 |

九级质量保持 `(0,0,0,0,541.2236111111111111111111111,1181514.034911111111111111111,11606,360,22)`。结果指纹 `f1f7678e13998a8945a5c887aa91388e6276a0fbcc1e21989e5666a8b881072a`、确定性运行指纹和轨迹指纹逐项一致。比较工具核对完整公开结果（仅移除实测阶段耗时字段），包括所有有序订单、资源和审计；准备请求和交期报告还做字节比较，两次均通过，而不是只比最终九个分数。

原始目录：

- `diagnostics/numpy_numeric_search_and_calculation/stage7_reference_400k_01/run_01/`；
- `diagnostics/numpy_numeric_search_and_calculation/stage7_native_400k_01/run_01/`、`run_02/`；
- `diagnostics/numpy_numeric_search_and_calculation/stage7_complete_comparison_01/comparison.json`。

实际命令（均使用同一 Conda Python，设置 `PYTHONDONTWRITEBYTECODE=1`）：

```bash
python tools/profile_numeric_solver.py --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --output-dir diagnostics/numpy_numeric_search_and_calculation/stage7_native_400k_01 --candidate-check-limit 400000 --repeat 2
python tools/verify_numeric_kernel_migration.py --reference-run diagnostics/numpy_numeric_search_and_calculation/stage7_reference_400k_01/run_01 --compare-run diagnostics/numpy_numeric_search_and_calculation/stage7_native_400k_01/run_01 --compare-run diagnostics/numpy_numeric_search_and_calculation/stage7_native_400k_01/run_02 --output-dir diagnostics/numpy_numeric_search_and_calculation/stage7_complete_comparison_01
python -m pytest -q -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service
```

### 完成边界

- 内核、串行接线和同标准结果一致性已验证；专项另验证普通生产拒绝候选的四类明细构造器为零调用、整数越界/缺值/参数分支和缓存边界。
- 初次全仓 4237 项通过；之后补充完整内核架构白名单及参数边界测试，最终共享树累计 **4239 项通过，99.72 秒，退出 0**。精确暂存树同范围结果、树身份和残留检查见本项提交正文（不自引用提交 SHA）。
- 本次只有一组旧版及两份新版样本，不宣称 20 对性能门通过。完整经过时间首次减少约 33.83%，同进程第二次减少约 40.46%；首次峰值常驻内存从旧版 196952064 字节增加到 633864192 字节。编译与原生运行有内存成本，后续并行不能假设免费。
- **一分钟目标仍未达到。**阶段 7.4 后停止；阶段 8 的扁平批次、候选并行及收益验收均未实施。Windows exe 未在本机验证。正式 YAML、SQLite、外部仓库及四份既有用户改动保持原状。
