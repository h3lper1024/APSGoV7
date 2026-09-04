# 真实材统一逆宽幅度与连续定义：设计修订验证

- 修订前提交：`8aab376`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`，Python 3.10.18。
- 精确四文件：详细设计、实施计划、`AGENTS.md`、本记录；未改代码、测试、工具、冻结输入、参考输出或正式门槛。

## 已确认决策与边界

用户最终确认普通真实材和实际过渡材的逆宽上限均为 20，仍保留可配置参数；此前真实过渡材 200 的提议不再采用。实际过渡材识别仍要求非战略客户、热轧 SPHC、执行标准 Q/TB 305-2017 三条件同时满足；不把这个分类条件变成所有订单逆宽的额外牌号许可。

设计 v0.6 第 11.8.1 节分开三项职责，并给出功能 5.5 的相邻连续增宽、空值/非增宽打断、启停、空参数、全材料参与及逐次违规契约。功能 5.12 独立总次数仍保持参考基准计数；生成虚拟材料的 200 绝对差容限不变；现有输入角色契约不变，不重复创建分类字段或规则层。实现与配置映射分别归后续独立步骤，本次不宣称已实现任何新规则。

原始配置和源码互证：冻结 `rule_context.json:2-25` 的三组分类值、普通上限 20 与参考解析一致；`solver.py:774-784` 确实把普通与实际过渡真实材都按 20 处理，只为虚拟边使用 200。`solver.py:954-1004` 另有保留基准连续判断及无条件 SPHC 承载过滤，两者与新业务定义的差异已登记到设计第 30.4 节，不能回写原始基线掩盖。

只读微例在已核验 SHA-256 的参考脚本上执行，退出码 0，用时约 1.24 秒：两类真实材分别增加 20 允许、增加 21 拒绝，共 4 个幅度案例；分类名单三项一致；宽度 `1000→1010→1005`、非 SPHC 且显式禁止连续时，参考依次发出 `C:reverse_carrier:1`、`C:consecutive_reverse:1-2`、`C:reverse_carrier:2`。目标连续定义不应报该连续违规，也不再实施承载白名单。这是登记的预期差异，不是目标运行结果。

## 验证结果与实际命令

- 共享树既有累计 679 项通过，0.85 秒。
- 初验文档暂存树 `7d39106cfe2e1c515aa158e2cf0ffc434c6a8a54`，干净导出 `/tmp/apsgo-width-docs-Ok1I9k`：同样 679 项通过，0.80 秒；残留检查通过。
- 独立只读复核通过；设计 SHA-256 为 `bdc5bcbba8510f1671ebb771bdfbaccb74b947fd81c7b95ca34fc5c7c758dfca`，计划 v0.19 同步。
- 首个非预期差异：无。仅删除旧待确认建议、补充已确认目标；不更改总阶段顺序、质量/性能门槛、评分或既有规则公式。

以下命令退出码均为 0；累计测试和残留检查在共享树、干净导出各执行一次：

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --check
git diff --exit-code HEAD -- src tests tools pyproject.toml
shasum -a 256 docs/design/apsgo_v6_solverpy_rule_driven_path_cover_local_search_detailed_design.md
```

补齐本证据和执行记录后再次精确暂存四文件、导出最终树，重跑同一累计测试及残留检查，核验 UTF-8 无 BOM 和树身份后独立提交。当前提交身份以 Git 历史为准。

未运行 GQGA4 求解、性能测量、构建或编译。下一项才是功能 5.5“连续逆宽规则启停语义”的实现与专用金样。
