# 阶段 2：仅关键候选调度的同预算完整对照

生产代码 `fe6c54d`，macOS / Conda `aps_3.10.18`；本阶段只补薄观察及报告，不接回收、不改生产。40 万次上限，搜索 9999 秒、收尾 10 秒，两个公共请求逐值相同。正式 YAML/SQLite 与九份保护材料未变。

## 完整结果

| 项目 | 原代码 40 万次 | 仅调度改进 40 万次 |
|---|---|---|
| 旧欠全部完成 | 6 月 22 日 01:11:45.225439 | 6 月 16 日 12:27:38.541713 |
| 旧欠加权平均等待 | 108.622121 小时 | 86.278471 小时 |
| 本月晚交 | 12 单 / 653.90 吨 | 15 单 / 715.37 吨 |
| 6 月 5 日交期晚交 | 3 单 / 348 吨 | 5 单 / 391.35 吨 |
| 6 月 10 日交期晚交 | 8 单 / 257.90 吨 | 8 单 / 252.02 吨 |
| 6 月 15 日交期晚交 | 1 单 / 48 吨 | 2 单 / 72 吨 |
| 6 月 30 日交期晚交 | 0 | 0 |
| 虚拟重量 / 链数 / 宽差 | 580 吨 / 25 / 15210 | 820 吨 / 25 / 14968 |
| 完整评价 / 接受 | 178216 / 880 | 158064 / 826 |
| 公共经过时间 / 进程 CPU | 1181.760883 / 1177.198096 秒 | 1100.634285 / 1096.807940 秒 |
| 后置精修耗时 | 773.211945 秒 | 688.259999 秒 |
| 停止原因 | 候选额度耗尽 | 候选额度耗尽 |
| 禁止 / 欠重 / 双审计 | 0 / 0 / 通过 | 0 / 0 / 通过 |

旧欠清空提前约 132.74 小时，但并非所有旧欠都提前：96 单中 58 单提前、38 单变晚；其他 435 单为 229 提前、206 变晚。3 单由晚交变准交，6 单由准交变晚交。完整原单对照保留于 `diagnostics/critical_delivery_search_and_bridge_reclamation/compare_search_only_01/order_changes.json`，哈希见[机器摘要](stage2_comparison.json)。不只比较原晚交名单，不将本月统计恶化藏在总评分后。

新方案按既定九级更优，因为第 5 项旧欠清空优先改善；它不是全面更优，也没有证明最优。单机单次且本组附带薄观察，不当作正式性能验收或自动采用决定。按原授权继续第二项回收试验。

## 前置边界与计数

- 原请求完全相同；首轮初始/最终快照（方案、评价、轨迹、计数与缓存）逐值一致。
- 前 414 次接受逐值一致。后置入口均为 193540 次检查、72297 次完整评价、414 次接受、23 链和同一九级评分；日志入口去时间戳逐值一致。
- 第 415 次接受首先在检查位置上分歧：原 193633、新 193693；该次动作和评分仍相同，后续路径才继续分化。
- 进入后置前没有独立完整快照文件：其链序保持由同初始状态、相同前置代码与完整接受前缀互证，不将入口汇总日志冒充完整快照比较。新增函数仅由后置入口调用，前置源文件未改。
- 后置重点：104248 提案/检查、36733 次完整评价、186 接受；常规：102212 提案/检查、49034 次完整评价、226 接受。合计 206460 次检查、85767 次完整评价、412 接受，与阶段起止差闭合。本例桥接没有额外扣费，工具仍按实际差分计入，不假定每提案永远一费。

## 命令及验证

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/run_delivery_comparison.py \
  --prepared-request diagnostics/urgent_order_search/input_budget400000_search9999.json \
  --timing-source diagnostics/delivery_objective/timing_source.json \
  --start 2026-06-01T00:00:00+08:00 --virtual-speed 100 \
  --variant delivery-backlog-seconds --observe-search-opportunities \
  --output-dir diagnostics/critical_delivery_search_and_bridge_reclamation/search_only_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/compare_backlog_search_runs.py \
  --old diagnostics/critical_delivery_search_and_bridge_reclamation/baseline_400000_01 \
  --new diagnostics/critical_delivery_search_and_bridge_reclamation/search_only_01 \
  --output-dir diagnostics/critical_delivery_search_and_bridge_reclamation/compare_search_only_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -q -p no:cacheprovider tests/app/test_backlog_search_comparison.py
```

两条真实命令均退出 0；完整评价、日期及全部 531 原单重量独立重算，核心/应用审计均通过。22 项薄工具测试通过 / 0.79 秒，含观察前后真实小型求解逐值一致及计数闭合。共享旧残留问题仍只原八项缺失及汇总；精确导出同范围结果记入提交正文。本组结果在回收生产改动前独立冻结。
