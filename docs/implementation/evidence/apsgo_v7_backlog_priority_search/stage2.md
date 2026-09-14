# 阶段 2：轮转正确性通过，样例机会检查未通过

2026-09-14，实施前 `3062099`，macOS / Conda `aps_3.10.18`。**阶段 2 尚未收口；暂停于目标位置枚举的设计调整，不进入阶段 3 完整复测，也未实施阶段 4 新评分。**此提交是可追溯试验检查点，不建议部署或采用。

## 1. 已实现与验证

仅交期启用且存在旧欠时，五类动作采用 64 提案轮转、每原单 4 提案。拒绝后继续原迭代器；接受后全部旧位置/穷尽标记失效，转下一动作类，并用原单/链/节点身份重新定位访问起点。访问到尾后回绕，不把旧拒绝结果当成新方案结论。旧九级、欠重两项分别不增加、候选总额度、桥接计费及无旧欠/无交期路径保持。

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider \
  tests/core/test_urgent_order_search.py tests/core/test_delivery_search_audit.py \
  tests/core/search/test_width_optimization_scan.py -q

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider \
  tests/core/search tests/core/test_urgent_order_search.py tests/core/test_delivery_search_audit.py \
  tests/core/test_delivery_objective.py tests/core/test_delivery_timing.py tests/app/test_delivery_preparation.py -q
```

专项退出 0，54 项 / 0.73 秒；扩展相关回归退出 0，867 项 / 26.94 秒。覆盖原动作集合、不重复交换、原单及拆片排序、64 步轮转、拒绝续访、真实接受后重建、链消失及位置重排、到尾回绕、取消、桥接追加计费、欠重保护与无缓存审计。不是项目全量回归或 GQGA4 全流程质量验收。最终精确暂存树按扩展相关范围复验，结果随提交正文记录。

## 2. 自动搜索机会诊断

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/replay_backlog_search_witnesses.py \
  --baseline-manifest docs/implementation/evidence/apsgo_v7_urgent_order_search/restored_search9999_comparison.json \
  --output-dir diagnostics/backlog_priority_search/stage2_opportunities_03 --opportunities
```

退出 0；3.447700 秒 / CPU 3.421436 秒。这里的退出 0 表示诊断执行成功，**不表示搜索机会门通过**。工具只观察通用枚举，目标订单 ID 仅用于记录命中，不影响生产排序；不构造/接受候选，不产生新排程。先枚举节点动作完整描述以定位两例，再用独立原状态观察五类动作的 400000 次共同额度（回调均返回拒绝，不执行候选业务检查或桥接）。这个受控机会观察不能冒充实际完整搜索的接受路径。

| 原订单 | 原单访问位次：旧 → 新 | 成功动作在节点类中的提案位次：旧 → 新 |
|---|---:|---:|
| `0002002009-000020` | 108 → 2 | 120667 → **430854** |
| `0030117283-000020` | 148 → 5 | 165345 → **406737** |

五类共同轮转的 400000 个提案内，两条具体成功动作均未出现；停止原因为 `candidate_limit_reached`，原方案指纹未变。动作分配如下：

| 动作 | 提案数 |
|---|---:|
| 链内节点前移 | 7413 |
| 跨链节点移动 / 交换 | 88916 / 41836 |
| 连续片段移动 / 交换 | 65491 / 65236 |
| 切链联合安置 | 130688 |
| 整链移位 | 420 |
| 合计 | 400000 |

原始证据：`diagnostics/backlog_priority_search/stage2_opportunities_03/opportunities.json`，SHA-256 `88e6ee8594ad91d8ff23d8f01c3b55cc7c85ad89a9eb4c0538d11dbdbeb73d6c`。包含实际源码哈希、原单次序、命中位置、额度、耗时与只读连接诊断。此前 `_01` / `_02` 的试验记录保留；两次均得到相同提案次序和额度分布，第三次增加连接位置证据。

## 3. 原因与待确认调整

新排序解决了“旧欠原单轮不到”的问题，但固定小批平均轮换加上各目标链仍从头逐位置扫描，延迟了后部有效插入。只按原单公平分配不等于有效位置更早出现。这是当前设计的不足，不应靠扩大额度或提前换评分掩盖。

调用现有连接缓存 `RuleEdgeDecisionCache.allows()` 做只读诊断：目标链 `initial-000003` 共 40 个插入位置；8.14 吨原单仅在下标 37、38、39 可两侧直接连接，已知成功位置 38 是其中第 2 个；3.44 吨原单仅在 23、24、25、26 可两侧直接连接，已知成功位置 23 是其中第 1 个。位置编号从 0 开始。直接连接只代表相邻边合规，不代表链级、资源或完整评分一定通过。

建议将设计第 4.2 节“目标链内按原位置顺序”修订为“**优先直接可连接的插入位置，其他位置仍保留随后尝试**”，复用已有缓存、最终仍完整评价和审计；先在节点动作验证，不增加新规则、数据库配置或总候选额度。预排序的 CPU 成本和是否显著前移两条机会须实测，不提前宣称收益。**此调整尚未获确认、尚未实现**；不自行把原 4 / 64 改成新参数，也不更改已确认的新九级业务优先级。

共享残留仍为既知 8 个 V6 历史文件缺失；旧五产物和输入保护哈希均通过，SQLite / YAML 未变。未执行完整新排程、阶段 3～6、正式启用、Windows、部署、合并或推送。
