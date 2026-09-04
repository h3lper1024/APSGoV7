# 功能 22.1：相邻链首尾宽差规则与第七级评价验证

- 日期：2026-09-04；平台：macOS / Darwin 27.0.0 arm64 / zsh。
- 实施前：`a7b0d107c67cd7cbeac187fbf9b794a3de80717b`。
- 依据：详细设计 v0.15 第 12.2.2 节，实施计划 v0.57 第 8.23.6 节。
- 范围：三份生产文件、一份新测试、实施计划、AGENTS 及本文，共七文件。正式配置、旧结果、质量/性能门槛及原始参考均不改。

## 实现与边界

新增 `InterChainWidthGapRule`，只读当前链序，逐对计算实际尾/首节点宽度的精确绝对差，含跨大辊期和虚拟端点，不首尾闭环。每条链所属期必须已按输入期序排列；本规则拒绝乱序，不代替下一项的生产链序分组。

有序边界由规则的 `boundary_contributions()` 按需返回前后链身份和已有 `MetricContribution`，`evaluate()` 复用同一结果；单链无边界但显式输出零指标。复用原完整评价器及质量声明，不改共同贡献类型或指纹编码，不生成第二份可变台账。

输入标准化按全部已核验启用规则取得虚拟原型宽度/厚度必需声明，覆盖只有方案级目标而没有连接规则的配置；真实材料和原型都不能漏宽度。温度和来源属性边界不扩展；停用不增加必需字段。正式 GQGA4 仍为原六级配置，七级能力只使用合成规则集验证。

## 实际验证

工作区和最终干净导出均使用 Conda `apsgo_v6_3.10.18`，不写共享字节码或 pytest 缓存。

```bash
# 专项
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_inter_chain_width_gap.py -q
# 聚焦
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py tests/app/test_input_normalizer.py tests/core/test_plan_evaluation.py -q
# 仓内累计
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
```

| 范围 | 工作区 | 最终干净导出 |
|---|---|---|
| 专项 | 78 通过，0.23 秒，退出 0 | 最终验证后记入提交正文 |
| 聚焦 | 1180 通过，1.34 秒，退出 0 | 最终验证后记入提交正文 |
| 仓内累计 | 2646 通过，169.02 秒，退出 0 | 最终验证后记入提交正文 |

静态和保护检查命令：

```bash
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --check
# 仅在最终干净导出执行
conda run --no-capture-output -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```

最终暂存树通过 `git write-tree` 固定，使用 `git archive` 导出到新临时目录并执行上面同一集合；树身份、导出路径、实际耗时和退出码记录在本次提交正文，避免文档自引用当前提交。提交前再次检查精确七文件白名单和残留保护。

## 独立核验与差异

- 手算小样本三链：1200→1500→1420，两处贡献为 300、80，总值 380 mm；包含跨期边界，外部 Decimal 精度设为 2 仍正确。
- 另一次独立检查覆盖极小差值 `6E-27`、完整评价聚合、非法/未知期序、单链零、停用不读取无效上下文，以及 PLAN-only 配置的真实/原型缺宽度诊断。
- 专项覆盖四种端点角色的左右组合、单链及多链外端点、精确大数、无闭环、期序非字典序及空期；前六级逐级优先、七级严格改善接受及全部同分拒绝均经真实评价或完整候选入口验证。仅借用量减少仍不能突破七项同分。
- 没有发现未解释的差异或失败测试。新增指标是预期功能扩展；原六项在同一候选上逐项保持原评价值。新的原型必填诊断仅由新启用规则声明触发，不给旧配置添加隐藏规则。

本项不证明链序搜索收益，不代表新目标 GQGA4 质量或性能验收通过。下一项为 22.2 生产链序与整链移位；新正式配置接线在 22.3，真实复测及边界报告在 22.4。
