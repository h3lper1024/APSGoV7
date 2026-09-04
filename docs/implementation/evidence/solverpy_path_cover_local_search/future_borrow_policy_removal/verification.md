# 未来借用比例规则与第七级评分移除验证

## 依据与范围

- 用户确认：删除未来借用比例规则及对应实施项，删除“优先借用重量更少”的第七级评分，保留借用事实和统计。
- 修订前提交：`b8e4c634ed12186e5e361aaafcae3e451e05002f`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`、Python 3.10.18；2026-09-03。
- 权威设计 v0.10 SHA-256：`6a5428f35ab576a613515161399bde201bbea1d086959162f2ead25b00c10ba7`；实施计划 v0.26 第 8.6.4 节。
- 精确五文件：设计文档、实施计划、`AGENTS.md`、`tests/core/rules/test_process_rule_set.py` 和本记录。不修改原始基线、通用资源字段、已完成规则或质量/性能门槛。

## 撤回内容与实现边界

撤回 `concrete.py` 的未提交 `FutureBorrowRatioRule` 及 `rule_set_loader.py` 的未提交注册，两份生产文件已与修订前提交一致；本次没有生产源码差异。原功能 5.8 取消，不算实现完成，不重排后续编号。

以下两份仅由本轮新建、未提交的文件已移除，删除前打包到 `/tmp/apsgo-withdraw-future-borrow-LE4RdF/withdrawn_uncommitted_files.tar`，可从该临时备份恢复；不自动恢复已取消功能：

- `tests/core/rules/test_future_borrow_ratio.py`；
- `docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_08_future_borrow_ratio/reference_differential.py`。

备份 SHA-256：`166415f239fb0535660fb2b96523900c25227ac7a90dbdd627828f18f2964f3d`，归档成员已逐项核验，仅含上述两个文件。

目标 GQGA4 六项依次为禁止违规数、禁止严重度、欠重链数、欠重缺口、链数、虚拟重量。欠重逐链两位再汇总不变；借用只做统计，不补同分裁决、隐藏惩罚或额外上限。已有禁止延后、覆盖守恒及拆单授权约束保持。

当前没有固定七级评分器，只有可变长度的质量配置接口。因此本次不新增评价实现，也不全局禁止其他产线显式配置借用指标。实际六级评价归功能 7、目标配置映射归功能 5.19、真实搜索对照归功能 21；三项配置回归不能证明这些功能已经完成。

原始规则快照仍是 17 条；目标映射需明确排除借用比例规则，变为 16 条（14 启用、2 停用）加独立拆单规则。原始参考脚本与七维结果不得改写；后续应记录六项同分但借用减少时“参考接受、目标拒绝”的首个差异。六项改善而借用增加时，目标也不附加借用否决。

## 前轮初验结果（历史，含额外参考核验）

新增三项回归：六项配置与两位欠重投影可加载；启用已删除类型报 `unknown_enabled_rule`；历史停用未知类型仍跳过执行、保留完整配置指纹。通用自定义质量、原始基线身份及其七维证据测试保持不变。

| 检查 | 共享工作树 | 初验暂存树干净导出 |
|---|---|---|
| 基类、规则集、加载器聚焦 | 217 项通过，0.24 秒 | 217 项通过，0.27 秒 |
| 规则累计 | 587 项通过，0.58 秒 | 587 项通过，0.62 秒 |
| 全量累计含参考身份 | 846 项通过，1.07 秒 | 846 项通过，1.14 秒 |
| Ruff 检查与格式 | 通过；40 个代码文件已格式化 | 通过；40 个代码文件已格式化 |
| 残留保护 | 通过；稳定 16 项，4 个既有 Ruff 缓存增项，未删除或暂存 | 通过；没有新增残留 |
| 字节码编译 | 未在共享树运行 | 通过 |

以上命令退出码均为 0；相对修订前 843 项，新增 3 项。独立只读复核确认没有固定七级比较器、隐藏借用惩罚或比例规则残留。首个未解释差异：无。

最终独立复核核对五文件范围、设计哈希、38 个实现加 1 个验收单元、取消状态、历史基线及备份，未发现实质问题；另行聚焦复测 217 项通过（0.23 秒，退出码 0）。

初验暂存树：`40e1970137f974582545e17141588fc2e2f8cd9a`；导出目录：`/tmp/apsgo-borrow-policy-removal-EgqA1j`。补齐本记录后再次验证最终暂存树，不能把初验树直接当作最终提交身份。

### 前轮误设提交前提：外部基线核验失败留档

后续暂存树 `298e91d381ae1e25862554276f6bc308a1f43ffd` 导出至 `/tmp/apsgo-borrow-policy-final-d7A6eN`。聚焦 217 项（0.24 秒）、规则累计 587 项（0.59 秒）、静态和编译通过；但累计首次为 845 通过、1 失败（1.09 秒，退出码 1），失败于 `test_v3_complete_file_manifest_and_metrics`。原 V3 目录当时不存在，列表为空；只读检索曾在桌面的 `日计划测试数据` 子目录找到同名目录，随后原路径恢复。

原路径恢复后原命令重跑仍为 845 通过、1 失败（1.02 秒，退出码 1），原因变为文件数 172 而非 170。逐项只读核验确认：原 170 条路径全部存在、SHA-256 全部一致；仅额外出现 `.DS_Store` 和 `rolling_final_repair/.DS_Store`。这是 macOS Finder 元数据增项，不是排产数据变化；仍不据此跳过既有严格测试或宣称最终复测通过。

本轮未移动外部基线、未删除这两份新增缓存、未更改测试或基线清单。此前请求清理缓存以解除提交阻塞是不必要的：用户指出 V3 文件不应阻塞此次 V6 提交，复核确认实施计划第 7.1 节原定累计命令本就只覆盖仓内四个目录。上表的 846 项通过及后续 845 通过、1 失败均保留为当时实际结果；不改写为“外部核验通过”，也不再把该失败当作本次提交前提。

## 本次提交的仓内回归

本次仅涉及规则/评分范围文档及规则集配置测试；没有修改外部参考工具、输入输出、基线身份或生产源码。恢复原定仓内提交门槛，不删测试、不加静默跳过、不调整 GQGA4 门槛。外部身份核验仍在基线冻结、正式对标或相关改动时单独执行；普通 V6 提交不依赖桌面目录状态。

| 检查 | 共享工作树 | 干净导出 |
|---|---|---|
| 基类、规则集、加载器聚焦 | 217 项通过，0.25 秒 | 217 项通过，0.25 秒 |
| 规则累计 | 587 项通过，0.59 秒 | 587 项通过，0.59 秒 |
| 仓内累计（架构/API/应用/核心） | 826 项通过，0.88 秒 | 826 项通过，0.87 秒 |

共享树退出码均为 0；Ruff 与格式检查通过，40 个代码文件已格式化。826 项为 V6 仓内测试，846 项为此前额外加上 20 项参考身份测试的数量，两者不得混称或据此宣称正式对标验收完成。

仓内验证初验树为 `0c2d2eb8ad1f443b29f82cf0c729ca86fe0ad3d6`，导出目录 `/tmp/apsgo-borrow-local-gate-0U1bp4`；上述导出测试、静态、残留保护和编译退出码均为 0。记录补齐后再次导出最终暂存树执行同一集合，再按精确五文件白名单提交。

独立只读复核确认原计划的分层与本次范围一致，另跑仓内累计 826 项（0.89 秒）及只读取仓内文件的输入身份、质量门槛、性能门槛检查 3 项，均通过；未访问或修改 V3。当前集成测试文件同时含仓内证据和外部核验，今后修改相关工具或门槛时仍须执行对应检查，不能一概排除。

## 实际命令

本次提交在共享树和干净导出使用同一组测试：

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_process_rule_set.py tests/core/rules/test_rule_base.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
```

共享树另执行：

```sh
git diff --check
git diff --exit-code HEAD -- src tests/baselines tools pyproject.toml
shasum -a 256 docs/design/apsgo_v6_solverpy_rule_driven_path_cover_local_search_detailed_design.md /tmp/apsgo-withdraw-future-borrow-LE4RdF/withdrawn_uncommitted_files.tar /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
```

以上哈希命令为前轮记录，本轮不再访问外部目录。指定参考脚本上次核验 SHA-256 为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。前轮扩展累计命令是在上述四目录后追加 `tests/integration/test_reference_baseline_identity.py`；该结果独立记载于前节。仅干净导出执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src`。

补齐记录后，提交前再次用 `git write-tree` / `git archive` 导出最终五文件暂存树，重跑上述测试及静态检查，核验 UTF-8 无 BOM、残留保护和最终树身份，再独立提交。当前提交由 Git 历史记录，不在内容中自引用。

未运行完整 GQGA4 求解或性能验收；最多 22 条链、零欠重、零禁止违规、20 次完整运行中位数/第 95 百分位均不超过 180 秒的门槛未调整。下一项为功能 5.9“软硬材连接规则”。
