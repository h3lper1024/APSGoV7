# 阶段 1：数值目录与规则适配

实施前提交 `b6598661fc2a333730f5d04527fbe849fcea1e13`。本机 macOS ARM64，全部Python运行使用Conda `aps_3.10.18`。本阶段只准备数据，不接入生产桥接，也没有Numba选桥内核。

## 1. 实施范围

- 新增私有 `_bridge_numeric.py`：目录绑定原问题、规则集、上下文对象；原型宽厚、缺失掩码、有序边规则及参数转换为只读NumPy数组。
- 每次锚点数据单独准备；桥接温区仍取原左右锚点的Decimal极值，不使用原型温区。原型及厚度区间按64行分块，分配和分块前后检查原预算，不扣候选额度。
- 未知/重复/派生边规则，派生工厂/缓存/规则集/节点/原型，不安全投影、字段重名和反转温区均返回不适用；不提前物化或抛出原遍历未必访问的业务异常。
- 所有数值桥接边都含生成型虚拟端点，所以软硬规则只提取原虚拟桥接开关，不新增牌号/材料类别编码。真实直接边与原Python桥接尚未变化。
- 项目精确声明NumPy2.2.6、Numba0.65.1、llvmlite0.47.0；架构只允许该私有模块导入NumPy/Numba，其他核心、服务和llvmlite/SciPy/数据库边界不放宽。

原 `virtual_material.py`、规则、缓存、预算、求解器、公开接口、发布脚本及所有基准不改。配置YAML与SQLite的SHA-256仍为计划第2.2节原值。

## 2. 验证

新增专项验证覆盖：四种规则的16种启停组合、顺序和厚度区间；0/1/27/65/129原型；只读/连续数组；缺失与浮点下溢零的区分；精确温区先后与正负零；普通、实际过渡、生成虚拟及拆片；非法未选原型回退；任务绑定隔离；准备中取消/到时且无候选扣次。真实正式GQGA4适配与原桥接入口未接线单独验证。

独立只读复核核对了四种具体规则、加载器和原物化/缓存路径，未发现语义差异。有效规则参数原本就要求Decimal，未放宽成任意int/float。专项、聚焦、共享累计与精确暂存树验证的实际数量/耗时见本阶段提交正文。

实际执行命令如下；导出路径为新临时目录，由提交记录绑定，不覆盖旧证据。

```bash
cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/core/search/test_virtual_bridge_numeric.py tests/core/search/test_virtual_material_factory.py tests/architecture -q
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service tests/release -q
/Users/miles/anaconda3/bin/ruff check src/apsgo_scheduler/core/_bridge_numeric.py tests/core/search/test_virtual_bridge_numeric.py tests/architecture/test_clean_room_boundaries.py
git diff --check
```

精确白名单暂存后，`git write-tree`、`git archive`到新临时目录，再运行原 `tools/check_workspace_residuals.py --verify` 与同一累计范围。普通V7目录不重建旧V6残留。阶段0实跑基线保留，不把尚未接线的数据准备称为性能改善、完整新算法验收或Windows通过。

独立提交后继续阶段2：严格浮点判边与有界单桥/双桥选择。
