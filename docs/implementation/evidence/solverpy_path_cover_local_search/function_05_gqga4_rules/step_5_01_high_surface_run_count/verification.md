# 功能 5.1：高表面连续数量规则——实施与验证

> 状态说明：以下前置检查和文档修订保留其当时事实；规则参数修复已独立提交 `a42be8d`。功能 5.1 现已实现与验证，实际范围和结果见末节，不再处于参数确认或编码等待状态。

- 检查当时状态：已完成只读语义对照；规则编码尚未开始，等待参数契约修订确认。
- 检查基线：`9578919`，分支 `codex/solverpy-path-cover-clean`，macOS / Darwin arm64，Conda `apsgo_v6_3.10.18`。
- 参考脚本：`/Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py`，SHA-256 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`，与冻结基线一致。

## 已核实规则语义

参考 `_segment_violations()`（858～908 行）、高表面规则调用（1097～1105 行）与本分支 `tests/baselines/gqga4/inputs/resolved_rules.json`（506～548 行）一致：

- 普通真实材料的表面等级属于 `FC、FD` 时连续计数，等级在两者之间变化不打断；
- 非高表面普通真实材料、实际过渡材料和生成型虚拟材料均打断；
- 拆单片段按每个排产节点计数，不按来源订单去重；
- 当前基线上限为 5，达到上限允许；一个连续超限段产生一条违规，严重度为实际数量减上限；
- 违规主体保留链、规则和零基闭区间位置；最长段指标包含未超限段，无命中时为零；
- 表面等级在输入规范化时去两端空白并转大写；启用该规则时原始输入缺失表面等级属于前置错误。

## 参数契约缺口与建议

目标设计第 11.6 节、`api/request.py:95` 与 `core/rules/base.py:270` 均将规则 `parameters` 定义为单值映射，复用 `core/contracts.py:122` 的 `freeze_scalars()`。它允许字符串、整数、十进制数、布尔值与空值，但拒绝列表和嵌套映射。

冻结配置实际包含高表面等级列表，还包含同规格分组字段列表、战略客户关键词列表和厚度分段区间配置。因此不是某条规则缺少数据，而是现有规则参数表达范围不足。已执行只读构造探测，两个样例均在进入加载器前被拒绝：

```text
ValueError: surface_grades must be a rule scalar, not a nested or mutable value
ValueError: thickness_rules must be a rule scalar, not a nested or mutable value
both structured rule parameter cases rejected before loading
```

探测通过断言确认两种拒绝行为，命令退出码 0；未写生产数据或修改代码。参数样例分别为 `{"surface_grades": ["FC", "FD"], "max_run_count": 5}` 与 `{"thickness_rules": {"basis": "thinner", "ranges": []}}`，调用当前 `RuleDefinitionSpec` 构造器。

建议单独确认并修订：**只扩展规则参数**为“单值 + 有序列表 + 字符串键分组配置”，列表在构造时转为只读元组，映射递归冻结；订单和虚拟原型的 `rule_attributes` 继续只允许单值。沿用现有规范指纹，不新增规则层级、不引入表达式语言，也不改变规则公式、门槛或求解流程。

编号参数键或把列表塞进 JSON 字符串在技术上可以编码，但会引入额外表示和解析约定，本轮没有擅自采用。当前设计及测试明确限定单值，因此推荐修订需要用户确认，不能混入功能 5.1 的规则提交。

独立只读复核确认相同缺口和范围建议。后续顺序：确认后独立 `#fix` 修订设计、参数契约和测试，再恢复功能 5.1；确认前不新增规则公式或修改既有契约。

## 本次记录验证

本次只修改本证据、实施计划和 `AGENTS.md`。共享树累计 374 项通过（0.65 秒），暂存树 `3231efe3013f8210906986658af1194429a8412b` 的干净导出 `/tmp/apsgo-rule-parameters-precheck-xodCyN` 同样 374 项通过（0.61 秒）；两处残留保护及代码/基线未变检查通过，以下命令退出码均为 0。补记本段后，提交前再次导出最终暂存树运行累计回归。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --exit-code HEAD -- src tests tools pyproject.toml
git diff --check
```

本次没有新规则测试、GQGA4 排程或性能结果，不能将既有契约测试视为高表面规则已实现。

## 用户确认后的文档修订（2026-09-03）

- 修订前基线：`637145f16f196368d289ff9b8fc60df8aa3a6a0c`，仍为 `codex/solverpy-path-cover-clean`，macOS / Darwin arm64、`zsh`。
- 用户确认仅扩展 `RuleDefinitionSpec.parameters` 和 `Rule.parameters`：允许标量、有序列表和字符串键分组配置，递归冻结；订单与虚拟原型的扩展属性仍为标量。
- 设计 v0.4 统一请求/规则冻结入口、输入隔离、启停数据形状校验、非法值与循环拒绝以及旧扁平指纹兼容要求；已有规范编码器直接复用，不增加表达式语言、第三层规则类或文本编码协议。
- 实施计划 v0.11 第 8.5.1 节将参数契约修复列为独立提交，完成后才进入功能 5.1。上文探测失败和当时待确认状态保留为历史，不代表修订已经实现。
- 本轮精确范围只有目标设计、实施计划、`AGENTS.md` 和本证据四份文档；未改源代码、测试、冻结输入或门槛，也未执行 GQGA4 排程。

### 本轮文档验证

共享树既有累计 374 项通过（0.64 秒）；暂存树 `10efbbdcde0627d597a7dab455aa4f0b17fa582e` 的干净导出 `/tmp/apsgo-rule-parameter-docs-TZSZeL` 同样 374 项通过（0.60 秒）。两处残留保护通过：共享树 16 个稳定残留不变，仅保留开始前已经存在的 3 个 Ruff 缓存增项；干净导出无残留增项。以下命令退出码均为 0：

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --exit-code HEAD -- src tests tools pyproject.toml
git diff --check
shasum -a 256 docs/design/apsgo_v6_solverpy_rule_driven_path_cover_local_search_detailed_design.md
```

