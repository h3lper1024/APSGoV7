# 阶段 8.1：混合违规下继续提前开工精修

## 范围与根因

- 实施前：`codex/earliest-process-start-constraint@e7d29d5`，macOS，Conda `aps_3.10.18`。
- `improve_numeric_refinement` 遇到其他禁止违规直接返回；`_descriptor_settings` 又会拒绝任何保留其他禁止违规的候选。必须同时修复，否则只删除入口仍无效。
- 最早开工启用时允许带既有违规搜索，统一消费/提交仍执行逐条其他规则禁止数量不增和九项严格改善；停用保持旧流程。严重度仍由原九项整体比较，不新建逐规则严重度门槛。
- 保留现场 YAML、SQLite、用户自行放开回写的 `final_audit.py`；不纳入提交。不切换订货量、不操作服务。

## 验证记录

共享树必要验证：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/core/test_earliest_process_start_search.py -q
```

退出 0，9 项通过，135.15 秒（含 Numba 编译）。批次大小 1 和 8 均能在保留单条窄钢超限的情况下清除手算小例的提前开工；核对其他各规则禁止数量不增、未增加等待、独立重算一致。停用、额度耗尽和拒绝新增逆宽违规的案例通过。该用时是测试进程耗时，不是排程性能成绩。

精确代码/测试树 `0cd5383374091f63d2b7f214fa415ecff907c76d` 导出到 `/tmp/apsgov7-earliest-repair-Abdcy4`，只含本项六文件，不含用户 YAML、SQLite、`final_audit.py` 差异。累计命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service -q
```

累计执行到约 68% 时，用户明确要求“不用进行全量测试，把订货量切换也执行了吧”。已停止该进程，退出 143；此前输出未出现失败，但不能记为全量通过，也不恢复全量测试。干净导出残留检查 `status=pass, mode=clean_export, errors=[]`；共享树检查仍为历史 8 个 V6 文件缺失，不改清单或恢复外部残留。最终只核验本项源文件/测试与已通过的 9 项专项版本一致，并补充订货量所需的小范围验证。

## 结果边界

本项验证搜索不会被既有窄钢违规整体阻断，不承诺消除所有提前开工。尚未运行改用订货量的真实 1 月完整对照。
