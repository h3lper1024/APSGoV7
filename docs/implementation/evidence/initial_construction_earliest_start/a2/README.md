# A2：同期间整链就绪调序与功能测试

## 范围与实现

2026-09-17，按用户“继续下一步”执行 A2。开发基线为 `e15f11412e72327819e7c49bd266292a2c6e9405`，继续原分支 `codex/earliest-process-start-constraint`。联合、全局及真实数据验证由用户本地完成，不再作为开发前置条件。

新增 [`_numeric_initial_layout.py`](../../../../../src/apsgo_scheduler/core/_numeric_initial_layout.py)，并在 `construct_numeric_initial_plan()` 完成基准评价后调用 `select_ready_initial_plan()`。该分支只在实际启用 `NumericRuleKind.EARLIEST_START` 时执行，最终初始指纹绑定选用方案与对应评价。

主要行为：

- 复用 A1 `chain_readiness_step()` 计算每条链的就绪阈值和实际工时；摘要、选择扫描均有分步预算检查。
- 只在当前期间选择原顺位最靠前的就绪整链；每选完一条重新判断，不预先按阈值一次排序，不跨期抢排。跨期间不重置时钟。
- 当前期间没有就绪链时取原顺位第一条，保留实际提前违规；不等待、改日期或增加虚拟材。
- 重排私有链元数据后才形成提案，核对链身份、链内行号、期间与代次不变；原方案数组保持只读且不被改写。
- 调用既有 `evaluate_numeric_plan()` 完整评价。按同一编译规则身份保护其他规则的禁止数量，原质量键严格改善才采用；相同、更差或保护不通过时返回原基准对象。
- 构造不扣搜索候选额度，不增加已接受搜索动作/拆单/虚拟序列。取消与超时不返回部分初始方案；数值或评价异常不伪装成质量回退。

本次不切链、不拆订单，不修改 DAG、匹配、后续借单/邻域、九项评分公式、权重口径、生产时钟或回写政策；不修改配置、规则库、依赖、CI、前端、用户服务或既有测试断言。

## 日志

`numeric_initial_readiness_summary` 包含 `base`、`proposal`、`selected` 的质量和提前节点/去重来源/累计毫秒，以及 `reason`、提案/选用链序、提议和实际调序数量、阻塞次数、扫描次数和耗时。

选择原因区分 `strict_improvement`、`order_unchanged`、`quality_equal`、`quality_worse`、`other_rule_increased` 和 `interrupted`。拒绝提案时实际调序数为 0；中断时 `selected` 为空。每个阻塞链记录期间、链开始偏移、链阈值，以及完整评价报告的首个提前节点、来源、实际开始和最早开始偏移。日志为本地诊断，不自动上传业务订单，不改变公共响应字段。

## 新增功能测试

测试文件：[`tests/core/test_numeric_initial_earliest_start.py`](../../../../../tests/core/test_numeric_initial_earliest_start.py)。

**最终结果：56 passed，退出 0，pytest 报告 5.00 秒。**测试验证了动态原顺位选择、当前期间限制、跨期连续时间、全未就绪退路、零工时、相等/提前一毫秒、分步恢复、总时间溢出、停止、严格质量比较、逐规则保护、基准对象保留、只读输入、身份与诊断。固定种子另外生成 100 组小例对照独立简单选链算法，计入相应测试，不把小例数量计作额外 pytest 项数。

实际执行使用现有 Linux x86_64 / Python 3.13.5、NumPy 2.3.5、Numba 0.65.1、llvmlite 0.47.0、pytest 9.0.2，不增加或更改项目依赖。代码使用项目 Python >=3.10 可用语法与现有依赖接口，没有宣称已在项目 Python 3.10.18 上复测。

命令为临时隔离入口 `PYTHONDONTWRITEBYTECODE=1 python /mnt/data/apsgo-a2/run_function_tests.py`，只加载新增模块、A1 与从固定源码读取的列访问/检查算术/链操作函数，pytest 只运行上述新测试文件。Numba 正常编译，测试检查 `nopython_signatures`。任务与正式方案构建、完整评价器使用测试文件中的显式替身，以测试新增控制逻辑；没有执行真实生产规则评价器、完整包导入或完整构造调用链，不能将这 56 项称为联合测试或求解质量验收。临时隔离加载器不提交到生产仓库。

首轮有 5 个新测试的日志断言使用了错误的 `LogRecord.args` 访问方式；修正为字典读取后通过。没有为通过测试改变既有业务断言或生产回写政策。

用户本地可按正常项目导入运行同一功能测试：

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q \
  tests/core/test_numeric_initial_earliest_start.py
```

## 交付状态

A2 代码接入及新增功能测试完成；完整依赖环境中的构造/公共入口联合验证及真实对照由用户执行。本次仅修改初始构造的接入点、新模块、新功能测试、实施计划和本记录。提交使用 `[skip ci]`，仅关联 `Refs #1`；不修改现有工作流、不关闭 Issue，不承诺真实违规减少或性能提升。
