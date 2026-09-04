# 前置修复：支持规则参数列表与分组配置

- 实施前提交：`6fb7a2b520b3b3fde7559b3ae2626e399dbf6a92`。
- 分支：`codex/solverpy-path-cover-clean`；环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`、Python 3.10.18。
- 依据：设计 v0.4 第 11.2、11.3、11.6、28.3、32.2 节及实施计划第 8.5.1 节；本修复独立于功能 5.1。

## 范围与实现

精确修改三份生产文件：`src/apsgo_scheduler/core/contracts.py`、`src/apsgo_scheduler/api/request.py`、`src/apsgo_scheduler/core/rules/base.py`；三份测试：`tests/api/test_request_contracts.py`、`tests/core/rules/test_rule_base.py`、`tests/app/test_rule_set_loader.py`。连同本证据、实施计划和 `AGENTS.md`，提交白名单为九份文件。

新增唯一 `RuleParameterValue` 与专用 `freeze_rule_parameters()`，请求规格和规则基类共用。单值保持类型；有序列表/元组递归复制为元组，字符串键映射递归复制为只读映射。保留顺序、重复项与合法空值，不补业务默认值；非法键、浮点数、非有限十进制数、无序集合、自定义值对象及循环引用在构造边界被拒绝。环检测只记录当前递归路径，多个字段共享同一个无环容器仍可冻结。

原 `freeze_scalars()`、`canonical_json()` 与 `fingerprint()` 未修改。全部节点/订单/原型扩展属性、计数和耗时映射继续使用原标量边界。加载器生产代码未改；所有配置包含停用定义都先过通用形状校验，停用未知规则仍不查注册表、不校验具体业务必需参数、不影响规则执行。

未修改具体规则公式、求解流程、五份原始输入、正式质量/性能门槛、工具、工程配置或权威设计；未执行 GQGA4 排程。

## 修复前固定金样

金样从未修改的 `6fb7a2b` 提取，预期编码和哈希作为字面值保留在测试，不通过新函数动态生成预期：

| 样本 | 修复前固定 SHA-256 | 提取证据 |
|---|---|---|
| 参数 `{text: FC, count: 5, limit: Decimal(0.60), enabled: True, unset: None}` | `d9b12deea111cffe9159054b41740a21eb203dfbbe39ab4be7eb6d509a33d6fd` | 改动前直接运行原 `freeze_scalars()`、`canonical_json()`、`fingerprint()`，退出 0 |
| API 测试默认请求 | `62f9672badd9984b46106043d664e4f954e2b538b1fff214422e0184d6c4ceff` | `git archive 6fb7a2b` 导出 `/tmp/apsgo-parameter-api-baseline-bi7LZC`，调用原测试 `request()` 与 `fingerprint_public_request()`，退出 0 |
| API 测试混合扁平参数请求 | `683e4b73f8b5f9498cef088ae879105ea83af6f5df9fbedba3133d2c29dcf1a6` | 同一导出，参数与规范编码字面值见 `test_flat_parameter_encoding_and_request_fingerprints_match_pre_fix_goldens()` |
| 加载器测试默认完整规则集（不含指纹字段自身） | `c9c1632b8f81e70e3c6485511ebd4df949b3c28cff574bafea3e85a25a9f8720` | `git archive 6fb7a2b` 导出 `/private/tmp/apsgo-rule-parameter-golden-zphNV9`，调用原测试 `spec()`、规范编码和 `fingerprint_rule_set_spec()`，退出 0 |

这些金样在共享树和干净导出全部一致；列表/元组等值归一、映射顺序无关、嵌套叶值或序列顺序变化改变身份、布尔值不等同于整数也有独立测试。修复前与修复后的首个预期行为差异仅是合法结构化规则参数从构造拒绝变为接受；无旧扁平编码或业务行为差异。

## 实测验证

| 检查 | 共享工作区 | 暂存代码树干净导出 |
|---|---|---|
| 三份聚焦测试 | 223 项通过，0.22 秒 | 223 项通过，0.22 秒 |
| 架构、API、应用、核心、参考身份累计 | 451 项通过，0.71 秒 | 451 项通过，0.67 秒 |
| Ruff 静态及格式 | 通过，32 个文件 | 通过，32 个文件 |
| 旧残留保护 | 通过 | 通过 |
| 编译及 wheel 构建 | 按约定不在共享树执行 | 通过 |
| wheel 独立导入及列表参数请求构造 | 使用干净导出 wheel 与 `python -I` | 通过 |

上述命令均退出 0。暂存代码树为 `7862c0d38152dfc02220b33b55ca74b23f8c0712`，导出路径 `/tmp/apsgo-rule-parameter-fix-kAhoQi`；wheel SHA-256 为 `7d4d22a05781b1d24ec8def3b3dfd114633cce5c291f67e8860253e11a86ebd5`。补齐本记录与进度文档后，提交前再次导出最终暂存树执行同一聚焦和累计回归。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/api/test_request_contracts.py tests/core/rules/test_rule_base.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --check
git diff --exit-code HEAD -- tests/baselines tools pyproject.toml src/apsgo_scheduler/app/rule_set_loader.py
# 以下仅在干净导出目录执行。
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
conda run -n apsgo_v6_3.10.18 python -m pip wheel --no-build-isolation --no-deps --wheel-dir dist .
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -I -c 'import sys; sys.path.insert(0, "/tmp/apsgo-rule-parameter-fix-kAhoQi/dist/apsgo_scheduler-0.1.0.dev0-py3-none-any.whl"); import apsgo_scheduler.core.contracts as contracts; from apsgo_scheduler.api.request import RuleDefinitionSpec; from apsgo_scheduler.core.rules.base import Rule; assert ".whl/" in contracts.__file__; value = RuleDefinitionSpec("id", "ExampleRule", "name", contracts.RuleScope.CHAIN, True, "v1", {"grades": ["FC", "FD"]}); assert value.parameters["grades"] == ("FC", "FD"); print("wheel imports and nested request parameters passed")'
```

独立只读复核聚焦 223 项通过（0.23 秒），没有阻断缺陷。额外探测确认只读代理背后的可变字典、元组内的可变子容器均与冻结结果脱离。加载器新增用例在请求仍使用旧冻结函数时曾有 9 项失败，双入口接线后全部通过，未修改预期来绕过失败。

16 个稳定旧残留与 992 个原始易变路径保持；仅保留实施开始前已记录的三个 Ruff 缓存增项，不删除、不暂存。下一项为功能 5.1“高表面连续数量规则”；本项 451 项回归不是具体业务规则或 GQGA4 求解质量验收。
