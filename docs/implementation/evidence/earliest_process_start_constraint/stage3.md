# 阶段 3：提前开工搜索修复

- 实施前 `ffcf170`；Conda `aps_3.10.18`。
- 精修入口只允许“仅有提前开工”的内部方案继续；候选设置中相应移除最早开工的全拒绝，其他规则仍按原设置拒绝。首轮和拆单共用的消费/正式提交检查各非时间规则禁止数不能增加，避免用其他硬违规换取提前数量改善。
- 利用当前评价已有明细形成数值重点标记，按提前量降序、实际位置和来源索引排序。位置唯一确定来源，末位身份不改变排序。按来源唯一归属；链内原有“已就绪节点向前移动”也会推后跨过的未就绪节点，将该动作放入重点通道，不另外生成反向的重复动作。
- 仍为原六类动作、64 步轮转、共享额度、严格完整评分和首改善失效；未增加等待、虚拟用途或专属求解流程。

## 实际验证

```text
tests/core/test_earliest_process_start_search.py
tests/core/test_numeric_refinement_scan.py
tests/core/test_numeric_refinement_common.py
tests/core/test_numeric_candidate_publication.py
tests/core/test_numeric_split_common.py
tests/core/test_numeric_search.py
```

- 命令：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider` 加上述范围。
- 初次新修复小例失败：入口放行后，候选策略仍含全部禁止类型；已准确修正该处，不扩大其他放行范围。
- 开发诊断暂用 `NUMBA_DISABLE_JIT=1` 跑新小例快速定位；其结果不当编译验收。最终**实际编译模式 157 项通过，241.58 秒，退出 0**。
- 新例验证：逐条/批量均能经过仍有提前的有效改善候选修到零并通过独立审核；产生逆宽违规的提前改善拒绝；其他硬问题仍不进精修；21 个链内移动均只归属一个通道；零预算不等待；停用无新重点标记。
- 旧相关测试继续对照生成顺序、资源、额度、取消、批次失效与拆单唯一重放。精确暂存树同范围结果见本阶段提交正文。
- 正式配置/数据库、用户服务保持；未开展真实数据质量或性能对标。
