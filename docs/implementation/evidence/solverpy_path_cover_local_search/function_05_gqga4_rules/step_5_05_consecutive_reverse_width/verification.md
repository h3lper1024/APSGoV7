# 功能 5.5：连续逆宽规则验证

- 实施前提交：`74c4802`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`、Python 3.10.18。
- 依据：设计 v0.6 第 11.8.1 节与实施计划功能 5.5；用户已确认按连续相邻增宽判断，不保留承载牌号许可。
- 精确七文件：`src/apsgo_scheduler/core/rules/concrete.py`、`src/apsgo_scheduler/app/rule_set_loader.py`、`tests/core/rules/test_consecutive_reverse_width.py`、本目录 `reference_differential.py` 与本记录、实施计划、`AGENTS.md`。

## 实现边界

`ConsecutiveReverseWidthRule` 直接继承既有 `Rule`，用标准库 `pairwise` 单次扫描相邻边，不新增状态框架或中间规则类。增宽条件为 `float(right.width) > float(left.width) + 1e-9`；宽度为空、相等或下降会打断连续，从第二条连续增宽边起逐次报告严重度 1 的禁止违规。普通真实材、实际过渡材、虚拟节点和拆分片段均按相同公式判断，没有额外牌号过滤。

启用规则只接受空参数；停用定义保留完整配置身份，但不产生违规、指标或必需字段，也不读取隐藏许可开关。新增唯一指标 `consecutive_reverse_width_violation_count`，没有产生功能 5.12 的总次数指标，也没有实现功能 5.13 的单次逆宽上限或功能 6 的三条件材料分类。真实材统一 20 已写入设计，不能据本项宣称其连接实现已经完成。

## 验证结果

| 检查 | 共享树 | 暂存代码树干净导出 |
|---|---|---|
| 专用金样 | 30 项，已包含在聚焦回归中；单独运行 0.08 秒 | 同一 30 项全部通过 |
| 基类、规则集、加载器与专用规则聚焦 | 244 项，0.26 秒 | 244 项，0.25 秒 |
| 规则累计 | 450 项，0.42 秒 | 450 项，0.42 秒 |
| 全量累计 | 709 项，0.95 秒 | 709 项，0.90 秒 |
| 参考定义对照 | 4029 案例，分组结果如下 | 相同分组与首差输出 |
| 残留保护 | 通过，16 项稳定残留，3 个既有 Ruff 缓存增项 | 通过，无新增残留 |
| Ruff 检查/格式 | 通过，含探针共 38 个文件 | 最终暂存树再次检查 |
| 字节码编译 | 不在共享树运行 | 通过，退出 0 |

初验代码树 `869a8a9a8f9d4d3a608059e5f1d71c9ce9cb43d9`；导出 `/tmp/apsgo-consecutive-width-cvWfAG`。独立只读复核通过，并另用三个相邻节点直接判断的独立方法验证 3905 条链；没有复用生产代码的边状态扫描。

专用测试覆盖下降仍高于旧基准、平台、空值前中后、浮点容差下方/等于/上方、三类材料、同源拆分片段不去重、纯函数与冻结、错误主体、启用旧参数拒绝、停用旧配置不执行、结构化加载诊断及指纹篡改。

## 参考差异分组

不能将本规则报告为“与参考全部一致”。探针先用独立三节点公式校验目标稳定违规字段（主体、规则身份、作用域、原因码、处置、严重度）与指标，再分别隔离三种已确认差异，禁止忽略其他参考违规；不比较两套实现的展示文案：

| 分组 | 数量 | 首个差异或结论 |
|---|---:|---|
| 启用且无承载冲突，定义一致 | 3070 | 参考和目标当前边位置一致 |
| 启用且无承载冲突，相邻定义修正 | 959 | `1000→1005→1005`：参考在末端报连续违规，目标不报；属于已确认平台打断 |
| 停用修正（两种定义本来一致） | 2 | `1000→1010→1020`：参考仍报 1 条，目标违规和指标均为空 |
| 承载过滤移除（无连续相邻增宽） | 12 | 非 SPHC 的 `1000→1010`：参考报承载违规，目标不报；不是连续开关的修正 |

4029 个定义案例由 3905 个短宽度序列、104 个材料角色组合、18 个数值边界及 2 个真实拆分场景组成；4 个全虚拟组合因核心链必须包含真实节点而明确排除，不计入通过数量。虚拟节点豁免旧承载过滤的情况也在独立承载组中验证。

探针还保留独立总次数边界：`1000→1010→1005` 的参考基准计数为 2，目标连续违规数为 0。这只是参考事实与职责隔离断言，不冒充尚未实现的功能 5.12 测试。首个未解释差异为 `null`。原始参考 SHA-256 固定为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`，未修改任何原始输入或输出。

## 实际命令

以下命令在共享树与干净导出分别执行，退出码均为 0：

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_consecutive_reverse_width.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_05_consecutive_reverse_width/reference_differential.py /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

静态检查与保护：

```sh
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_05_consecutive_reverse_width/reference_differential.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_05_consecutive_reverse_width/reference_differential.py
git diff --check
git diff --exit-code HEAD -- tests/baselines tools pyproject.toml docs/design/apsgo_v6_solverpy_rule_driven_path_cover_local_search_detailed_design.md
```

只在干净导出执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src`。补齐本证据后再次导出最终七文件暂存树，重跑三组测试、探针及残留/静态检查；核验 UTF-8 无 BOM 和树身份后独立提交。提交身份由 Git 历史确定。

未运行完整 GQGA4 排程或性能验收，22 条链/零欠重/零禁止违规/180 秒门槛不变。下一项为功能 5.6“原订单延后计划期规则”。
