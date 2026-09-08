# 阶段3：唯一桥接入口接线

实施前 `1994ee3`。生产只修改 `_bridge_numeric.py` 和 `virtual_material.py`，没有改规则、预算、邻域、评价、API、YAML、SQLite或发布脚本。

## 实现与边界

原直接连接、有效虚拟数量上限及预算前缀保持；原Python虚拟搜索完整提取为私有方法。仅适用的内置规则进入数值分块，原型目录绑定本工厂的问题/规则/上下文，锚点数组每次重建。只创建最终胜出的一至两个节点；准备、扫描及物化前后停止都整体丢弃，不推进编号、不返回半个桥。

未知规则/对象及不安全数值回原路径。资格检查不轮询，因此旧自定义规则的17次查询/49次轮询、4命中/13未命中/13条目不变。单次锚点不适用不污染已准备目录；无限整数编号沿用原格式化边界，不能因不再预先物化而隐藏原异常。依赖、编译、索引等程序错误不作为业务回退吞掉。

日志复用原时间戳辅助，每工厂至多一次 `solver_bridge_numeric_start`，单桥/双桥各一次 `solver_bridge_numeric_ready`。ready只在实际成功扫描且存在nopython签名后输出；无逐候选日志或新增响应字段。

## 验证

- 数值专项及原虚拟工厂合计 **233项通过，5.46秒，退出0**；原精确自定义回退测试没有修改。
- 覆盖真实选桥及全部物化字段、目录复用和锚点隔离、错误传播、取消各阶段、日志失败隔离及无限整数编号边界。
- 新进程实际执行服务help、临时SQLite规则GET、直接连接和零上限调用，确认没有导入数值模块/Numba。
- 新进程从无签名开始真实编译；首块返回时受控时钟越过原截止点，立即停止、不重置预算、不发ready。这是预算接线测试，不是实际墙钟性能样本；真实编译耗时另见阶段2探针。
- 语法树比较实施前 `git show 1994ee3:src/apsgo_scheduler/core/virtual_material.py` 与当前源码：原bridge前缀完全相同，原虚拟搜索语句与提取后方法完全相同，其他已有方法完全相同。独立复核全部调用者、目录生命周期、预算及日志，无阻塞项。

```bash
cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/core/search/test_virtual_bridge_numeric.py tests/core/search/test_virtual_material_factory.py -q
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service tests/release -q
```

共享及精确暂存树累计、原残留clean_export、保护哈希、Ruff、diff检查按计划第6节执行；最终数量、耗时、退出码与树身份见本阶段提交正文。配置及SQLite仍使用计划冻结哈希。当前仅证接线正确，**不代表完整求解提速、质量门或Windows实包验收**；提交后持续进入阶段4完整对照。
