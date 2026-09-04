# 功能 5.4：链重范围与目标指标规则验证

- 实施前提交：`de07c90`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin arm64、zsh、Conda `apsgo_v6_3.10.18`，Python 3.10.18。
- 依据：设计 v0.4 第 11.8、12.1～12.2 节，实施计划第 8.6 节，冻结链重规则和参考 `evaluate_chain()`。
- 参考 SHA-256：`87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。
- 精确范围：具体规则、加载器唯一注册项、`test_chain_weight_range.py`、本目录两份证据、实施计划和 `AGENTS.md`，共七份文件。

## 实现

`ChainWeightRangeRule` 直接继承 `Rule`，使用既有 `Chain.total_weight`，包括普通真实材料、实际过渡材料、生成型虚拟材料和各拆单片段的自身重量。参数显式提供：有限 Decimal 的 `min_weight >= 0`、`max_weight > 0`、`min_weight <= max_weight`、`target_weight >= 0`。设计没有要求目标必须落在上下限内，因此不增加这一限制。

- 欠重：`minimum - total > 0.000001`，原因 `chain_weight_below_minimum`，处置 `ALLOWED_FINAL_DEVIATION`，严重度保留完整差值。
- 超重：`total - maximum > 0.000001`，原因 `chain_weight_above_maximum`，处置 `PROHIBITED`，严重度保留完整差值。
- 违规主体使用调用方提供的链主体；按原因区分处置，不能放行整个链重规则。
- 停用时不产生任何违规或指标，不要求业务参数；错误主体仍拒绝。重量完整性已由 Node 保证，无新增原始必需字段。

指标顺序固定为：`underweight_chain_count`、`underweight_total_gap`、`overweight_chain_count`、`overweight_total_excess`、`chain_target_weight_deviation`。数量为整数，其余为精确 Decimal；缺口/超量只在真正越过容差时计入，否则零。目标偏差为总重与目标的绝对差，只提供指标，不增加违规或默认质量等级。

连续重量与链重现共用同文件 `_positive_weight_difference()`，沿用已验证的独立 Decimal Context，目标差值通过较大值减较小值计算，避免外部低精度环境对减法或 abs 再次舍入。不新增框架、依赖或其他规则。

## 与参考的边界及已确认后续变更

参考 1155～1158 行的摘要即使在容差内也可能保留正缺口，但全局评价 1392～1396 行只累计真正欠重链。新 `underweight_total_gap` 是后续评分用的精确原始贡献，不冒称摘要字段逐字复现。

参考 `decimal_text()` 先将每条欠重链缺口量化到六位，再以 Decimal 求和、转 float 并 round 六位。探针证明：两条各欠 `0.00000149` 的链得到 `0.000002`，先求和再舍入则是 `0.000003`。用户本轮明确要求**改为逐条缺口保留两位小数再汇总**；原始重量、容差与违规严重度保持精确。该变化是用户确认的评分差异，不是参考等价。

本提交只完成链重规则及精确原始贡献，不实现方案评分或提前量化；收口后另作设计/实施说明修订，明确两位小数的命名投影、舍入顺序及功能 5.19/7 接线。参考探针中的六位断言仍是原始参考事实，不作为新两位评分的期望值。

参考停用链重后仍产生欠重质量项，新规则停用贡献为空，符合既定启停修正。参考 `target_weight` 仅加载而未使用；目标偏差是设计要求的新指标，单独验证，不宣称来自参考评价。

## 验证

| 检查 | 共享工作区 | 暂存代码树干净导出 |
|---|---|---|
| 专用金样 | 51 项，0.08 秒 | 包含在聚焦集合 |
| 聚焦回归 | 243 项，0.29 秒 | 243 项，0.24 秒 |
| 规则累计 | 398 项，0.39 秒 | 398 项，0.37 秒 |
| 全量累计 | 657 项，0.86 秒 | 657 项，0.86 秒 |
| 链重参考差分 | 618 案例一致，首个差异为空 | 同样通过 |
| 差值提取后的既有回归 | 窄钢 5613、同规格 11900 案例均一致 | 同样通过 |
| 静态/格式/残留 | 通过 | 残留通过 |
| 编译 | 未运行 | 通过 |

以上均退出 0。代码树 `1caf07205000b14eb9935267a76e92491f3a2717`，导出 `/tmp/apsgo-chain-weight-PAD0Hi`；最终文档补齐后再导出最终暂存树执行同一检查后提交。独立只读复核通过，另以有理数期望在 Decimal 精度 1 下核验 240 个配置/临界值案例一致。

618 个案例覆盖六组上下限/目标、六种材料组合及容差两侧。违规投影为参考浮点严重度对比，另以精确 Decimal 检查全部原始差值；不把精确贡献错误地与已经量化的参考摘要直接比较。新目标指标和停用修正单独断言，不混入“参考完全一致”计数。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_chain_weight_range.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
for task_probe in step_5_02_continuous_narrow_steel_weight step_5_03_same_spec_continuous_real_weight step_5_04_chain_weight_range; do
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/"$task_probe"/reference_differential.py /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
done
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# 仅在干净导出运行。
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules
```

稳定残留、冻结输入与质量/性能门槛未改，缓存不提交。本项不包含 GQGA4 排程、完整方案评分、结果发布或性能验收。
