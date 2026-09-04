# 逆宽规则补充：跨虚拟材真实端点幅度设计验证

- 修订前提交：`dc18757`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`，Python 3.10.18。
- 精确范围：详细设计、实施计划、`AGENTS.md`、本记录四份文档；接续此前未提交的设计草稿，未修改代码、测试、工具、冻结输入输出或正式门槛。
- 状态：仅业务设计同步；真实端点幅度检查未实现。下一项仍为功能 5.6，不因本文进入宽度规则编码。

## 确认内容与未决边界

用户确认本项是“逆宽规则”的补充，不仅针对连续逆宽。真实 A→虚拟 B→真实 C 中，各虚拟相邻边保持 200 mm 绝对差，真实端点另须满足 C−A≤20 mm；1000→1100→1050 虽先增后减，仍因端点净增 50 禁止。用户随后确认中间多个连续虚拟材采用同样处理，不放宽连续虚拟数量等其他规则。允许连续逆宽不等于取消幅度限制，不新增独立用户开关或第二份 20 mm 配置。

设计 v0.7 第 11.8.2 节保存九个业务语义的推导案例，包含多个虚拟节点的允许/拒绝及遇到真实节点后基准更新。单/多虚拟范围已确认；只有具体类划分、身份/作用域、同一配置的绑定及直接调用边界须在功能 5.13 前确认，计划 v0.21 第 8.6.2 节明确这一检查点。业务确认不等于接口决策已全部完成。

当前代码证据：`src/apsgo_scheduler/core/rules/base.py:62-78、272-287` 的相邻主体只有两个节点且每条具体规则只有一个作用域；`src/apsgo_scheduler/core/rules/rule_set.py:122-126` 已有链级入口；`src/apsgo_scheduler/core/rules/concrete.py:395-440` 的连续规则停用后不执行。故不能把新限制绑在连续禁止开关，也不能声称普通边缓存能检查第三个节点。建议复用链级检查，未更改框架。

参考文件仍为 `/Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py`，SHA-256 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。只读核对其 `774-784` 的相邻宽度函数与 `954-1004` 的链内逆宽扫描，均无虚拟桥真实端点净增宽检查。本轮未运行参考求解；相邻宽度通过不等于参考整链合法。

## 实际验证

- 共享树累计测试：初验 709 项通过，0.94 秒；用户确认多虚拟同样处理并同步后复测仍为 709 项通过，0.91 秒，退出码均为 0。没有增加业务测试或改变旧期望。
- 独立数字检查：初验七个案例通过；扩展多虚拟后九个案例在共享树和干净导出均通过，退出码 0，命令外层均约 1.52 秒。下面保留九案例脚本，不代表规则实现或完整排程验收。
- 代码/测试/工具/基线未变、文档格式、开始时残留保护均通过；稳定残留 16、易变残留 992，未删除任何残留。此前已存在的三个 Ruff 缓存新增路径保持记录，不纳入提交。
- 初验文档暂存树 `c3b65a28b388f0b457fc6bac0448343dfb66f089`，干净导出 `/tmp/apsgo-virtual-span-docs-AAlwY1`：累计 709 项通过，0.85 秒；残留保护通过，无新增或删除残留路径；九案例检查通过。
- 设计 SHA-256 `5a8b852722c53e520615eb14224de57a33155e0c57cebe9b94d26cec7c8a5c2e` 与计划 v0.21 一致；独立只读复核确认业务归属、相邻/链级检查边界与后续步骤一致。首个非预期差异：无；相对参考新增的端点限制已登记，未进行搜索轨迹对比。

累计测试与保护命令：

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --exit-code HEAD -- src tests tools pyproject.toml
git diff --check
shasum -a 256 docs/design/apsgo_v6_solverpy_rule_driven_path_cover_local_search_detailed_design.md
```

九案例脚本的实际提取和执行命令，在共享树与干净导出均退出 0：

```sh
set -o pipefail
awk '/^```python$/{capture=1;next} /^```$/{capture=0} capture' docs/implementation/evidence/solverpy_path_cover_local_search/virtual_bridge_real_width_span_design_fix/verification.md | PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -
```

数字检查使用以下标准库脚本，通过 `PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -` 从标准输入执行；整数例子不涉及物理容差金样，容差测试仍待代码实施：

```python
cases = [
    ((1000, 1010, 1020), True),
    ((1000, 1010, 1030), False),
    ((1000, 1100, 1050), False),
    ((1000, 1200, 1020), True),
    ((1000, 1201, 1020), False),
    ((1000, 1100, 950), True),
]
for (a, b, c), expected in cases:
    allowed = abs(b-a) <= 200 and abs(c-b) <= 200 and c-a <= 20
    assert allowed is expected, (a, b, c)
a, b, c, d, e = 1000, 1010, 1020, 1030, 1040
assert all(abs(y-x) <= 200 for x, y in zip((a,b,c,d), (b,c,d,e)))
assert c-a == 20 and e-c == 20 and e-a == 40
for widths, expected in [((1000, 1100, 1120, 1050), False),
                         ((1000, 1100, 1120, 1020), True)]:
    edges_allowed = all(abs(y-x) <= 200 for x, y in zip(widths, widths[1:]))
    assert (edges_allowed and widths[-1]-widths[0] <= 20) is expected
print('9 confirmed width examples passed; single/multiple virtual nodes covered; no solver executed')
```

补齐本记录后，提交条件是再次精确暂存四文件、以 `git write-tree` / `git archive` 导出最终树、重跑相同累计测试/残留/九案例检查，并核验 UTF-8 无 BOM 与提交树完全一致。不得提交未通过验证的新树。

本次未运行 GQGA4 求解、性能测量、构建或编译。正式链数、欠重、禁止违规和 180 秒性能门槛不变；当前提交身份由 Git 历史标识，不在提交内容中自引用。

## 勘误：参考实现已有真实端点检查（2026-09-03）

后续功能 5.13 实施时，完整复核同一 SHA-256 的 `solver.py:1067-1095`，确认参考已对一个或多个连续虚拟节点两端的最近真实节点调用 `width_allowed()`，并共用逆宽规则开关和真实材上限。上文仅检查 `774-784` 与 `954-1004`，据此推断参考缺少该逻辑，证据范围不足；“相对参考新增端点限制”的表述不成立。

本次保留原始验证过程供追溯，不改写历史测试结果。九个业务案例均符合参考已有行为；例如 `1000→1100(虚拟)→1050` 及 `1000→1100(虚拟)→1120(虚拟)→1050`，参考均给出真实端点违规、严重度 1.5。正式实现、正常宽度差分及缺宽度直接调用的保护差异见 [功能 5.13 验证记录](../function_05_gqga4_rules/step_5_13_width_transition/verification.md)。这次勘误不改变原始参考文件、冻结输入输出或验收门槛。
