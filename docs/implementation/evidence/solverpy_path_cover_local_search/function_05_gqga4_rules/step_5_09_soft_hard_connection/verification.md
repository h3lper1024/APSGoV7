# 功能 5.9：软硬材连接规则验证

- 实施前提交：`90bc8c5ca8ed561d23f81deb8d932341fc8301a8`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`、Python 3.10.18；2026-09-03。
- 依据：设计 v0.10 第 11.8、12.2.1、15、16.2 节；实施计划 v0.27 第 8.6.5 节。
- 精确七文件：`src/apsgo_scheduler/core/rules/concrete.py`、`src/apsgo_scheduler/app/rule_set_loader.py`、`tests/core/rules/test_soft_hard_connection.py`、本目录 `reference_differential.py` 和本记录、实施计划、`AGENTS.md`。

## 实现与边界

新增 `SoftHardConnectionRule` 并接入唯一注册表；直接继承已有 `Rule`，不增加接口、材料分类器或全局授权状态。三项参数显式展平，缺失/多余/错误类型/未知策略拒绝；停用只校验公共形状，不产生必需字段、违规或指标。

连接顺序为“任一虚拟材 → 任一实际过渡材 → 普通材类别/回退”。对应材料开关直接决定允许或禁止，不在关闭后继续比较类别。普通真实材及拆片类别去空白后大小写敏感地比较；任一缺失时才按策略处理，GQGA4 的回退要求两个非空热轧牌号原值相同，不读取成品牌号 `Node.grade`。禁止贡献一条、严重度为精确 `Decimal(1)`，沿用调用方主体与规则身份，无额外指标。

热轧牌号去空白/转大写属于后续输入规范化；本条规则仅把纯空白视为缺失，不改其原值。实际读取的非文本属性拒绝；材料短路不检查未使用属性。必需字段声明仅 `hot_roll_grade`，不强制软硬类别非空；未来边缓存仍须包含可空类别、热轧牌号与材料角色。

## 验证结果

新增 44 项专用测试，覆盖材料开关优先级、双向连接、未知类别文本、类别冲突不能被相同热轧牌号抵消、五种缺失策略、热轧牌号边界、拆片、参数和字段错误、停用、不可变及加载指纹。

| 检查 | 共享树 | 初验干净导出 |
|---|---|---|
| 专用金样 | 44 项通过，0.08 秒（测试协作者） | 由下述聚焦集合覆盖 |
| 专用/基类/规则集/加载器聚焦 | 261 项通过，0.29 秒 | 261 项通过，0.30 秒 |
| 规则累计 | 631 项通过，0.61 秒 | 631 项通过，0.65 秒 |
| 仓内累计 | 870 项通过，0.95 秒 | 870 项通过，0.94 秒 |
| 逐规则参考差分 | 283 启用一致、36 停用一致、2 个已说明保护差异；外层 1.39 秒 | 相同结果；外层 1.79 秒 |
| 静态/格式 | 通过；生产/测试/工具 41 个文件，专项脚本另查通过 | 通过；含专项脚本共 42 个文件 |
| 残留保护与编译 | 残留保护通过；共享树不编译 | 两项通过，无新增残留 |

当前以上已运行命令退出码均为 0。独立只读复核另跑聚焦 261 项（0.30 秒）、仓内累计 870 项（0.95 秒），未发现代码或测试问题。仓内累计不包含外部历史文件身份核验；本项未访问 V3 目录，不操作外部缓存。

初验暂存树为 `b95f7a8493936e7f8160d96526801d7720489e8f`，导出目录 `/tmp/apsgo-soft-hard-QD6mJ0`；补齐本记录后，最终暂存树再次执行同一测试集合。共享树稳定残留 16 项不变，4 个既有 Ruff 缓存增项保持原样，不暂存或清理。

专项差分复用指定参考 `soft_hard_allowed()` 和 `edge_rule_failures()`，只启用当前规则；共 321 次相邻入口核验，另加 285 次底层直接核验（不含相邻入口内部调用）。首个已说明差异：两端类别缺失、热轧牌号均为三个空格时，参考原始底层允许、目标按缺失拒绝；制表符同例另计一次。它们是不规范输入保护，不当作合法输入等价，也不修改原始参考期望值。其余 319 例一致，未解释差异为空。独立复核已校验用例数及分组，最终输出使用明确的 `direct_predicate_calls` 名称。

指定参考 SHA-256 为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`；脚本核对参考与仓内规则输入前后字节不变。不调用输入规范化器或求解器，不把该验证当作 GQGA4 排程质量或性能验收。

## 实际命令

共享树与干净导出执行相同集合：

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_soft_hard_connection.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_09_soft_hard_connection/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_09_soft_hard_connection/reference_differential.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_09_soft_hard_connection/reference_differential.py
```

共享树还执行 `git diff --check`，并核验 `tests/baselines`、`tools`、`pyproject.toml` 和权威设计相对实施前提交无差异。字节码编译仅在干净导出执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src`。

按七文件白名单暂存，经 `git write-tree` / `git archive` 导出初验；补齐记录后再次导出最终暂存树，执行同一集合，核对 UTF-8 无 BOM、残留保护与树身份再独立提交。提交身份由 Git 历史记录，不在内容中自引用。已存在的 `.idea/`、设计资源和易变缓存不纳入提交。

完整评分器、连接缓存及排程仍在后续步骤；本次不改六级目标、质量或性能门槛。下一项为功能 5.10“牌号连接策略规则”。
