# 功能 5.13：逆宽幅度与跨虚拟材真实端点检查

## 范围与身份

- 实施前提交：`64841862ed117cf0ba50b9f134a7a8ceb890d1fe`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`，Python 3.10.18；Ruff 0.12.0。
- 用户批准把原功能 5.13 提前，先完成逆宽幅度，再恢复功能 5.6；不增加规则基线提交单元，不夹带原订单延后或其他下一条规则。
- 精确范围：三个生产文件 `core/rules/concrete.py`、`core/rules/rule_set.py`、`app/rule_set_loader.py`；一个新宽度测试文件；本目录差分脚本与记录；设计、计划、AGENTS 及历史宽度设计验证的勘误。未修改原有连续逆宽实现、规则基类、公开请求、通用指纹、冻结输入输出或正式门槛。
- 所有新增路径在创建前核验不存在；未覆盖旧残留文件。生产代码仅依赖标准库和当前包；外部参考只由此目录的可选证据脚本显式加载，不参与正常测试或运行。

## 已落实的业务及实现边界

1. 普通真实材料与实际过渡材料共用 `max_reverse_width`，有向增宽受限、减宽不受该上限限制；任一端是生成型虚拟材料时，改用 `virtual_width_tolerance` 限制绝对差。GQGA4 后续映射值为 20/200，核心不写死。
2. 一个或多个连续虚拟材料两端的最近真实节点，另查净增宽；真实 `1000→虚拟 1100→真实 1050` 及双虚拟 `1000→1100→1120→1050` 均禁止、严重度 1.5。遇到新的真实节点更新基准，不用全链第一个节点累计，不重复报相邻真实边。
3. 唯一公开 `WidthTransitionRule` 为 EDGE 规则；规则集自动派生内部 CHAIN 检查对象，保持父规则身份、名称、版本、启停与参数。内部对象不进入公开 `.rules` 或注册表，不能单独配置；停用父规则会移除两个入口，停用连续逆宽规则不会移除幅度检查。
4. 显式参数仅两个，要求非负有限 Decimal 且 float 投影有限，零值允许。物理宽度先转 float 再相减、与限值加 `1e-9` 比较；严重度按参考式计算后转 Decimal。`1e16` 附近把减法移到比较另一侧会改变结果，已锁定反例。
5. 直接比较端点缺宽度报 `missing_width`，投影非有限报 `invalid_width`，均禁止、严重度 1。链级检查只查比较到的真实端点，不替代相邻检查或后续输入校验。全部订单/虚拟原型的字段校验仍由功能 6 接线。
6. 完整配置指纹算法及旧字面金样不变，不承诺序列化整个派生执行对象的指纹与旧对象相同。未来功能 7/18 使用作用域入口，不能从公开规则列表重新过滤；功能 9 两节点缓存不能代替链级约束。

## 参考事实勘误与差分

参考文件：`/Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py`，SHA-256 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。

完整核对 `width_allowed():774-784`、相邻失败及严重度 `807-823`、`evaluate_chain():1067-1095` 后确认：**参考本就检查一个或多个虚拟节点两端最近真实节点**。旧设计只看了链扫描前段而断言缺少检查，属于证据遗漏，已在设计 v0.8 和历史验证记录勘误。本项正常宽度属于参考对齐，不是新增算法差异。

差分脚本仅开启宽度规则，并显式允许连续逆宽、使用通配承载配置，隔离已登记的其他语义变化；读取参考全部失败记录，不过滤掉不相干错误。直接规则、直接规则集和加载后的规则集同时比较，校验允许/拒绝、位置、原因对应和严重度，不以英文/中文说明文字逐字相同为目标。

| 分组 | 数量 | 结果 |
|---|---:|---|
| 相邻边正常宽度 | 1945 | 与参考一致 |
| 完整链内宽度检查（相邻及端点） | 72 | 与参考一致，含全部九个业务案例及真实角色组合 |
| 停用 | 6 | 与参考一致，两个入口均无贡献 |
| 缺宽度直接调用 | 31（边 27、链 4） | 已解释保护差异，目标明确禁止 |
| 非有限浮点投影直接调用 | 31（边 27、链 4） | 已解释保护差异，目标明确禁止 |
| 合计 | 2085 | 首个未解释差异为 `null` |

首个缺宽度例为 `[None, 1000]`：参考底层宽度函数返回允许，目标返回 `missing_width`、严重度 1。非有限例为 `[Decimal("1e1000"), 1000]`：参考物理字段规范化为 None，目标保留有限 Decimal 并返回 `invalid_width`、严重度 1。这些只描述隔离规则调用差异；参考完整输入阶段本来还会拒绝必需宽度缺失，不能据此声称参考全流程会接受非法输入。

## 验证结果

共享树执行，退出码均为 0：

- 新增专用测试 74 项（独立测试执行 0.11 秒）；聚焦宽度/基类/规则集/加载器 288 项，0.29 秒。
- 规则累计 524 项，0.49 秒；全量累计 783 项，0.94 秒，包含既有连续逆宽测试与旧配置指纹金样。
- 参考差分 2085 例通过，外层 1.51 秒，首个未解释差异为空。
- Ruff 检查与格式检查通过，5 个 Python 文件已格式化；`git diff --check` 通过；规则基类、核心公共契约、基线、工具及工程配置无变更。
- 开始及提交准备时残留保护通过：稳定 16、易变 992，无删除；三个已存在的 Ruff 缓存新增路径同前次记录，本项 `--no-cache` 不产生新缓存。未删除任何材料。
- 独立只读代码复核未发现实质缺陷；另外独立组合探测 7260 条链及一个多配置隔离例无差异，该一次性探测不替代保留在仓库的 74 项测试与差分脚本。

初验暂存树 `55adeb90efe917b0328ceaf78b8a17eecd1a2357` 已导出到 `/tmp/apsgo-width-rule-2eekvg`：聚焦 288 项（0.33 秒）、规则累计 524 项（0.50 秒）、全量 783 项（1.01 秒）通过，参考差分同样 2085 例且输出与共享树一致；残留检查为 `clean_export`、无新增或删除，`compileall` 通过。退出码均为 0；差分/残留/编译组合命令外层 4.70 秒。补齐本文与计划执行记录后必须再精确暂存、导出最终树，重跑相同验证再提交；最终树身份由 Git 及当次工具输出核对，不能拿初验树冒充最终提交树。

### 可复现命令

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_width_transition.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_13_width_transition/reference_differential.py /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
/Users/miles/anaconda3/bin/ruff check --no-cache src/apsgo_scheduler/app/rule_set_loader.py src/apsgo_scheduler/core/rules/concrete.py src/apsgo_scheduler/core/rules/rule_set.py tests/core/rules/test_width_transition.py docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_13_width_transition/reference_differential.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src/apsgo_scheduler/app/rule_set_loader.py src/apsgo_scheduler/core/rules/concrete.py src/apsgo_scheduler/core/rules/rule_set.py tests/core/rules/test_width_transition.py docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_13_width_transition/reference_differential.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --exit-code HEAD -- tests/baselines tools pyproject.toml src/apsgo_scheduler/core/rules/base.py src/apsgo_scheduler/core/contracts.py
git diff --check
```

精确暂存本项十个文件后，使用 `git write-tree` 与 `git archive` 导出至 `mktemp -d` 创建的新目录；在导出目录执行同样测试、差分和残留检查，另运行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests`。只在干净导出编译，不在共享树生成字节码。补齐记录后重新导出最终树复测，核对暂存文件 UTF-8 无 BOM，并确认最终提交树等于最终已测试树；不自引用当前提交 SHA。

## 结论与后续边界

本项完成的是逆宽规则实现、加载与规则集调用，不是完整求解器。未运行 GQGA4 主搜索、正式质量或性能验收，未实现边缓存、完整方案评价、输入标准化或最终无缓存审计；这些仍分别归后续步骤。连续逆宽、独立总次数及虚拟数量规则互不合并，不以本项放宽其他约束。收口后下一项为功能 5.6“原订单延后计划期规则”；原顺序到 5.13 时跳过已完成单元。
