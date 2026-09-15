# 阶段 2：数值规则与全量评价执行记录

## 1. 子项 2.1：规则参数编译与数值判定

- 实施前 `c930fd3`。新增私有规则内核 `core/_numeric_rules.py`，把现有规则对象一次编译为任务绑定的整数参数；搜索侧规则判断只读取 `NumericTask`、`NumericPlan` 和编译结果，不在每次判断中查询参数字典或恢复领域对象。
- 编译器显式覆盖当前注册表的全部 20 类具体规则；冻结 GQGA4 请求实际启用 17 条规则，全部编译，零静默跳过。任务、规则集、编译程序和方案分别携带身份，跨任务组合明确拒绝。
- 已实现边级、链级、静态方案级与受控拆单资格判定。覆盖宽度、厚度、温区、软硬材、连续段、链重、逆宽、跨连续虚拟材真实端点、期序、虚拟比例、链间宽差、填充和拆单资格。交期规则参数已编译，但生产时钟及九级评分留在 2.2，不能把 2.1 称为完整评价完成。
- 所有边界直接按整数比较，不引入隐式宽限；相对厚度使用交叉乘法，厚度区间保持配置原序首命中。欠重违规保留为允许偏差，超重和其他禁用项保留为禁止违规。动态虚拟材、拆片和已接受方案的全量验证依赖阶段 3 的候选物化，本项不伪造动态运行结果。

## 2. 真实输入静态扫描

冻结请求经现有加载和归一化后构建数值任务与规则程序。为只检查全部规则路径而不冒充求解质量，按来源期和原始行序建立 531 条单节点链，未放入原型、未拆单、未搜索。

| 项目 | 实际结果 |
|---|---|
| 数值任务身份 | `9ad4247f71f130a8746cedd33389f98ee9419453227161bf02491106340bf8de` |
| 数值规则程序身份 | `3a1d92c4a648adf0adcf6d452e214a46b61256c31b8d98fbd51db08f52c42276` |
| 编译结果 | 17 条启用规则，531 条单节点链 |
| 判定输出 | 533 条规则违规记录、5314 条规则指标记录 |
| 判定耗时 | 0.026837 秒；仅静态扫描，不是候选搜索或正式性能结果 |

首次内联扫描命令遗漏 `PYTHONPATH=src`，在导入阶段以 `ModuleNotFoundError: apsgo_scheduler` 退出，未进入业务计算；补齐项目包路径后得到上表结果。

## 3. 集中验证

共享树命令：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/rules \
  tests/core/test_numeric_units.py \
  tests/core/test_numeric_state.py \
  tests/core/test_numeric_rules.py \
  tests/app/test_gqga4_rule_set_mapping.py \
  tests/architecture/test_package_dependencies.py \
  tests/architecture/test_clean_room_boundaries.py
```

- 共享树最终复验：1174 项通过，1.50 秒，退出 0。
- Ruff：本项五个 Python 文件检查通过；自动修正 1 处导入顺序。
- 首次精确导出发现测试错误依赖未跟踪的 `diagnostics` 请求，因文件缺失为 1173 项通过、1 项失败；测试随后改为仓内自包含的全部 20 类编译样例，真实 531 单只保留为独立扫描证据。最终精确暂存树导出、相同测试及残留检查记录在本项提交正文。
- 正式配置 SHA-256 仍为 `04cb4998cd3d8100a4f7f2ff9c62e10111ced4ae8f827df841507fa535c2f0c0`；SQLite SHA-256 仍为 `72a60e32fea0d2d55f883a8b743175aa5e8fd94e779388db6ae8c8c26ec2bd6a`。

## 4. 当前边界与下一项

子项 2.1 完成后进入 2.2“全量数值评价、生产时钟和九级评分”。生产求解仍未接入新内核；本记录不代表完整 GQGA4 排程、质量门槛、一分钟目标、Windows 发布包或正式配置迁移已经通过。
