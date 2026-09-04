# 功能 5.11 连续虚拟材料规则验证

- 实施前提交：`4f65405f9a4abacdabe1d86061e1639829af0e05`。
- 环境：Darwin 27.0.0 arm64，zsh，Conda `apsgo_v6_3.10.18`。
- 范围：`concrete.py` 的具体规则、加载器唯一注册、新规则测试、本目录脚本和记录、实施计划、AGENTS；不改其他规则、设计、原始基线或门槛。
- 复用既有材料角色、规则接口及标准库连续分组，无新抽象。按每个最大连续虚拟段单次判断，真实材/实际过渡材/拆片打断，虚拟用途及牌号不影响计数。
- 唯一参数 `max_count` 是非负严格整数；严重度为段长减上限，原因 `virtual_sphc_run`，指标为最大连续虚拟数，停用无贡献。完整语义见计划第 8.6.7 节。

## 实际结果

| 检查 | 共享树 | 干净导出 |
|---|---|---|
| 专用测试 | 30 项通过，0.07 秒 | 随聚焦集合执行 |
| 聚焦 | 249 项通过，0.32 秒 | 249 项通过，0.29 秒 |
| 规则累计 | 663 项通过，0.69 秒 | 663 项通过，0.65 秒 |
| 仓内累计 | 902 项通过，0.98 秒 | 902 项通过，0.93 秒 |
| 参考差分 | 1556 启用一致，16 停用预期差异，2.014 秒 | 同样 1556/16，2.104 秒 |

初验暂存树 `16d2da79f0ba29f180dd98262f66b84222cb190e` 导出到 `/tmp/apsgo-virtual-run-WPJVSk`，上述集合、静态/格式、残留及编译均通过。补齐记录后再导出最终暂存树，运行同一集合并核对提交树一致。

以上成功命令退出码均为 0。首轮开发回归曾为 901 通过、1 失败：测试错误地要求浮点参数进入规则加载阶段，但既有公开配置先以 `ValueError` 拒绝浮点参数；仅修正测试分组，保留该非法输入覆盖，不改变生产校验。最终共享树无失败。

专项脚本直接调用指定参考 `evaluate_chain`，共 1572 次；启用逐段比较位置、严重度、最大数量，主体前缀按目标统一规则身份规范化。首个停用差异 `VVVR/max_count=0`：参考仍报告最大值 3 和不合格标志，目标无违规或指标；这是已确认的严格启停，不是完整搜索等价声明。未解释差异为空，参考源及两份冻结输入前后哈希未变。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_consecutive_virtual_material.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_11_consecutive_virtual_material/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
```

Ruff 检查/格式使用 `--no-cache`，范围 `src tests tools` 加本目录差分脚本（43 文件）；残留保护按 `tools/check_workspace_residuals.py --verify` 执行。编译只在干净导出运行 `python -m compileall -q src`。共享树静态和差异检查通过，最终提交前复核原始基线、工具、设计未改，精确暂存七个文件。

未运行完整排程、输入标准化、搜索开关对照或 V3 核验；不声称 GQGA4 质量与性能验收通过。提交后进入 5.12“链内逆宽次数规则”。恢复以实施前提交为依据，反向恢复本项文件，不触碰其他工作区内容。
