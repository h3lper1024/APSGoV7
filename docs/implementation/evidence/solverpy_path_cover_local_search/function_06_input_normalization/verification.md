# 功能 6 输入标准化、身份校验与问题指纹

- 开始实施前 `29957820e5cbf9eb6fb716d0eb3706587610f641`；期间表面等级可缺省经用户确认后独立提交 `652edb45da6d5f9264df25318d2dfecc365cd587`，为本功能的最终父提交。
- 平台 Darwin 27.0.0 arm64，zsh，Conda `apsgo_v6_3.10.18` / Python 3.10.18。
- 设计 v0.12 SHA256 `beba3592df2befd2c17cec2864c6a9ce8d663dc0e3a3ed6c04f26eddb58af565`，本项不修改设计、原始输入、规则/策略样本或正式门槛。
- 六文件：一个应用层入口、两份新测试、本证据、实施计划及 AGENTS；不创建新的指纹模块、生产原始数据适配器、缓存或搜索。

## 已实现边界

复用既有不可变请求、`validate_request()`、`load_rule_set()`、领域构造及 `fingerprint()`。订单/来源/期/原型身份去空白与参考 `id_value()` 对齐；产线/工序/场景保持公开契约的精确匹配。原型和节点不排序，计划期仅按显式序号排序，不固定期数。

所有安全可定位错误汇总为原有诊断类型，按阶段稳定排序；每个安全节点只创建一次，即使其他订单出错仍检查其优先级类型，不返回部分问题。规则声明来自重新核验的完整配置；已加载对象及实际作用域内容也需匹配，伪造同指纹不能绕过。未知配置不用于推导字段要求。

表面等级可缺省，非空文本规范化；其他启用真实必填字段保持，原型不承担真实专有字段或非空温度。非空物理值需有限浮点投影，权威值仍为 Decimal；重量不无故投影。原子重量边界使用精确上限加允许误差，低 Decimal 精度及 Inexact 陷阱不改变结果。通用入口保留显式材料角色，不硬编码三条件分类。

问题指纹包含全部规范化业务输入；映射键序不影响，节点/原型顺序及期序影响。请求编号、规则和策略身份独立，不混入问题指纹。

## 共享树执行

| 检查 | 结果 |
|---|---|
| 输入及问题指纹专项 | 82 + 29 = 111 项通过，0.37 秒 |
| 加公开请求/加载器/领域的聚焦 | 301 项通过，0.57 秒 |
| 完整仓内累计 | 1378 项通过，1.66 秒 |
| Ruff / 格式 | 52 文件通过 |
| 残留保护 | 通过；根 IDE 按用户确认政策忽略内容，未跟踪、未提交 |

以上退出码均 0，累计不再排除任何功能 6 测试。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/app/test_input_normalizer.py tests/core/test_problem_fingerprint.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/app/test_input_normalizer.py tests/core/test_problem_fingerprint.py tests/api/test_request_contracts.py tests/app/test_rule_set_loader.py tests/core/test_model_contracts.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

## GQGA4 仓内数据实测

测试专用值标签解码器从冻结问题提取请求，不加载参考 Python。531 个订单、27 个原型，原始真实重量 `29333.91`，实际过渡材 61 个；逐源 CSV 重量、来源期及三条件分类一致，首末节点/原型与原始序列一致，未产生任何拆片或虚拟节点。所有请求内容在规范化前后保持不变。

实际输入指纹 `cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea`。共享树一次规范化耗时 `0.029170915979193524` 秒（仅调用本入口，不含原始文件读取）；外层探针 1.508 秒、退出 0。这是输入准备实证，不是 180 秒完整求解性能验收，也不证明链数/规则终态门槛。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import json,runpy,sys,time; sys.path.insert(0,"src"); values=runpy.run_path("tests/app/test_input_normalizer.py"); spec=values["gqga4_spec"].__wrapped__(); request=values["gqga4_request"].__wrapped__(spec); started=time.perf_counter(); problem=values["normalize"](request); elapsed=time.perf_counter()-started; values["test_frozen_gqga4_531_orders_and_27_prototypes_normalize_without_search"](request); print(json.dumps({"orders":len(problem.nodes),"prototypes":len(problem.virtual_prototypes),"actual_transition_orders":sum(n.material_role.value=="actual_transition" for n in problem.nodes),"weight":str(values["sum_weights"](n.weight for n in problem.nodes)),"input_fingerprint":problem.input_fingerprint,"normalization_seconds":elapsed,"search_executed":False}))'
```

## 开发期复核与差异

复核曾发现合成整数优先级可错误接纳布尔值：测试也一度把 False 写作合法例。改为复用公开优先级入口做类型预检；首轮修正后 135 通过/1 失败为该旧测试预期，更新后通过。随后发现其他节点基础错误会提前阻断安全节点的优先级错误，已改为逐节点汇总并补四个混合回归；空白必需属性只报缺失，不重复报类型错误。只读复核独立重放通过，没有放松整数契约或新增类型框架。

表面可空是独立用户确认修订，不与参考原先必填输入宣称等价；GQGA4 三条件在测试原始映射及 CSV 中互证，核心仍只消费公开角色。测试写入期间一次格式上下文导致 apply_patch 未命中，没有部分写入；已按实际格式重做。

## 干净导出与提交

初验树 `c94a491e644b8c84e5e02965fd62fc1cf04ff312`，导出 `/tmp/apsgo-normalize-initial-XAP4Bk`。按上述相同命令执行：专项 111 项（0.38 秒）、聚焦 301 项（0.58 秒）、完整仓内 1378 项（1.70 秒）通过；静态/格式 52 文件与残留保护通过。仅在导出执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools`，退出 0。

GQGA4 同一探针在导出复跑，531/27/61、29333.91 及完整输入指纹均与共享树一致；规范化约 0.031360417 秒，外层 2.073 秒，退出 0。以上没有失败项。

补齐本记录后重新导出最终暂存树，按相同集合复测并核对提交树身份后提交。范围只有列出的六文件；下一项功能 7，当前未运行主搜索或完整验收。
