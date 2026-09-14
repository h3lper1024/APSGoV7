# 阶段 3：普通连接材回收、完整修复与编号保护

实施前 `82106e3`；阶段 2 完整结果已提交后才修改本阶段生产代码。macOS / Conda `aps_3.10.18`；不改评分、前置流程、规则声明、正式配置或旧产物。

## 实现边界

- 抽取拆单路径已有的普通桥接识别：生成型、`EDGE_BRIDGE`、无关联分区。拆单识别语义不变，不删除分隔材或填充材。
- 普通清理一段先整删、失败再逐个，懒枚举，不做删除子集组合；首批最多 64 提案，之后成为常规轮转中的第六类，其他五类保留。接受后优先变化链，其他链完整回绕，没有跨接受的拒绝缓存。
- 链内前移、节点移动/交换、片段移动/交换使用同一完整修复入口，先清理变动接口两侧普通桥接，再调用原桥接工厂；失败则试原保留版本。无清理差异不重复，两种实际版本单独扣费，取消后不回退执行。
- 完整接受校验精确删除集合、原节点用途和所属受影响链；保留节点逐值不变、新桥接端点物化和连续生成区间不变。已有完整评价、期序/期锁及严格九级比较保留；非秒级模式不能借删除声明放行。
- 删除不降低 `virtual_sequence`；现有接受日志在有回收时补删除身份/重量/上界。薄观察记录接受后的增删明细，重点/常规/清理计数分别闭合。
- 诊断快照新增现有生成上界，完整运行额外保留精修入口快照。旧快照无上界仅可只读复算/不新建桥接见证，诊断上下文把最大新桥接数置零；原请求不改，明确要求恢复生成时拒绝。不能把存活最大编号当作可恢复上界。
- 新旧快照比较只在一侧缺少上界元数据时排除该缺失项，并显式记录未比较上界；两侧都有时必须相等。方案、评价、轨迹、计数和缓存仍逐值比较，不因兼容读取忽略业务差异。

## 集中验证

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/search/test_bridge_reclamation.py tests/core/search/test_width_optimization_guard.py \
  tests/core/search/test_width_optimization_nodes.py tests/core/search/test_width_optimization_blocks.py \
  tests/core/search/test_virtual_numbering.py tests/core/search/test_width_optimization_scan.py \
  tests/core/search/test_critical_delivery_scan.py tests/core/test_urgent_order_search.py \
  tests/core/test_delivery_search_audit.py tests/core/test_backlog_priority_objective.py \
  tests/core/audit/test_final_audit_without_cache.py tests/app/test_result_contract_audit.py \
  tests/app/test_backlog_search_comparison.py tests/app/test_solver_profile_tool.py \
  tests/app/test_critical_delivery_witnesses.py tests/app/test_backlog_priority_preparation.py \
  tests/app/test_delivery_second_preparation.py

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  tools/probe_critical_delivery_witnesses.py \
  --run diagnostics/backlog_priority_search/stage5_2_second_precision_1000000_01 \
  --output-dir diagnostics/critical_delivery_search_and_bridge_reclamation/stage3_witnesses_01 \
  --expect-reclamation
```

共享 **490 项通过 / 4.21 秒**；精确导出同范围结果见 Git 提交正文。覆盖旧模式、五类真实动作的完整接受与核心审计、首尾/中间桥接、精确身份/错误链/真实节点和用途拒绝、两块各可删但合删欠重、链级连续规则拒绝后原保留版本实际接受、取消/预算、未变链重新回访、删除最高编号再生成、快照上界及旧格式拒绝恢复。完整规则/核心与应用审计的原有回归一并运行，不只依赖相邻连接缓存。

固定见证退出 0：原 45 个普通桥中 14 个独立改善和核心审计通过的删除，均由正式完整候选入口接受；拆单分隔材反例仍拒绝。有效移动仍接受且核心审计通过；组合调度纯枚举位次为 30396。完整产物保留于独立目录，[机器见证](stage3_witnesses.json)记录源码及原输入哈希。此处不是把 14 个全部同时删除，也不是完整应用排程结果。

本次未修改核心/应用审计或放宽标准。尚未进行组合 GQGA4 完整求解；下一项阶段 4，同请求、40 万次运行并重复验证，再判断真实交期和材料收益。
