# 功能 5.2：窄钢连续真实重量规则验证

- 实施前提交：`bba9d47`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin arm64、zsh、Conda `apsgo_v6_3.10.18`，Python 3.10.18。
- 依据：设计 v0.4 第 11.8、12.2 节，实施计划第 8.6 节；指定 `solver.py` 的 858～908、1111～1128 行及冻结规则 JSON 的窄钢记录。
- 参考脚本 SHA-256：`87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。
- 精确范围：具体规则文件、加载器唯一注册项、`test_continuous_narrow_steel_weight.py`、本目录两份证据文件、实施计划和 `AGENTS.md`，共七份文件。

## 实现语义

`ContinuousNarrowSteelWeightRule` 直接继承 `Rule`，复用标准库连续分组和既有 `sum_weights()`；不新增中间规则层、数值框架或拆单授权路径。

| 项目 | 已实现口径 |
|---|---|
| 参数 | `grade_class` 为非空文本，配置原文不自动去空白；`width_upper_exclusive` 为有限正 Decimal；`max_real_weight` 为有限非负 Decimal；全部显式提供，无默认值 |
| 命中 | 仅普通真实节点；节点类别去首尾空白、大小写敏感比较；宽度非空且 `float(width) < float(width_upper_exclusive)`，该比较不加物理容差 |
| 打断 | 其他类别、宽度达到或超过上限、宽度为空、实际过渡和生成型虚拟材料；后两种不读取类别 |
| 连续重量 | 每个普通真实拆单片段累加自己的重量，不按来源去重；精确 Decimal 求和 |
| 超限 | 严格超过 `max_real_weight + Decimal("0.000001")` 才禁止；严重度为完整重量减上限，不再扣容差 |
| 主体与顺序 | 每个最大超限段一条违规，按链位置发出；主体 `<调用方链主体>:<规则身份>:<零基起点>-<零基终点>`；原因码 `if_narrow_run_weight` |
| 指标 | `max_if_narrow_real_run_weight` 为 Decimal，包含合法段；无命中为零。冻结表达式中的 `max_if_narrow_steel_real_run_weight` 在功能 5.19 映射为参考输出名，不能混淆 |
| 启停和职责 | 停用贡献、指标、必需字段为空；错误主体仍拒绝；本规则不产生任何拆单资格，授权留在功能 5.18 |

求和、上限加容差均复用 `sum_weights()`；严重度用标准库独立 `Context.subtract()`，按数值最高有效位到最低指数确定精度，避免调用方低精度环境截断权威重量。未提前转换重量为浮点数。

输入边界：本规则 `required_fields()` 仅声明 `grade_class`。参考输入预检在本规则启用时要求全部原始订单（含实际过渡材料）类别非空；宽度必需检查由逆宽规则启用触发。仅启用窄钢时，缺宽度在链内不命中，不能误称参考原行为是“窄钢强制宽度”。前置标准化仍在功能 6，本项只评价已有链。

## 验证结果

| 检查 | 共享工作区 | 暂存代码树干净导出 |
|---|---|---|
| 专用金样 | 56 项，0.07 秒 | 包含于聚焦集合 |
| 聚焦回归 | 248 项，0.23 秒 | 248 项，0.24 秒 |
| 规则累计 | 288 项，0.27 秒 | 288 项，0.27 秒 |
| 全量累计 | 547 项，0.73 秒 | 547 项，0.74 秒 |
| 参考差分 | 5613 个案例一致，首个差异为空 | 同样通过 |
| 静态、格式、残留 | 通过 | 残留通过 |
| 编译 | 未在共享树运行 | 通过，含本项证据脚本 |

以上命令均退出 0。代码树 `ab5e3f7a3248ceab08126503847bacded0d88e1e`，导出 `/tmp/apsgo-narrow-weight-B3aVlX`。补齐文档后再导出最终暂存树，执行同一聚焦、规则累计、全量累计和差分检查，保证提交与测试对象一致。

差分枚举七种角色/类别/宽度/重量组合的长度 1～4 序列，上限取 0 和 500，共 5592 个有效案例；另有 21 个重量容差、浮点宽度投影、自定义上限和文本边界案例。按核心契约跳过 8 个全虚拟链；逐项比较主体、严重度、最大段重量，兼查原因码和处置。56 项便携金样另锁住外部 Decimal 精度为 2 时的完整重量、允许边界及差值。独立只读复核未发现阻断问题。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_continuous_narrow_steel_weight.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_02_continuous_narrow_steel_weight/reference_differential.py /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_02_continuous_narrow_steel_weight/reference_differential.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_02_continuous_narrow_steel_weight/reference_differential.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# 仅在干净导出运行。
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_02_continuous_narrow_steel_weight/reference_differential.py
```

旧稳定残留保持，三个既有 Ruff 缓存增项未删、未提交；冻结输入、质量与性能门槛、目标设计均未改。本项没有 GQGA4 排程、完整规则集或全流程性能结论。下一项为功能 5.3“同规格连续真实重量规则”。
