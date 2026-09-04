# 功能 5.12 链内逆宽次数验证

- 实施前：`f318df205b67493b744ab450de33c4aeefedfd49`；环境 Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 七文件范围：具体规则、唯一注册、新规则测试、本目录差分和记录、实施计划、AGENTS。设计、原始基线、工具、其他规则及门槛不改。
- 计数复现保留宽度基准；`None` 只清基准，不清累计次数；全部角色/拆片按节点参与。唯一 `max_count` 非负整数，超限全链一条禁止违规，严重度为次数减上限，指标 `reverse_width_count`。不混入相邻连续逆宽或承载牌号。
- 非有限浮点投影按既有数值有效性契约明确拒绝并定位节点；停用不读宽度、不产出贡献。完整语义见计划第 8.6.8 节。

| 检查 | 共享树，退出 0 | 干净导出 |
|---|---|---|
| 专用测试 | 38 项，0.08 秒 | 随聚焦集合执行 |
| 聚焦 | 257 项，0.29 秒 | 257 项，0.32 秒 |
| 规则累计 | 701 项，0.70 秒 | 701 项，0.92 秒 |
| 仓内累计 | 940 项，0.99 秒 | 940 项，1.21 秒 |
| 专项差分 | 3888 启用一致 / 21 停用 / 9 溢出保护，2.055 秒 | 同样 3888/21/9，2.913 秒 |

初验导出 `/tmp/apsgo-reverse-count-jGx8WG`，暂存树 `cf99e24d49cb162730223fdf28991a8ecfa7202e`；全部命令退出 0，独立复核聚焦 257 项通过（0.29 秒）。补齐记录后再次导出最终暂存树并执行同一集合，提交后核对树身份一致。没有失败测试或未解释差异。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_reverse_width_count.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_12_reverse_width_count/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
```

参考实际链评价调用 3918 次，只隔离本条规则；21 个停用中有 9 个同时验证停用不读取溢出宽度。首个停用差异为 `1000→1010→1005/max_count=0`：参考仍输出次数 2、不合格标志，目标无贡献。首个保护差异为 `1E1000→1010→1005`：参考 `optional_float` 转成空值后计数 0，目标以 `n-0.width` 定位拒绝。这不是参考完整输入校验差异；正常数值等价，未解释差异为空，源文件及冻结输入前后哈希未变。

Ruff `check --no-cache`、`format --check --no-cache` 对 `src tests tools` 加本目录差分脚本均通过（44 文件），`git diff --check` 通过。残留保护执行 `tools/check_workspace_residuals.py --verify`；编译仅在干净导出执行 `python -m compileall -q src`。最终提交树须等于最后一次干净导出复测树。

未运行输入标准化、完整排程、搜索开关对照或 V3 核验；不宣称整体 GQGA4 验收。提交后跳过已完成的 5.13，继续 5.14“战略客户构造优先级规则”。恢复以实施前提交为依据，反向恢复本项文件，不操作其他工作区内容。
