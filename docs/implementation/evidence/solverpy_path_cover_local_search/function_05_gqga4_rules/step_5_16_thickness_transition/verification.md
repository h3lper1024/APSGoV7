# 功能 5.16 厚度跳跃验证

- 实施前：`9a92b919fc67d934b05cc92523ab11e040bcb764`；Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 仅具体规则、唯一注册、新测试、本目录差分/证据、计划与 AGENTS 七文件；原始基线、设计、其他规则和门槛不改。
- 复用现有两节点入口及冻结参数。区间原序首命中、精确开闭边界、较薄/较厚基准、绝对/相对模式、显式后备容差、无角色豁免；本规则无指标或新评分。具体语义见计划第 8.6.11 节，不引入新的区间框架。

| 检查 | 共享树，退出 0 | 干净导出 |
|---|---|---|
| 专用测试 | 63 项，0.10 秒 | 随聚焦执行 |
| 聚焦 | 282 项，0.33 秒 | 282 项，0.33 秒 |
| 规则累计 | 855 项，0.88 秒 | 855 项，0.87 秒 |
| 仓内累计 | 1094 项，1.21 秒 | 1094 项，1.16 秒 |
| 参考差分 | 2167 启用、18 停用一致；36 自定义后备容差；三类保护各 6；最终标签精简后主代理复测 1.641 秒 | 同样 2167/18/36/6/6/6，1.906 秒 |

初验导出 `/tmp/apsgo-thickness-Gl7KAp`，树 `8173cd79783f6d2a6082c7df72c960ac71ae17bc`，全部退出 0。独立复核聚焦 282 项（0.31 秒）、仓内累计 1094 项（1.13 秒）和专项参考对照通过，无阻塞问题。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_thickness_transition.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_16_thickness_transition/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
```

实际参考连接检查及厚度判定各调用 2239 次。36 个非 0.1 后备容差案例中 24 个结果与参考不同：首例 0.5→0.65、无命中区间，参考固定容差 0.1 的严重度为 1，目标显式容差 0 的严重度为 150000000。这是配置生效差异，GQGA4 配置仍取参考的 0.1；不向参考函数塞入它不识别的参数。三类数值保护首例分别为 1E1000 原始厚度转空、相对容差 1E308 乘基准 4 溢出、1→1E308 且容差 0 的严重度溢出；目标均定位拒绝。有限正厚度的差值不会溢出，不绕过领域构造伪造反例。

启用/停用、所有角色、缺值、区间边界、重叠顺序及浮点边界均有覆盖；未解释差异为空。源 SHA `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318` 和冻结 resolved/context 前后不变。直接缺厚度放行不是输入合格，真实字段完整性仍由功能 6 验证。

Ruff 对 `src tests tools` 加本目录差分双树检查/格式检查通过（47 文件）；首次静态检查遇到作者仍在格式化的新测试文件，等待完成后复验通过，未更改测试断言，无失败测试。共享架构专项 46 项通过（0.13 秒）。七文件精确暂存，保护文件不变、残留保护、干净导出同集合与编译均通过；补记证据后再次验证最终暂存树并核对提交树身份。

不运行完整输入、搜索、性能或 V3 核验，不宣称整体 GQGA4 验收。独立提交后继续 5.17；回退以实施前提交反向恢复本项文件，不动其他工作区内容。
