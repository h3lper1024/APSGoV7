# 欠重评分逐链两位汇总：设计修订验证

- 修订前提交：`adc52f1`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin arm64、zsh、Conda `apsgo_v6_3.10.18`，Python 3.10.18。
- 用户明确回复“保留两位小数就可以了”，对应本轮提出的欠重评分逐链舍入，不扩展为全部重量、阈值、严重度或其他评分项都截断两位。
- 精确四文件：权威设计、实施计划、`AGENTS.md`、本证据；生产代码、测试、冻结输入及门槛不改。

## 设计结果与执行边界

设计 v0.5 明确欠重评分第四项：真正欠重链的原始差值分别按 `ROUND_HALF_EVEN` 保留两位，再精确 Decimal 汇总；原始指标与违规差值不回写，欠重数量和 `0.000001` 规则容差不变。其余三项计数仍为整数，严重度/虚拟重量/借用重量仍各按参考路径六位投影。目标重量只提供指标。

新增命名声明 `UNDERWEIGHT_GAP_ROUND_2_THEN_SUM`，仅匹配 `underweight_total_gap + SUM + MINIMIZE`。实施计划 v0.17 第 8.6.1 节列出独立枚举/组合校验/加载诊断修复，须先于功能 5.19 新规则集映射；函数运算仍由功能 7 完成。本次没有实现新枚举、加载行为或评分器，不把文档确认当作代码完成。

参考实际是逐链六位量化后 Decimal 汇总并转 float 六位舍入，证据在功能 5.4 探针中保留；目标两位是用户确认的产品差异。设计第 30.4 节登记第四项和可能的首次接受轨迹分叉，实施计划功能 7、11、21 的验收口径同步。旧参考字节不改，新声明以后进入新 `quality_spec` 的指纹。22 条链、零欠重、零禁止违规与 180 秒门槛不变。

## 验证结果

- 文档六个数值案例经标准库 Decimal 检查通过：逐链 `0.0149+0.0149→0.02`、先求和才舍入为 `0.03`、`0.005/0.015/0.025→0.00/0.02/0.02`、`0.00000149→0.00`。这是设计公式验证，不是未实现的新评分器测试。
- 共享树既有全量累计 657 项通过，0.92 秒；暂存文档树初验 `253d47c31e0535371384ef0d9bc2a315b7d21069`、导出 `/tmp/apsgo-underweight-docs-l13BPR`，同样 657 项通过，0.80 秒。
- 初验后补充本证据并修正“目标评分表误称参考原表”和“后续加载前提误写已实现”两处措辞，最终暂存树提交前重新导出运行相同累计回归。
- 当前目标设计 SHA-256 为 `7bd4da1cb3f008f13afcbf904f788abc60756a874d1cb9cd5281e22105318122`，实施计划同步；旧 v0.4 哈希保留为历史。
- 两处残留保护、源代码/测试/工具/基线未变检查和差异格式检查均通过；所有实际命令退出 0。独立只读复核的两处措辞问题均已修正。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --exit-code HEAD -- src tests tools pyproject.toml
git diff --check
shasum -a 256 docs/design/apsgo_v6_solverpy_rule_driven_path_cover_local_search_detailed_design.md
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'from decimal import Decimal, Context, ROUND_HALF_EVEN; d=Decimal; c=Context(prec=40, rounding=ROUND_HALF_EVEN); q=lambda value:c.quantize(d(value), d("0.01")); assert sum((q("0.0149"),q("0.0149")),d(0))==d("0.02"); assert q("0.0298")==d("0.03"); assert [q(x) for x in ("0.005","0.015","0.025","0.00000149")]==[d("0.00"),d("0.02"),d("0.02"),d("0.00")]; print("design arithmetic examples: 6 assertions passed; no production projection executed")'
```

本次未运行 GQGA4 求解、性能测量、构建或编译。下一项为独立欠重两位评分投影契约修复，随后恢复功能 5.5“连续逆宽规则启停语义”。
