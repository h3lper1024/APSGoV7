# 欠重两位评分声明契约修复验证

- 实施前提交：`220f0937f7d6dad6d4cdff925a0139bb8fe4a385`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`、Python 3.10.18。
- 依据：权威设计 v0.5 第 11.2、12.2 节，实施计划第 8.6.1 节；用户已确认欠重评分逐链保留两位后汇总。
- 精确七文件：`core/rules/base.py`、`app/rule_set_loader.py`、`tests/core/rules/test_rule_base.py`、`tests/app/test_rule_set_loader.py`、实施计划、`AGENTS.md`、本记录；源码位于 `src/apsgo_scheduler/` 下。

## 实现与边界

新增 `NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM`，配置字符串为 `underweight_gap_round_2_then_sum`。类型明确的质量项仅接受 `underweight_total_gap + SUM + MINIMIZE`；其他组合抛出 ValueError，加载器在同一构造边界转换为 `invalid_quality_combination`，保留质量项下标、身份和中文诊断，继续汇集其他配置错误。

复用完整配置指纹和冻结逻辑；不增加评分器、配置编码器或第二套计算框架。旧两种投影不加新限制，两个旧投影的字面金样保持；新声明改变指纹，不能沿用旧签名。新金样由未修改的实施前提交导出 `/private/tmp/apsgo-projection-baseline-ykBgjY` 取得，不以修改后的输出反向替换旧期望值。

测试证明加载新声明后，链重规则仍产生精确缺口和严重度 `0.0051`，与旧投影声明下贡献完全相同；没有量化为两位。实际半偶舍入和逐链汇总仍由功能 7 实现，功能 5.19 只映射配置。冻结输入、参考输出、质量与性能门槛和权威设计均未修改。

## 实测结果

| 检查 | 共享工作树 | 暂存代码树干净导出 |
|---|---|---|
| 基类、规则集、加载器聚焦 | 214 项通过，0.21 秒 | 214 项通过，0.21 秒 |
| 第 7 节全量累计 | 679 项通过，0.85 秒 | 679 项通过，0.81 秒 |
| 残留保护 | 通过；稳定残留 16 项，3 个既有 Ruff 缓存增项 | 通过；没有新增残留 |
| Ruff 检查与格式 | 通过；36 个代码文件已格式化 | 同一暂存代码树，未另改代码 |
| 字节码编译 | 不在共享树运行 | 通过 |

相对实施前累计 657 项，新增 22 项；重点覆盖合法加载、指标/聚合/方向非法组合、多个错误的稳定聚合、未知枚举、错误路径与主体、错误时不读取注册表、指纹篡改、原始贡献不舍入。既有规范编码和字面金样全部保留。

代码初验暂存树：`8667fc34756a8a691f805ad7870c4b2dc4d86e0c`；导出目录：`/tmp/apsgo-underweight-contract-I1WraG`。独立只读复核另跑 72 个声明组合及聚焦 214/累计 679 项，全部通过，未发现剩余缺陷。首个非预期差异：无。

## 实际命令

以下测试分别在共享树与上述干净导出执行，退出码均为 0：

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
```

共享树静态检查：

```sh
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
git diff --check
git diff --exit-code HEAD -- tests/baselines tools pyproject.toml docs/design/apsgo_v6_solverpy_rule_driven_path_cover_local_search_detailed_design.md
```

仅干净导出编译：

```sh
conda run -n apsgo_v6_3.10.18 python -m compileall -q src
```

证据和当前入口补齐后，提交前再次按 `git write-tree` / `git archive` 导出最终七文件暂存树，重跑相同聚焦与累计命令；核验 UTF-8 无 BOM、精确白名单、残留保护和最终树身份后独立提交。当前提交身份由 Git 历史记录，不在提交内容中自引用。

没有运行 GQGA4 求解或性能验收，也没有实现下一条规则；本次契约测试不代表新求解器达到正式门槛。
