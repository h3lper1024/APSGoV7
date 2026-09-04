# 步骤 5：集中链间宽差精修接入

实施前提交 `12dc232`；macOS / Darwin arm64 / zsh，Conda `apsgo_v6_3.10.18`。当前提交导出 `/tmp/apsgo-v7-width-step5-start.vvEaTz` 的原残留检查 `clean_export` 通过。

## 接线范围

原构造、固定修复、受控订单拆分及其至多一次旧搜索重放保持原调用顺序。之后、核心审计之前，仅对启用宽差目标且完整评价零违规的方案进入一次集中精修；先前存在真实停止时跳过，不计为自然扫描完成。

四类动作依次为单节点、连续段、切链联合安置和纯整链调序；全部复用共享分批扫描器，拒绝后续扫、接受后重新枚举。整链调序只提取原同期间位置和移位序列两个私有函数，原阶段不改枚举、检查位置或计数；新阶段同时启用纯调序与宽差候选保护。

同一方案状态、搜索上下文、连接缓存、虚拟工厂和运行预算贯穿全流程。阶段入口只清除自然完成标记，不改候选上限、计数、截止时间、正式编号或拆单计数。耗时通过既有映射新增 `width_optimization`；异常由原核心失败路径报告，最终审计和发布契约不变。

## 验证

共享树与最终暂存树干净导出均运行：

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_width_optimization_integration.py tests/core/test_solver_orchestration.py tests/core/search/test_chain_order.py tests/core/search/test_chain_order_integration.py tests/core/search/test_local_search_orchestration.py tests/core/search/test_post_split_single_replay.py tests/core/search/test_width_optimization_baseline.py tests/core/search/test_width_optimization_guard.py tests/core/search/test_width_optimization_scan.py tests/core/search/test_width_optimization_nodes.py tests/core/search/test_width_optimization_blocks.py tests/core/search/test_width_optimization_cut.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
```

旧链序提取前后各 29 项测试通过（各 0.46 秒）；1092 种期标签布局、9294 次移位与原片段位置和顺序完全一致。格式检查首次退出 1，仅按格式要求合并条件；再验 Ruff 格式与代码检查通过，未改语义。

主代理首次聚焦命令误用了不存在的 `test_chain_order_neighborhood.py`，退出 4、没有执行测试；改用实际受跟踪的 `test_chain_order.py` 后，架构、旧链序与接线聚焦 134 项通过（0.95 秒）。不将命令错误说成生产缺陷，也未调整旧断言。

真实阶段入口小样本：700→100 mm，78 次候选检查、3 次完整评价/接受，自然结束，接受两次单节点交换和一次单节点移动。新接线测试另外验证四类轮转、纯整链双重保护、规则关闭或诊断模式、残余违规跳过、真实停止保留、旧拆单重放与异常边界。

新增阶段专项 35 项、既有核心接线文件新增 16 项；原接线文件最终 37 项通过（0.53 秒），阶段加扫描及原整链调序 86 项通过（0.77 秒）。等宽样本四类共 91 个真实描述逐项扣次，无完整评价，恰好 91 次额度仍可自然穷尽；片段专属样本第 35 次接受、最终 100 次额度停止，如实保留截断原因。

专项补测出现过 1 个错误预期：减少总额度会改变批次份额，不能沿用原额度下第 6 次接受的位置。改用三条单节点链的真实首个交换，1 次额度内 800→700 mm、1 次评价/接受，随后不能免费续扫；不改生产、设计、预算公式或既有断言。

最终同范围测试数量、退出码、耗时、暂存树和导出路径由本项提交正文记录；本步骤不代表完整 GQGA4 的新收益、确定性或 20 对性能验收。
