# 步骤 1：完整候选保护与分批扫描

实施前提交 `4a02466`。本步实现安全入口和私有扫描骨架，不包含真实结构枚举器，不接公共求解流程；不进行新算法 GQGA4 收益或性能验收。

## 实现边界

- `core/neighborhoods.py`：原唯一候选入口增加默认关闭的宽差保护；旧调用不变。当前和候选均无任何规则违规，前置评分完全相同且宽差严格减少，才可接受；旧节点完整保留。
- 新桥只允许在两个旧节点之间，按任务原型及同一对接口锚点经现有工厂重建后完整比较，沿用桥数量、正式序号、拆片所属期及原子发布保护。旧虚拟节点不重新物化。
- `core/width_optimization.py`：私有动作描述交替、分批扫描与接受后重启。只用已有预算、状态和候选入口；不增加配置、注册表、规则、随机接受或另一个评价器。
- 真实候选族在步骤 2～4 实现，进入条件和唯一正式接线在步骤 5 实施；这里的合成描述不能冒充实际搜索覆盖。

## 验证

使用 Conda `apsgo_v6_3.10.18`、Python 3.10.18，macOS / Darwin arm64 / zsh：

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_width_optimization_guard.py tests/core/search/test_width_optimization_scan.py tests/core/search/test_complete_candidate_lifecycle.py tests/core/search/test_chain_order.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
```

候选保护测试覆盖：默认兼容、输入类型、目标开关、全部旧节点保全、新桥身份/用途/字段/温区/接口/上限、序号、拆片期锁、规则与评分拒绝、取消和异常不发布部分方案。

扫描测试覆盖：每类分批机会、未接受时续扫、真实接受后重建、移动/交换交替、额度不足/零/恰好用满、既有额度不重置、自然完成与真实截断区别、取消与异常。扫描骨架初次 22 项通过（0.58 秒），独立复核 22 项通过（0.53 秒）。

共享树最终聚焦 156 项通过（0.81 秒），累计 2837 项通过（178.33 秒），退出均为 0。新增保护 73 项、扫描 22 项；首轮保护测试的旧虚拟移动例存在非法接边，修正测试布局后通过，生产规则未放宽。四个变动 Python 文件的 Ruff 检查及格式检查通过；扫描测试曾仅有格式差异，格式化后重新验证通过。

每次提交前对最终暂存树干净导出运行原残留检查、上述同范围测试和累计回归；最终数量、耗时、退出码、树身份及失败修复记录由本项提交正文保留。项目 Conda 没有 Ruff 模块，本步发现并复用本机已有 `/Users/miles/anaconda3/bin/ruff` 0.12.0，不安装依赖；代码风格检查与 Python 功能测试分别记录。
