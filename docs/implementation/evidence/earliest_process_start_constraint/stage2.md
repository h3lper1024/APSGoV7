# 阶段 2：硬规则、统一评价与独立审核

- 实施前 `bbd14cf`；Conda `aps_3.10.18`，macOS。
- 注册方案级空参数 `EarliestProcessStartRule` / `earliest_process_start`，追加内部规则和原因编号，不改变九项评分。
- 同一整数时钟在累加本单之前比较来源下界。真实过渡材和拆片来源一致；虚拟只计用时。违规只进入方案级，不缓存进链评价。严重度为提前秒数，内部保留百万分之一秒的整数刻度并检查溢出。
- 输入层拒绝缺失下界、缺失计时及不支持的旧执行模式；编译器再次保护完整原单数组。独立审核从原始输入重建数组，新字段自动参加既有全部原单列对比。
- 发现并同步整链调序快速预检：它原来只重算交期与宽差，现在从同一时钟重新计算提前违规，替换当前评分中的旧提前贡献；不在预检阶段漏掉修复候选。
- 公共违规定位真实节点及来源，报告给出每节点最早/实际开始/提前量和汇总。未启用或未提供下界的旧输出结构保持；没有等待或显示端补偿。

## 验证范围与执行记录

```text
tests/core/test_earliest_process_start.py
tests/core/test_earliest_process_start_rule.py
tests/core/test_numeric_rules.py
tests/core/test_numeric_evaluation.py
tests/core/test_numeric_incremental_evaluation.py
tests/core/test_numeric_boundary_audit.py
tests/app/test_input_normalizer.py
tests/app/test_rule_set_loader.py
tests/app/test_earliest_process_start_validation.py
```

- 环境及零候选编译预热方法沿用 `stage1.md`；原测试预算不变。
- 首组规则专项 15 项通过（28.64 秒）。扩展后集中首测 231 通过/2 失败（49.23 秒）：旧规则枚举测试进程提前导入了修改前断言；新批次样例第二种移动还触发原逆宽次数规则，不能将“最早开工修好”等同于“所有硬规则合格”。测试改为核对最早规则命中和完整评分逐项一致，保留实际其他违规。
- 新专项包括 9 个手算时钟例、1 毫秒、负下界、拆片继承、真实过渡材、批次/单条、整链预检/完整/复用一致、最终拒绝发布、输入缺失、身份篡改及溢出。
- 补充旧时钟及空参数输入验证 2 项通过（0.36 秒）；初次测试夹具误用空评分声明，已改为合法七项旧声明后核对前置拒绝。不是通过跳过新规则来兼容旧模式。
- 最终共享与精确暂存树结果、退出码见本阶段提交正文；不得用首次失败组冒充通过。
- 共享残留检查仍是既有 8 个旧 V6 残留缺失，未改清单；正式库和服务配置 SHA 与阶段 0 一致。没有启动或停止服务，没有全量 GQGA4 求解。

下一项：阶段 3 修复搜索入口及候选保护。阶段 2 的评价正确性不等于搜索已能修复真实数据。
