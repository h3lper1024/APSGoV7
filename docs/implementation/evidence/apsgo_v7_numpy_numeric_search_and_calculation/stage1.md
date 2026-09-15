# 阶段 1：权威数值结构执行记录

## 1. 子项 1.1：单位、四舍五入、范围和身份

- 实施前 `0a7d3c4`；新增私有输入边界模块 `core/_numeric_units.py`，集中精确倍率选择/转换/还原、有理数约分、整数范围检查、四舍五入、毫秒/交期、稳定拆片工时分配及新单位身份。
- Python 大整数只在输入准备证明和比值舍入中使用，已检查的结果才能进入原生数组；本项没有把这些边界函数当作已完成的 Numba 内核。后续数值内核的中间乘积仍须在运算前证明。
- 所有量化点按四舍五入；缺值在数组阶段用有效位，不把物理空值转零。原工时直接从请求转换，不通过旧每吨工时反推。
- 新标准 `integer_physical_ms_half_up_v1` 和单位身份已定义，但未切换旧 `SolverPolicy`、请求/规则声明或正式求解入口；阶段 2～4 统一接线，不能提前宣称新请求已可运行。
- 新增 `tests/core/test_numeric_units.py`，包含精度上下文不变性、浮点/非法输入、巨大指数拒绝、正负中点、严重度六位、欠重两位、范围/前缀溢出、拆片零毫秒/守恒、毫秒交期边界及起排高精度小数。
- 实际命令：`PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/core/test_numeric_units.py tests/architecture/test_package_dependencies.py tests/architecture/test_clean_room_boundaries.py::test_production_tree_is_self_contained`。
- 共享结果：42 项通过，0.56 秒，退出 0；没有运行完整求解或累计回归。精确暂存树同范围验证记录在本项提交正文。第一阶段集中检查仍待 1.2 后执行。

## 2. 当前边界

下一项 1.2 建立数值字段列、原单/原型/时间与全部片段索引。正式运行仍为旧实现，不混合新旧数值；配置、SQLite、原诊断、前序脏文档和用户技术说明不变。
