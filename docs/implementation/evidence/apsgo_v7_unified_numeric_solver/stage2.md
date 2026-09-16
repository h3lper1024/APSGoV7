# 阶段 2：连接修复与私有资源

## 前置修复：非自适应温区下的无单材解

实施前 `473596c`。阶段 2.1 小例发现原 `choose_virtual_bridge()` 非静态分支只在找到可用单材后才赋值 `best`；没有单材时进入双材分支前抛出 `UnboundLocalError`。已在阶段 0 独立冻结源码 `/tmp/apsgo-unified-stage0-V92qIO` 复现，退出 1，不是新数值实现引入。

见证为真实端点宽度 1000/400、温区 700～710/800～810，虚拟原型宽度 800/600，关闭虚拟温度自适应。原宽度约束下一个原型不能连接，两个原型可以；不是修改温度或宽度配置来放行。

用户已明确确认“允许修复，并继续推进”。仅在该分支扫描前初始化 `best = None`；候选枚举、严格平滑度择优、参数和规则保持。最多一材时返回无解，最多两材时选择原型 0、1；虚拟材温区仍按原端点派生为 700～810。当前冻结 GQGA4 使用自适应温区，不进入本故障分支。

必要验证（共享树和独立精确树同范围）：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_bridge_nonadaptive.py tests/core/test_numeric_search.py
```

实际数量、耗时、退出码和精确树写入本项 Git 提交正文，不在文档中自引用当前提交。只暂存这一行生产修复、独立回归和三份记录；正在开发的 2.1 公共实现不包含在本修复提交中。未执行完整求解、不宣称阶段 2 完成或提速；原配置/SQLite、用户文件及历史产物不变。
