# 功能 5.15 温度区间重叠验证

- 实施前：`a1fa187dc88ee17849fbc76252ae0f59962253a0`；Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 七文件：具体规则、唯一注册、新温度测试、本目录差分/证据、实施计划、AGENTS。设计、公共接口、其他规则、原始基线及门槛不改。
- 显式三参数、既有两节点入口；按参考顺序处理忽略、虚拟自适应、缺值及温区交集。没有新指标或评分。实际过渡材不按虚拟豁免，完整实值沿用浮点运算顺序及严重度公式；边界见计划第 8.6.10 节。

| 检查 | 共享树，最终退出 0 | 干净导出 |
|---|---|---|
| 专用 | 47 项，0.08 秒 | 随聚焦执行 |
| 聚焦 | 266 项，0.30 秒 | 266 项，0.30 秒 |
| 规则累计 | 792 项，0.79 秒 | 792 项，0.78 秒 |
| 仓内累计 | 1031 项，1.08 秒 | 1031 项，1.06 秒 |
| 参考差分 | 3190 启用、112 停用、58 忽略一致；三类保护各 12；1.843 秒 | 同样 3190/112/58/12/12/12，1.808 秒 |

初验导出 `/tmp/apsgo-temperature-AlFhUY`，树 `f95ae065dca1b0e7cd8e1e0b04bc72451121aabe`，全部退出 0。独立只读复核聚焦 266 项（0.28 秒）、仓内累计 1031 项（1.12 秒）通过，无阻塞问题。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_temperature_overlap.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_15_temperature_overlap/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
```

开发首次累计退出 1（1 failed, 1030 passed，1.06 秒）：架构守卫将温区列表下标 `3` 识别为禁止的基准数字，定位 `concrete.py:245`。单独执行 `tests/architecture/test_clean_room_boundaries.py::test_production_tree_is_self_contained` 复现退出 1（0.05 秒）。改为具名的左右最低/最高温度解包，原计算顺序与公式不变；不改守卫或放宽测试。修正后累计 1031 项通过（1.03 秒），最终共享树同集合再通过，如表。首次格式检查后的标准格式化不改变语义。

实际参考 `edge_rule_failures()` 调用 3396 次；首个原始投影保护为 `-1E1000` 被参考转空并放行、目标定位拒绝；首个交集保护为两端 `[-1E308,1E308]` 产生无限交集、目标拒绝；首个严重度保护为相距 `1E308` 且下限 0 的温区，参考严重度无限、目标拒绝。各 12 例均按既有有限数值契约登记；正常/缺值/角色/启停的 3360 个案例一致，无未解释差异。缺值直接放行不能冒充完整真实输入合格，输入校验仍在功能 6。

参考 SHA `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318` 与冻结 resolved/context 前后未变。双树 Ruff `check --no-cache`、`format --check --no-cache` 对 `src tests tools` 加本项差分通过（46 文件），`git diff --check` 及保护文件不变检查通过。残留检查与干净导出编译均通过；补齐记录后对最终暂存树执行同一集合并核对提交树。

不执行完整输入标准化、排程、性能或 V3 核验，不代表 GQGA4 整体验收。独立提交后继续 5.16；恢复按实施前提交反向恢复本项，不操作其他工作区内容。
