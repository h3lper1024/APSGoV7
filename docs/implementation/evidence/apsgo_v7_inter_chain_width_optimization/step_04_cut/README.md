# 步骤 4：切链与两段联合安置

实施前 `eaaf1db`，macOS / Darwin arm64 / zsh，Conda `apsgo_v6_3.10.18`。开始工作区干净，当前提交干净导出原残留检查 `clean_export` 通过。

在现有宽差模块中实现切点及两个最终位置的轻量描述，消费一次共享检查后才进行两段合法性筛选。前段保留旧 ID，后段用现有指纹函数及碰撞处理派生；两段分别规范化来源期，其他旧链保持相对顺序。原始跨期非法位置直接拒绝，不以排序投影制造重复候选。

不生成桥、不补重、不拆订单重量、不更新拆单计数、不新建状态或提交器；只有完整方案满足已有全部规则和宽差保护时才接受。切开后不能合规的片段直接拒绝，不允许保留欠重中间结果。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_width_optimization_cut.py tests/core/search/test_width_optimization_blocks.py tests/core/search/test_width_optimization_nodes.py tests/core/search/test_width_optimization_baseline.py tests/core/search/test_width_optimization_guard.py tests/core/search/test_width_optimization_scan.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
```

实际切链生成、同期间两段双方向、未来段归期、非法原始位置、旧虚拟/拆片保全、稳定身份及碰撞、预算和取消测试与最终双树聚焦/累计分别记录。最终导出先检查残留，再运行测试；Ruff 显式 `--no-cache`，不与初始保护竞态运行。实际数量、退出码、耗时和最终暂存树由本项提交正文保留。

新增切链专项 25 项；初次 19 项通过（0.64 秒），补入构造中到时和全部预存停止原因后，与节点、连续段合计 59 项通过（0.68 秒），没有测试失败。主代理与独立复核均调用真实切链枚举和扫描：700→400 mm、2→3 链，第 8 次检查接受，最终 32 次检查、1 次完整评价/接受并自然结束；全节点、虚拟与订单拆分计数保全。

独立只读复核另外验证：跨期原始槽位不靠分组制造重复候选、碰撞后稳定前置下划线、禁止桥和拆单隔离工厂调用、7 种停止原因保留以及规范化中取消/到时/异常无状态泄漏，命令均退出 0。架构加既有节点/连续段先行回归 99 项通过（0.98 秒）。这些观察仍是小样本，不是全流程收益证明。

公共求解流程接线仍在步骤 5；本项不是实际 GQGA4 收益或性能验收。
