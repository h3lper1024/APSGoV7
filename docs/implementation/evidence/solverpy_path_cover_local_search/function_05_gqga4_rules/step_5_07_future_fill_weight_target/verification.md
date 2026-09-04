# 功能 5.7：逐链总重的未来填充目标指标验证

## 范围与确认依据

- 实施前提交 `7bf94287d54485d0c684221d32b2adc84bdf4cd2`；分支 `codex/solverpy-path-cover-clean`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18` / Python 3.10.18、Ruff 0.12.0。
- 用户确认“按照每条链统计，就是每条链的总重量和”“超过 1200 吨不算偏差，只要没超过最大 2000 都可以”。设计 v0.9 第 11.8.4 节、实施计划 v0.24 第 8.6.3 节据此关闭原公式缺口。
- 生产改动只有具体规则和加载注册两文件；新增一个测试文件、本目录两份证据，同步设计、实施计划及 AGENTS，共八文件。新路径创建前确认不存在，未覆盖旧残留。
- 按 Ponytail Lite 复用 `Chain.total_weight`、精确正向差和 `sum_weights()`；不增加台账、缓存、基类接口或依赖，不修改其他规则、模型、冻结输入、评分或门槛。

## 业务及技术结果

每条链先计算 `max(0, future_fill_weight_target - chain.total_weight)`，再精确求和为 `future_fill_total_gap`。普通真实材、实际过渡材、拆片、虚拟材全部按实际节点重量参与；不按计划期或未来借用状态筛选。1000/1400 吨两条链的总缺口为 200，不能互相抵消。

目标是唯一显式的非负有限 Decimal 参数，不在通用核心默认写死 1200。停用无贡献，非法配置和错误主体拒绝；不提前舍入或进行容差截断。规则只输出诊断，不新增违规、评分级或搜索动作。已有链重下限 700、上限 2000 和重量容差不变；1200～2000 的填充缺口为零，超过 2000 仍由已有链重规则禁止。旧 `chain_target_weight_deviation` 的 2000 目标距离是另一项诊断，不是新填充缺口或违规。

## 参考事实与预期差异

指定参考 `solver.py` SHA-256 为 `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318`。源码第 69 行仅列出支持的规则身份；通用规则解析保存记录，没有读取 1200 并计算填充缺口的实现。冻结 `resolved_rules.json:2-5、731-734` 的链重为 700/2000/2000；`1161-1164` 启用未来填充目标 1200，断言为恒真；资源策略与 `solver_config.json:99` 同为 1200。

可选脚本以显式 `--reference` 路径核验 SHA 后独立加载参考；只保留冻结链重及填充目标两条定义，对小方案调用原始 `evaluate_plan()`，不修改、隔离替换任何参考函数，也不执行搜索：

| 分组 | 数量 | 结果 |
|---|---:|---|
| 参考实际评价入口：目标 0/700/1200/2000/5000、停用、缺少参数、移除规则 | 8 配置 × 3 方案 = 24 | 完整评价及七级质量键均不随目标配置变化，没有填充缺口指标 |
| 新规则显式公式 | 7 | 单链边界、跨链不抵消、全材料总重、精确微小缺口及零目标符合预期 |
| 新规则停用 | 1 | 违规和指标均为空 |

首个预期差异：单链 700、目标 1200，参考无该指标，新规则输出缺口 500；这是已确认的指标补充，不是参考等价失败。首个未解释差异 `null`，参考文件和两份冻结输入前后哈希不变。该脚本不是 GQGA4 完整规则或搜索验收。

## 实际检查

共享树全部退出 0：专用 31 项（0.07 秒）；聚焦 245 项（0.26 秒）；规则累计 584 项（0.56 秒）；全量累计 843 项（1.43 秒）。参考脚本的 24/7/1 组检查通过，外层 1.46 秒。Ruff 与格式检查覆盖 `src tests tools` 及本脚本，41 文件通过；`git diff --check` 通过。独立只读复核没有实质性问题，另行复跑聚焦和全量通过。

共享树残留检查通过：稳定 16、易变 992，无删除；三个既存 Ruff 缓存新增路径与前项记录相同，未暂存。基础接口、资源事实、工程工具、冻结基线均无差异。

初验暂存树 `bdb10c998021c8304519eee0cbb85e817cd80f2d` 导出至 `/tmp/apsgo-future-fill-5is7Gp`：聚焦 245 项（0.26 秒）、规则累计 584 项（0.56 秒）、全量 843 项（1.04 秒）均通过；参考 24/7/1 组结果与共享树一致，残留检查为 `clean_export`、无新增或删除，导出编译通过；后三项组合命令外层 4.46 秒，全部退出 0。补齐本记录及计划完成状态后，按以下同样命令再次复测最终暂存树再提交，不以初验树替代最终提交树。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_future_fill_weight_target.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_07_future_fill_weight_target/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_07_future_fill_weight_target/reference_differential.py
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_07_future_fill_weight_target/reference_differential.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
git diff --exit-code HEAD -- tests/baselines tools pyproject.toml src/apsgo_scheduler/core/rules/base.py src/apsgo_scheduler/core/rules/rule_set.py src/apsgo_scheduler/core/resource_facts.py
git diff --check
```

提交前按精确八文件白名单暂存；以 `git write-tree` / `git archive` 导出至新 `mktemp -d` 目录，执行同样聚焦、规则累计、全量、参考和残留检查，仅在导出目录运行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests`。补齐导出结果后再次复测最终暂存树；核验 UTF-8 无 BOM、设计哈希与实施计划一致以及最终提交树等于被测树。当前提交身份由 Git 历史标识，不在本文自引用。

## 未完成边界

这一步完成规则贡献，不代表完整方案评价、资源自动计算、搜索填充或最终审计已接线。功能 5.19 映射 GQGA4 配置，功能 7 接通完整评价，功能 21 执行真实 GQGA4 质量/性能门槛。下一项为功能 5.8“未来借用比例规则”；本轮不放宽链数、欠重、禁止违规或全流程 180 秒门槛。
