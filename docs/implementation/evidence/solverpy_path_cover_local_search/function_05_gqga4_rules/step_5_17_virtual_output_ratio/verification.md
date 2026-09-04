# 功能 5.17 虚拟材料产出比例验证

- 实施前 `81dc292f8e7aab3bc8f8aaada1e842a3cb4eb689`；Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 七文件：具体规则、唯一注册、新测试、本目录差分/记录、计划及 AGENTS。设计、原始基线、其他规则及门槛不改。
- 复用已有只读资源视图和精确重量求和，不派生台账；最终总重作分母。启用规则固定 28 位半偶 Decimal 除法/阈值/严重度，超限一条方案禁止，只有原始比例指标，无新增质量级。详细语义见计划第 8.6.12 节。

| 检查 | 共享树，退出 0 | 干净导出 |
|---|---|---|
| 专用测试 | 39 项，0.09 秒 | 随聚焦执行 |
| 聚焦 | 258 项，0.30 秒 | 258 项，0.30 秒 |
| 规则累计 | 894 项，0.89 秒 | 894 项，0.97 秒 |
| 仓内累计 | 1133 项，1.17 秒 | 1133 项，1.27 秒 |
| 参考对照 | 实际方案入口 57 次，1.862 秒 | 同样分组，1.953 秒 |

初验导出 `/tmp/apsgo-ratio-aQeFsX`，树 `0b4a90cc01de32b59bf228e998e2430dca17b030`；同集合、静态、残留保护及编译全部退出 0。补齐记录后最终暂存树再运行同集合，通过才提交。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_virtual_output_ratio.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_17_virtual_output_ratio/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
```

57 次包含 44 正常等价、8 停用指标省略、4 精确汇总差异及 1 参考空计划；另有 168 次目标数值上下文检查、目标零视图及停用不读视图各 1 例。正常案例包含真实过渡材、多个链及有来源记录的未来拆片归还；停用后参考保留显示统计、目标省略。空视图单测明确不代表视图与锚定方案一致，派生及覆盖守恒仍在功能 7/18。

精确汇总四例均有原始比例差异，一例有临界判定差异：真实重量 `1 + 4e-28`、虚拟 `1`，参考先行汇总的原比例重算为 `0.5`，目标为 `0.4999999999999999999999999999`；上限 `0.4999989999999999999999999999` 时参考违规、目标不违规。这是已确认保留权威重量精度的差异。参考报告直接提供的是六位显示值，并不暴露原始比例，脚本清楚区分实际入口和单独重建的参考运算。未解释差异为空。

独立复核聚焦 258 项（0.28 秒）、仓内累计 1133 项（1.12 秒）及差分通过；没有失败测试。一次文档补丁上下文未匹配，原子拒绝后按实际行重试，未改动无关文件。参考 SHA `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318` 与冻结输入前后不变。共享 Ruff 检查/格式检查通过（48 文件）；最终精确暂存后执行同集合、残留保护及导出编译，再核对提交树。

不运行完整搜索、性能或 V3 核验，不宣称 GQGA4 验收。提交后继续 5.18；必要回退仅反向恢复本项文件，不动其他工作区内容。
