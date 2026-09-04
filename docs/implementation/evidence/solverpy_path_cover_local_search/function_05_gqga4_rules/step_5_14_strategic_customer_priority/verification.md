# 功能 5.14 战略客户构造优先级验证

- 实施前：`d31c78f9b3353f3b447fa05409f9307044d930f8`；Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 仅新增具体规则、唯一注册、一个规则测试、本目录差分和证据，更新实施计划及 AGENTS；不改设计、公共接口、原始基线、搜索或质量门槛。
- 客户名称可空，关键词按原文子串匹配；显式整数等级共用于构造排序和 `strategic_customer_rank`，没有违规或新评分项。停用无贡献；详见计划第 8.6.9 节。沿用既有节点入口和文本校验，无新匹配框架。

| 检查 | 共享树，退出 0 | 干净导出 |
|---|---|---|
| 专用测试 | 44 项，0.08 秒 | 随聚焦执行 |
| 聚焦 | 263 项，0.29 秒 | 263 项，0.29 秒 |
| 规则累计 | 745 项，0.73 秒 | 745 项，0.73 秒 |
| 仓内累计 | 984 项，1.02 秒 | 984 项，1.00 秒 |
| 参考差分 | 631 启用一致、554 停用中性表示、60 自定义等级、3 类型保护、6 目标虚拟契约；1.449 秒 | 同样 631/554/60/3/6，1.731 秒 |

初验导出 `/tmp/apsgo-customer-priority-Y306uC`，树 `c7e1ee8ecfebbc611dd872f5aeb5a6b20cae0382`，全部退出 0。独立只读复核聚焦 263 项通过（0.27 秒），专项差分一致，无阻塞问题。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_strategic_customer_priority.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_14_strategic_customer_priority/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
```

参考实际调用 `parse_rule_book()` 与 `normalize_nodes()` 13 次。冻结 531 单中 58 单名称命中、473 单默认等级；这里只投影客户字段，不宣称目标输入标准化完成。首个停用差异：参考移除关键词后所有订单等级 1，目标空优先级，双方都中立。首个自定义等级差异：空名称参考 1、配置默认 8 的目标为 8。首个类型差异：整数名称 123 被参考转为文本，目标按强类型边界拒绝。虚拟六例单列目标契约，不伪造参考原始虚拟订单路径。未解释差异为空，参考 SHA `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318` 与三份冻结输入哈希前后未变。

Ruff 对 `src tests tools` 加本项差分 `check --no-cache`、`format --check --no-cache` 双树通过（45 文件）。首次生产格式检查提示一行需折行，格式化后通过，无测试失败。精确七文件暂存，原始基线/工具/设计不变检查、残留保护及干净导出编译均通过；补齐记录再对最终暂存树执行同一集合，提交后核对树身份。

未运行完整排程、搜索开关对照、性能或 V3 核验；本项通过不代表 GQGA4 排程验收。后续为功能 5.15；如需回退，以实施前提交反向恢复本项文件，不动其他内容。
