# A1：链就绪阈值辅助与功能测试

## 实现范围

2026-09-17，按用户最新要求直接编写代码并测试新增功能，不再要求 A0 原构造复跑或完整依赖环境验收。联合测试、全局测试由用户本地完成，历史未执行项不改成通过。

基于提交 `d263f3927757e486795fc0cf2c731516589affe3`，新增 `src/apsgo_scheduler/core/_numeric_readiness.py` 的 `chain_readiness_step()`。它计算：

```text
release_offset_ms = max(原单最早开始偏移 - 本节点前累计工时)
duration_ms = 链内全部节点工时之和
可在 T 开始连续生产：T >= release_offset_ms
```

真实节点按来源索引取下界；生成虚拟节点只计时。复用 `_numeric_kernel.py` 的 `column_value()`、`column_size()`、`_add()`、`_sub()` 及现有状态处理，不另写规则或评分公式。允许负阈值和合法的 0 毫秒拆片工时；空链、全虚拟链、缺下界、错误索引、负工时和溢出不会变成有效摘要。

接口接收任务数值列、链的节点行号切片、4 项整数游标和既有 5 项状态数组。默认每步处理至多 256 个节点，返回 `(code, release_offset_ms, duration_ms, complete)`；`MORE_WORK` 时由调用方检查共享预算再继续。所有未完成或错误返回的 `complete` 都为假、输出数值为零，不能拿游标中的部分累计量作排程决策。调用者负责同一不可变任务/链视图的绑定，不在这里解析日期或修改输入。

本轮尚未修改初始构造入口，不实现整链调序、分段、借单、等待或回写变更；生产链序的接入属于 A2。

## 新增功能测试

测试文件：`tests/core/test_numeric_chain_readiness.py`。

实际结果：**43 passed，退出 0，pytest 报告 6.42 秒**。覆盖设计两链案例、内部阻塞、到位就绪、源索引与节点索引不同、重复来源、虚拟工时、私有扩展列、0 毫秒片段、负阈值、精确相等/提前 1 毫秒、缺失下界、整数溢出、分步恢复、停止状态和输入不被改写；另以固定种子生成小链对照独立的逐节点整数时间相加。

本轮使用现有 Linux x86_64 / Python 3.13.5、NumPy 2.3.5、Numba 0.65.1、llvmlite 0.47.0、pytest 9.0.2。代码采用项目 Python >=3.10 可用语法和现有依赖接口，不修改环境文件、不增加依赖，也不声称已在项目 Python 3.10.18 环境复测。

实际执行采用容器临时功能测试入口 `PYTHONDONTWRITEBYTECODE=1 python run_function_tests.py`：加载提交的新增模块，并隔离加载从该基线读取的既有列访问、检查算术和状态原语；pytest 仅运行上述新测试文件。Numba 正常启用，测试断言存在 `nopython_signatures`，没有以 Python 模拟执行代替编译函数。临时装载入口不提交到仓库，不修改包初始化或生产导入链。

这是新增函数的功能测试，不是完整包导入、`_clock_view()` 联合验证、原构造基线测试、全局回归或真实排程验收；这些均未在本轮运行，不再作为继续开发的前置条件。

用户本地可直接按正常项目导入运行同一功能测试：

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q \
  tests/core/test_numeric_chain_readiness.py
```

## 后续

A1 实现与新增功能测试已完成；A2 将在原初始构造中调用辅助、进行同期间整链就绪调序并完整比较质量。无需先补齐旧的环境或联合测试门槛。提交只关联 Issue #1，使用 `[skip ci]` 避免触发已有全量 push 测试，不修改 CI 配置，不关闭 Issue。
