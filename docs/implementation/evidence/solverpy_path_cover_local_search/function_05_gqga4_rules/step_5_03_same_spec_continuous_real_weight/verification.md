# 功能 5.3：同规格连续真实重量规则验证

- 实施前提交：`279095c`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin arm64、zsh、Conda `apsgo_v6_3.10.18`，Python 3.10.18。
- 依据：设计 v0.4 第 11.8、12.2 节与实施计划第 8.6 节；冻结规则的 `group_by_fields`、`max_real_weight`，参考第 448～453、858～908、1131～1142 行。
- 参考 SHA-256：`87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。
- 文件范围：具体规则、加载器唯一注册项、新规则测试、本目录两份证据、实施计划、`AGENTS.md`，共七份文件。

## 规则及能力边界

`SameSpecContinuousRealWeightRule` 直接继承 `Rule`。参数 `group_by_fields` 为非空、无重复的有序字段列表，当前版本显式支持 `thickness、width、grade` 的任意子集及顺序；`max_real_weight` 为有限非负 Decimal，均无默认值。GQGA4 使用全部三字段和 1000 吨上限。功能 5.19 将原配置 `node.` 前缀一次性映射掉，规则本身拒绝未知字段和未转换名称，不静默忽略。

参考实际使用可配置字段动态分组，不使用固定三元组属性；它也能读取其他 Node 字段，例如 `surface_grade`。**本版只实现上述三个已定义规格字段的配置能力，不宣称覆盖参考的任意属性读取。**若新增分组字段，须明确该字段的规范化、空值及版本口径，再扩展白名单和测试；当前不增加泛型属性解释器。

| 项目 | 当前口径 |
|---|---|
| 连续分组 | 普通真实节点，按配置字段逐项形成键；键变化即关闭前段，相同规格被其他段隔开后不合并 |
| 数值相等 | 宽度、厚度先转参考 float，再精确相等；不使用连接规则的 `1e-9` 容差 |
| 空规格 | 宽厚 `None` 保留在键中，两个相同空值可同组；`(None,)` 与“不参与”的 None 哨兵不同 |
| 牌号 | 节点去空白并转大写；普通真实牌号必须非空，即使配置字段子集未选牌号 |
| 材料范围 | 实际过渡材料和生成型虚拟材料打断且不读取牌号；每个真实拆单片段计入自身重量 |
| 违规 | 每个超限最大连续段一条，主体为链/规则/零基闭区间；原因 `same_spec_run_weight`；重量严格超过上限加 `0.000001` 时禁止，严重度为总重减上限 |
| 指标 | `max_same_spec_real_run_weight` 为精确 Decimal，含未超限段；无普通真实段时零 |
| 启停 | 停用不要求业务参数或字段，贡献/指标为空；错误主体仍拒绝 |

当前第二条连续重量规则出现后，才在同文件抽取 `_continuous_weight_contribution()`，共用连续扫描、精确求和、容差和严重度；具体规则仍各自负责命中、分组和字段校验。没有第三层继承或独立数值框架。窄钢提示文字改为带规则名称的通用句；其违规身份、原因、重量、处置与指标不变，不宣称展示字符串逐字不变。

原始输入检查仍待功能 6：同规格规则启用时全部原始订单（包括实际过渡材料）牌号非空，宽厚必需性来自各自连接规则，不能把所有分组字段一律强制非空。

## 验证

| 检查 | 共享工作区 | 暂存代码树干净导出 |
|---|---|---|
| 新规则专用金样 | 59 项，0.09 秒 | 包含于聚焦集合 |
| 聚焦回归 | 251 项，0.25 秒 | 251 项，0.25 秒 |
| 规则累计 | 347 项，0.32 秒 | 347 项，0.33 秒 |
| 全量累计 | 606 项，0.82 秒 | 606 项，0.82 秒 |
| 同规格参考差分 | 11900 案例一致，首个差异为空 | 同样通过 |
| 窄钢提取后回归差分 | 5613 案例一致，首个差异为空 | 同样通过 |
| 静态/格式/残留 | 通过 | 残留通过 |
| 编译 | 未运行 | 通过 |

以上均退出 0；代码树 `bf73130a4c063282baf832a0a68275dae47d7d14`，导出 `/tmp/apsgo-same-spec-weight-6X5I8O`。最终文档补齐后，再导出最终暂存树复跑同一集合后提交。

同规格差分为 11880 个穷举案例和 20 个边界案例：七种规格/角色状态、长度 1～3、十五种非空有序字段子集、上限 0/1000，按链契约跳过 90 个全虚拟组合。比较最大段重量、全部违规主体与严重度，兼查原因和处置。金样另覆盖低 Decimal 精度、字段重排身份不同但评价相同、空值元组、断段、拆片及启停。独立只读代码复核通过。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_same_spec_continuous_real_weight.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
for task_probe in step_5_02_continuous_narrow_steel_weight step_5_03_same_spec_continuous_real_weight; do
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/"$task_probe"/reference_differential.py /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
done
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# 仅在干净导出运行。
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules
```

16 个稳定残留及三个既有 Ruff 缓存增项保持，未覆盖、删除或提交旧文件；权威设计、冻结输入和门槛未改。本项不是 GQGA4 排程或性能验收。下一项：功能 5.4“链重范围与目标指标规则”。
