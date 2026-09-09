# 阶段 1：共用评价组装

实施前提交 `1bc5bf322f8cdf34c20d947af0bacb36d954c818`；macOS ARM64，Conda `aps_3.10.18`。开始前基线即阶段 0 已通过残留检查和 3485 项累计的精确导出树。生产仅 `core/evaluation.py`，未改搜索入口。

## 修改与正确性

公开 `evaluate_plan(plan, rule_set, context)` 保持三参数、同返回值、每次完全重算。一份私有 `_evaluate_plan()` 共用原聚合函数；`previous_entries=None` 不构造任何条目，空映射捕获冷候选。条目保存原链强引用、完整链原始贡献、逐节点有序贡献及链摘要。不存在第二套评分公式。

复用按链对象相同判定；资源先重算，然后全部链贡献、全部节点贡献、方案贡献。节点生成器惰性消费，不把节点计算提前到链评价之间。即使共用函数已具备内部复用能力，此阶段搜索没有调用它的捕获路径，不能声称已减少搜索计算。

新增 9 项用例：

- 两链宽度增量 `3,5,7,11` 的 SUM/MAXIMUM/COUNT 为 `26/11/4`，不能误用链汇总 `8,18` 得到 `26/18/2`；覆盖零违规和同规则四条违规，以及节点原始贡献顺序和完整评价指纹。
- 公共和私有无缓存入口禁止创建条目，保证初始与审计的原通路。
- 两种路径分别覆盖正常执行、第二链异常、首节点异常；验证资源、全部链、全部节点、方案的调用及中止顺序。

已独立审阅计划列明的全部 20 个内置具体类型及相关数值辅助，未发现读取时钟、随机、外部 I/O 或评价期修改输入；内部派生 `_VirtualBridgeWidthRule` 纳入下一阶段集合。方案及动作类型仅核验兼容性，不缓存全局或拆单资格评价。规则集子类可能覆写分派，因此下一阶段也要求精确 `ProcessRuleSet`，未知子类回落，公开入口仍兼容。

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider \
  tests/core/test_plan_evaluation.py tests/core/test_reference_numeric_projection.py \
  tests/core/rules/test_process_rule_set.py -q
/Users/miles/anaconda3/bin/ruff check src/apsgo_scheduler/core/evaluation.py tests/core/test_plan_evaluation.py
```

聚焦 130 项通过，0.17 秒，退出 0；Ruff、独立只读代码复核及差异检查通过。新增测试首次执行时实现已经存在，不伪称先红后绿。共享和精确暂存树累计均按计划第 6 节执行，最终结果见本阶段提交正文。

不修改规则、配置、数据库、依赖或算法决策，不在本阶段重跑性能样本。下一步阶段 2：持有当前已接受方案，唯一候选入口接线及完整生命周期验证。
