# 功能 2：建立核心值类型、不可变领域模型与诊断契约

- 实施前提交：`51caca3`；分支：`codex/solverpy-path-cover-clean`。
- 环境：macOS 27.0 arm64、zsh；Conda `apsgo_v6_3.10.18` / Python 3.10.18；现有 Ruff 0.12.0。
- 依据：实施计划第 8.3 节、设计第 10、11.2、13、22、23、26 节。

## 范围与实现选择

新增核心 `contracts.py`、`model.py`、`resource_facts.py`，API 只在 `diagnostics.py` 原样重导出三个核心诊断类型。新增三份计划内测试。没有新依赖，没有恢复旧代码或更改已冻结基线。

1. 节点、虚拟原型、谱系、链、方案和资源事实用标准库冻结数据类；输入序列复制为元组，属性复制后只读。`RuleScalar` 只允许设计列出的标量，嵌套容器和浮点权威数值被拒绝，因此无需通用递归冻结框架。
2. 真实材料与实际过渡材料有完整来源身份，生成虚拟材料只有虚拟谱系。实际过渡材料不得携带拆单谱系。拆单模式、来源期、原排产期、目标期和来源接受序号一并校验。
3. 资源事实仅为值载体；`fingerprint_payload()` 列出分区全部有序语义字段且排除自身指纹，不生成最终资源事实或哈希。真正派生与跨明细审计留给功能 18。
4. `SearchState` 按设计保留可变私有状态；`commit_accepted()` 先构造、校验完整替换值，再成组提交。拒绝候选直接丢弃，非法提交不改变正式状态。此方法不判断候选优劣，严格改善判断仍属于后续评价与总控。
5. 显式构造顺序身份命名为 `solverpy_stable_order_v1`，数值身份为设计已有的 `solverpy_float_epsilon_1e_9`。策略允许零候选预算、负随机种子、零到两个虚拟桥节点；没有产线专用隐藏默认值。
6. 虚拟原型保存第 13.1 节输入定义中的温度上下限等事实；第 20.3.5 节已固定温度生成算法，本项不新增可配置的温度算法。
7. 评价类属于功能 7。本项保留字段的延迟类型注解，不创建 `Any/object` 别名或第二套评价壳类；对应注解在功能 7 接线。本项不验证评价内容或执行 `get_type_hints()` 的跨阶段解析。核心释放目前仅是值与身份载体，不实现正式审计签发。

## 验证

共享树聚焦测试 88 项通过（0.08 秒），含策略与诊断 40 项、领域及资源事实 48 项；架构、API、核心与参考基线累计 154 项通过（0.39 秒）。Ruff 检查通过，18 个 Python 文件格式检查通过。上述命令退出码均为 0。

代码复核复现了默认 Decimal 上下文舍入可能把拆单超出 0.1 的差额抹掉的问题；现由 `sum_weights()` 按输入有效位、最小指数和进位空间选择局部精度，统一用于链重量派生和分区精确守恒，不改变调用方上下文。低精度上下文和大数差额反例已通过；不新增业务精度上限或重量限制。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_model_contracts.py tests/core/test_solver_policy.py tests/api/test_diagnostic_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/core tests/integration/test_reference_baseline_identity.py -q
ruff check --no-cache src tests tools
ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# 仅在暂存树干净导出运行
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```

工作区 16 个稳定残留内容和 992 个原始易变路径未变；新增 Ruff 缓存路径仅记录，不纳入提交。提交前只暂存本项 7 个源码/测试文件、本证据、实施计划和 `AGENTS.md`，共 10 个文件；再从暂存树导出运行相同检查，结果在提交前回读。

暂存代码树 `3f2883cdf6de8fc27ae8d7dbbdfe54f1b3512514` 的干净导出中，相同聚焦命令 88 项通过（0.08 秒），累计 154 项通过（0.38 秒）；Ruff、格式、残留保护和编译均通过，全部退出 0。之后只补充本说明和状态，代码与测试不变；最终暂存树再执行累计回归后提交。没有删除旧残留或变更参考文件；全部本项文件为 UTF-8 无 BOM。

## 未实施范围

没有规则公式、输入加载、评价算法、局部搜索、最终审计、结果签发或性能复测。154 项测试只验证当前契约与历史基线，不证明新求解器 GQGA4 门槛通过。

下一项：功能 3“建立公开请求、策略与前置失败结果契约”。