四份文档均通过严格 UTF-8 无 BOM 解码检查；独立只读复核未发现目标契约、实施白名单、恢复入口或历史状态矛盾。设计 SHA-256 为 `828d8cb612e51ac4a3af5b86f44e12b09aa2f5e6248747809492e66726d1216f`，与实施计划第 2.2 节一致。

补记本段后，提交前再次导出最终暂存树并执行同一累计回归。当前 374 项是既有契约回归，不包含尚未编写的列表/分组参数新测试或具体规则测试；本轮未运行 GQGA4 求解、性能测量、构建或编译，不能据此宣称参数修复已实现。

## 功能 5.1 实施结果（2026-09-03）

- 实施前提交：`a42be8d`；分支 `codex/solverpy-path-cover-clean`，macOS / Darwin arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 依据：设计 v0.4 第 11.8、12.2、32.2 节，实施计划第 8.6 节，指定参考脚本同一冻结哈希的连续段扫描及高表面调用。
- 精确范围：`src/apsgo_scheduler/core/rules/concrete.py`、`src/apsgo_scheduler/app/rule_set_loader.py` 的本规则注册项、`tests/core/rules/test_high_surface_run_count.py`、本目录的 `reference_differential.py` 和本证据、实施计划、`AGENTS.md`，共七份文件。独立差分脚本仅为证据工具，生产和便携单元测试不导入或访问参考实现。

### 已实现口径

`HighSurfaceRunCountRule` 直接继承 `Rule`，通过标准库连续分组遍历一次链。只依据材料角色和冻结配置判断，不硬编码产线、等级名单或数量上限，不增加第三层规则或输入校验框架。

| 项目 | 当前实现 |
|---|---|
| 参数 | `surface_grades`：非空有序文本名单；`max_run_count`：非负整数，拒绝布尔值、小数、字符串和缺省参数；不套隐藏默认值 |
| 匹配 | 普通真实材料，等级按去空白/大写后属于配置名单；不同命中等级之间不打断 |
| 打断 | 非命中普通真实材料、实际过渡材料、生成型虚拟材料；后两者不读取等级 |
| 拆单 | 每个普通真实拆单片段分别计数，不按来源订单去重 |
| 违规 | 每个最大超限连续段恰好一条，按链位置顺序发出；主体为 `<调用方链主体>:<规则身份>:<零基起点>-<零基终点>`；原因码 `high_surface_run_count`，禁止违规，严重度为 `Decimal(段长 - 上限)` |
| 指标 | `max_high_surface_run_count` 为整数，包括未超限段；无命中为零 |
| 停用及错误 | 停用不要求业务参数、不产生指标或违规；错误主体仍拒绝；普通真实节点等级缺失/非文本/空白时报错，不伪装成非命中 |

参考连续段比较使用 `阈值 + 0.000001`，本规则的段长和合法上限均为整数，因此直接 `段长 > 上限` 与参考严格等价。冻结 JSON 中上限写作 `5.0`：功能 5.19 导入中立配置时须先确认是整数值，再转换为 `5`；不得静默截断非整数或把参考默认值搬入规则。

字段完整性与链内计数是两个边界：参考输入预检要求全部原始订单（包括实际过渡材料）具有表面等级；链内匹配只读取普通真实材料。前者继续由功能 6 实现，已回写实施计划；本规则通过不代表原始输入已完成规范化验收。

### 验证结果

| 检查 | 共享工作区 | 暂存代码树干净导出 |
|---|---|---|
| 专用金样 | 40 项，包含在下述聚焦集合 | 同一 40 项 |
| 聚焦及规则累计 | 各 232 项通过，各 0.22 秒 | 各 232 项通过，各 0.22 秒 |
| 架构/API/应用/核心/参考身份全量累计 | 491 项通过，0.67 秒 | 491 项通过，0.66 秒 |
| 参考差分 | 1554 个有效案例一致，首个差异为空 | 同样通过 |
| 静态、格式、旧残留保护 | 通过 | 残留保护通过 |
| 编译 | 按约定未在共享树运行 | 通过，含本项差分脚本 |

以上命令均退出 0。暂存代码树为 `5e2dadf7e80d8c537a3afd336a3d82b41ca0beb0`，导出 `/tmp/apsgo-high-surface-0zgAeK`；补齐状态和证据后，提交前再次导出最终暂存树运行相同回归。

差分脚本枚举 FC、FD、非命中普通真实、实际过渡、生成型虚拟五种节点的长度 1～4 序列，上限取 0 和 2，另加上限 5 时长度 6、7 的高表面段；按核心链契约跳过 8 个全虚拟链案例。逐项比较违规主体、严重度和最长段指标，兼查原因码及处置类型，未运行完整求解。独立评审还以另一种顺序扫描核对了 5456 个组合，没有发现实现缺陷；评审建议的自定义等级和非命中段内缺失等级回归均已补入。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_high_surface_run_count.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_01_high_surface_run_count/reference_differential.py /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_01_high_surface_run_count/reference_differential.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_01_high_surface_run_count/reference_differential.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# 仅在干净导出运行编译。
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_01_high_surface_run_count/reference_differential.py
```

16 个稳定残留和原始易变路径保持，三个已有 Ruff 缓存增项未删除或提交。五份原始输入、正式质量/性能门槛和权威设计未改；本项没有 GQGA4 主搜索、全流程性能或完整规则集验收结论。下一项：功能 5.2“窄钢连续真实重量规则”。
