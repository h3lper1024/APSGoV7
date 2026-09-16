# 阶段 7：统一串行全流程与完整对照

## 7.1 调用边界、独立审核与探针接线

实施前 `6b457b9`，macOS、Conda `aps_3.10.18`。本项没有新增生产算法：前序已接通公共层，本项核验公共服务、独立核心审核和应用审核的完整链路。公共服务小例把旧首轮布局/拆单准备、旧精修准备/对象覆盖/源枚举和旧索引构建替换为失败桩，仍产生公共候选并通过双审计；静态传递调用见证覆盖首轮、拆单及精修生产入口。拒绝不物化、资源/期锁/错误/容量/取消等由前序专项与本次累计共同验证，不把小例称为完整 GQGA4 质量验收。

`verify_unified_numeric_solver.py` 改挂两处公共消费别名；结构描述仅为旧提案摘要映射 7 个元数据，不展开链。正式编辑对象现在只在接受时产生，旧“每个拒绝也生成编辑对象”的物理计数不再适用；新增消费流，额度、接受、阶段及正式结果仍严格核对。不能为探针重建拒绝对象，也不能用物理流改变为由放宽公开业务结果比较。

`verify_numeric_serial_batch.py` 改挂公共消费，不再挂已退出生产的旧覆盖函数。每个实际完整消费候选用相同权威视图独立重算，所有数值结果数组逐项比较；采样为零明确失败。旧平铺批量预热移除，本项仍是有界公共逐条预准备，不声称原生批量/并行已完成。

### 真实单条与 8 条窗口

实际命令（仓库根目录）：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_numeric_serial_batch.py \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage71_batch_window_01 \
  --window-checks 20000
```

退出 0。两侧从正常前缀 134226 次、方案 `fdda717a044c91cb78e88587d1a8339682c6d47d29ea756ac9efcce11ebbc369` 进入，追加 20000 次，实际完整消费各 5560 次、接受各 18 次。逐项汇总、顺序轨迹、资源序号、最终完整公开结果除实测时间外一致，准备请求/交期报告字节相同，双审计通过；退出方案 `6205c1544159c9299dae61c79fbb920ef855c20478fee59e70f6a08c9cb65477`。批次 8 实际协调 1237 次、预计算 5601 次、消费 5560 次、作废 41 次；单条没有协调批次。两侧最大私有内存/冷热及额外重算不同，窗口时间只作诊断，不能作为提速对比。

### 必要累计验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/architecture tests/api tests/app tests/core
```

共享累计 **4078 项通过，215.43 秒，退出 0**。开发时新测试误用不存在的应用审核字段，聚焦检查拦截后改为实际 `audit_report.passed`；没有改生产或断言门槛。精确暂存树用同命令复验，实际树、目录、数量和耗时记入本项提交正文，不在本文件自引用当前提交。

六项保护与原请求摘要保持；共享残留仍为已登记 8 项缺失及汇总，退出 1，不记通过；精确导出残留须退出 0。下一项 7.2 为完整同标准 40 万次结果及独立正式耗时组；历史交期退步仍未关闭。
