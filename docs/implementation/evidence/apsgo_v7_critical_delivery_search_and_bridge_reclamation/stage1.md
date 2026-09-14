# 阶段 1：关键候选调度与接受后续访

实施前 `375bc3e`；macOS / Conda `aps_3.10.18`，只修改秒级旧欠试验的后置候选调度。原九级评价、前置流程、正式配置及虚拟材保留检查不变。

## 实现及验证

- 一次当前方案计时，按拖尾旧欠、整链后移潜力、本月已晚交三组交替去重；同分用输入序，多片段最后位置先试。后移潜力仅排序，剩余真实节点及拆片期锁检查保留。
- 两队列复用同一有向移动/无向交换归属。重点包含短动作，常规保留长片段、切链、整链及其他动作；不缓存百万级拒绝集合。
- 64 提案交替，4 提案/原单；桥接追加扣费仍共享原总预算。接受切换队列、重算时间与下标，仅保留稳定标识访问偏好，双队列无接受穷尽才自然结束。

集中命令（共享 96 项通过 / 0.96 秒，退出 0；精确导出同范围结果见 Git 提交正文）：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/search/test_critical_delivery_scan.py tests/core/test_urgent_order_search.py \
  tests/core/test_delivery_search_audit.py tests/core/search/test_width_optimization_scan.py \
  tests/core/test_backlog_priority_objective.py

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/probe_critical_delivery_witnesses.py \
  --run diagnostics/backlog_priority_search/stage5_2_second_precision_1000000_01 \
  --output-dir diagnostics/critical_delivery_search_and_bridge_reclamation/stage1_witnesses_01
```

测试覆盖有限方案两队列并集/不重叠、对称交换、长片段、多片段原单、确定性、拆片期锁、接受后新位置、取消/额度/自然停止及空重点。开发中新增夹具的不可变缓存身份、分片重量精度与谱系字段错误已修正；未通过放松生产校验或删断言处理。

## 固定机会见证

`stage1_witnesses_01/summary.json` 保留源代码摘要、原产物哈希和枚举明细。纯枚举上限 400000，8.14 吨有效移动第 **30409** 个出现，原来 **110116**；独立执行该动作仍接受且核心审计通过。45 个普通桥单独删除仍有 14 个独立评分/审计有效，但旧入口全部拒绝；拆单分隔材反例仍审计失败。本阶段没有接入回收。

这是同一冻结状态的机会对照，不是全量求解结果或应用发布验证。下一步从阶段 1 提交执行 40 万次完整公共入口，冻结后才进入阶段 3。
