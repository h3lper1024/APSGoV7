# 功能 4：规则基类、规则集、注册表与加载器

- 实施前提交：`95fcd5b5c4e5718b08fd54afb8e45e001b8c1826`。
- 分支：`codex/solverpy-path-cover-clean`；环境：macOS / Darwin arm64、zsh、Conda `apsgo_v6_3.10.18`、Python 3.10.18、Ruff 0.12.0。
- 依据：目标设计 v0.3 第 11、12.1、12.3 节，实施计划第 8.5 节。

## 精确范围

新增六份生产文件：`core/rules/__init__.py`、`base.py`、`helpers.py`、`concrete.py`、`rule_set.py` 和 `app/rule_set_loader.py`，均位于 `src/apsgo_scheduler/`。初始化文件是新普通 Python 子包所必需，不是新增架构层。

新增三份测试：`tests/core/rules/test_rule_base.py`、`test_process_rule_set.py`、`tests/app/test_rule_set_loader.py`。连同本证据、实施计划和 `AGENTS.md`，精确白名单共 12 个文件；既有生产契约、参考数据、门槛、工具和工程配置未改。

## 已实现与未实现

1. 冻结的 `Rule` 基类直接连接具体规则类，无第三层继承；复用唯一作用域、拆单模式、数值校验、集合冻结和规范指纹工具。规则贡献逐条保序，禁止按违规主体去重。
2. 判定对象、违规、指标、质量项、任务上下文和拆单返回值按设计建立。任务顺序显式传入，索引必须精确对应非空唯一期序；虚拟原型目录可空，不把任务顺序放进规则对象。
3. 规则集分派启用节点、相邻、链和方案规则，检查指标唯一生产者和质量优先级。链片段及方案内订单保留自己的违规主体，聚合层仅检查规则身份和作用域。
4. 应用加载器重算完整 `RuleSetSpec` 指纹；包括停用记录、参数、启停、质量顺序和允许偏差。停用定义不查注册表、不实例化、不验证业务参数，但基础身份与重复身份仍检查。核心规则集持有启用实例及已核验指纹，不增加停用规则载体。
5. 两条合成规则证明不同配置可产生不同连接贡献、构造优先级和质量项顺序；它们不作为真实产线业务规则。完整数值质量键计算留在功能 7。
6. `ControlledOrderSplitRule` 仅声明最终具体类型和双参数入口，调用时显式 `NotImplementedError`，默认注册表不注册。测试替换该确切类型的方法证明原样传递主体和上下文；零生产者返回规范拒绝。具体同期间、未来借入、延后关系及拆分业务仍属功能 5.18。
7. `PlanRuleSubject.resource_view` 沿用既有延迟注解方式，功能 7 接入唯一真实视图；没有占位资源台账、实际审计、求解主循环或公开求解服务。

## 实测验证

| 检查 | 共享工作区 | 暂存代码树干净导出 |
|---|---|---|
| 本项三份测试 | 141 项通过，0.18 秒 | 141 项通过，0.17 秒 |
| 架构、API、应用、核心、参考身份累计 | 374 项通过，0.62 秒 | 374 项通过，0.62 秒 |
| Ruff 静态与格式 | 通过，32 个文件 | 通过，32 个文件 |
| 旧残留保护 | 通过 | 通过 |
| 编译、wheel 构建 | 按约定未在共享树执行 | 通过 |
| wheel 独立导入 | 使用干净导出的 wheel 及 `python -I`，通过 | 默认表恰好只有两条合成规则 |

上述命令退出码均为 0。暂存代码树：`20eaa4e949f9560bd533e060623d1e042f664d5c`；导出：`/tmp/apsgo-function04-NhSGcy`。wheel SHA-256：`8fde5c3a78f2694731d67cdbca8e8bb1f046558ac62f8dd1d14fe7beac691e6d`。补充本记录与状态文档后，提交前再次导出最终暂存树执行同一累计回归。

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/integration/test_reference_baseline_identity.py -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# 以下仅在干净导出目录执行。
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
conda run -n apsgo_v6_3.10.18 python -m pip wheel --no-build-isolation --no-deps --wheel-dir dist .
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -I -c 'import sys; sys.path.insert(0, "/tmp/apsgo-function04-NhSGcy/dist/apsgo_scheduler-0.1.0.dev0-py3-none-any.whl"); import apsgo_scheduler.app.rule_set_loader as loader; assert ".whl/" in loader.__file__; assert set(loader.RULE_REGISTRY) == {"SyntheticWidthLimitRule", "SyntheticNodePriorityRule"}; print("wheel import passed:", loader.__file__)'
```

初次架构回归发现加载器比较枚举数量时使用数字字面量 `3`，触发既有基准数字门禁；已改为按实际枚举字段数量比较，门禁未放宽。独立复核发现违规主体等值限制过严、注册类缺少作用域声明时泄漏 `AttributeError` 两项问题，均修正并补充回归；最终独立聚焦复测 141 项通过（0.16 秒），未发现剩余实质问题。

16 个稳定旧残留与 992 个原始易变路径均保留，三个先前已记录的 Ruff 缓存新增路径没有删除或暂存。本项未执行 GQGA4 排程及性能验收，不能将契约回归视为算法质量达标。下一项：功能 5.1“高表面连续数量规则”。
