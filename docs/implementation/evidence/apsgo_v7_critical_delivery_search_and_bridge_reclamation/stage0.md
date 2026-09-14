# 阶段 0：40 万次基线与独立诊断见证

## 范围与身份

- 日期：2026-09-14；实施前 `5b0c662`，macOS，Conda `aps_3.10.18`。
- 本轮授权将专项上限由 1000000 改为 400000。复用 `diagnostics/urgent_order_search/input_budget400000_search9999.json`；已通过公共载体逐值比较，只有 `policy.candidate_check_limit` 不同，原输入均未修改。
- 搜索仍 9999 秒、收尾 10 秒；种子、秒级九项评分、规则、工时与起排时间不变，正式 YAML/SQLite 不改。
- 新原代码完整基线：`diagnostics/critical_delivery_search_and_bridge_reclamation/baseline_400000_01/`；使用 `5b0c662` 生产代码。本阶段不改生产源码。
- 固定见证仍使用历史百万次最终方案，不能冒充 40 万次完整求解结果；见证互相独立，不累计提交。

## 实际命令

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/run_delivery_comparison.py \
  --prepared-request diagnostics/urgent_order_search/input_budget400000_search9999.json \
  --timing-source diagnostics/delivery_objective/timing_source.json \
  --start 2026-06-01T00:00:00+08:00 --virtual-speed 100 \
  --variant delivery-backlog-seconds \
  --output-dir diagnostics/critical_delivery_search_and_bridge_reclamation/baseline_400000_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/probe_critical_delivery_witnesses.py \
  --run diagnostics/backlog_priority_search/stage5_2_second_precision_1000000_01 \
  --output-dir diagnostics/critical_delivery_search_and_bridge_reclamation/stage0_witnesses_02

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -q -p no:cacheprovider tests/app/test_critical_delivery_witnesses.py
```

## 验证记录

| 检查 | 实际结果 |
|---|---|
| 见证复现 | 退出 0；8.14 吨移动被原生产入口接受且核心审计通过，未新增虚拟材 |
| 普通连接材独立删除 | 共 45 次，各自改善且核心审计通过 14 次；原精修入口全部拒绝，原方案未改 |
| 分隔材反例 | `virtual-000025` 不属于普通桥接，删除后核心审计不通过，精修入口拒绝 |
| 原搜索机会 | 纯枚举 400000 个提案，第 110116 个出现有效移动；未构造或评价扫描中的候选 |
| 薄工具检查 | 3 项通过 / 0.75 秒；类型过滤、既有精确序列化及禁止覆盖、错误方案不猜坐标 |
| 原代码同预算完整基线 | 退出 0；400000 次检查、178216 次完整评价、880 次接受，候选额度停止；零禁止/零欠重、来源守恒与双审计通过 |
| 共享残留 | 退出 1，仅历史 8 项旧 V6 残留缺失及汇总，无新增异常 |

首次见证运行 `stage0_witnesses_01` 因诊断写出未调用既有载体转换而退出 1；失败目录保留。复用 `_json_values` 修正后在新目录成功，不修改算法。最终精确暂存树同范围检查记录在提交正文；不将本阶段小型检查称为累计回归或性能验收。

详细身份与独立删除名单见 [见证摘要](stage0_witnesses.json)。14 次单独成功不代表能一起删除；所有见证只核验核心审计，不是完整应用发布。

## 同预算基线结果

完整结果独立回读重算通过，见[基线机器摘要](stage0_baseline_400000.json)。25 条链、虚拟 580 吨、宽差 15210；旧欠全部完成时间 `2026-06-22T01:11:45.225439+08:00`，本月晚交 12 单 / 653.90 吨。公共入口经过时间 1181.760883 秒、进程 CPU 1177.198096 秒，后置精修 773.211945 秒。基线运行期间并行完成了小型见证核验，不是隔离性能测试；不宣称正式性能门通过。

最终工具版本在 `stage0_witnesses_03` 再次复现相同 14 次独立删除和第 110116 个移动提案。阶段 0 已完成，下一项为阶段 1：只改后置候选调度，回收尚不接线。
