# 表面等级可缺省修正

- 实施前：`29957820e5cbf9eb6fb716d0eb3706587610f641`；Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 用户确认：表面等级无需非空；缺失及空白允许输入，`FC → 空值 → FC` 是两段各 1 个。
- 仅现有 `HighSurfaceRunCountRule`、既有金样、本证据、设计/计划/AGENTS 六文件；功能 6 未提交文件不混入本修正。

具体规则不再声明表面等级必需，复用 `_optional_text_attribute()`：缺失/None 读取为空串，空白去除后不命中高表面集合并打断连续段，非文本非空值仍拒绝。材料角色、等级配置、数量上限、违规主体和严重度公式不改；配置样本与原始五输入不改。

原金样将“缺失或空白必须抛错”更新为用户新确认语义，保留整数/布尔类型拒绝，覆盖单空节点、两侧高表面节点、尾部空节点和规则集路由；不修改上限测试以消除失败。功能 6 另外验证输入阶段真实与实际过渡材都允许缺省，本项不宣称其已完成。

## 验证

| 项目 | 共享树 | 初验干净导出 |
|---|---|---|
| 高表面专项 | 40 项，0.10 秒 | 40 项，0.08 秒 |
| 已实施规则及加载器 | 967 项，1.08 秒 | 967 项，0.97 秒 |
| 本提交仓内累计 | 1267 项，1.57 秒 | 1267 项，1.40 秒 |
| 原参考对照 | 1554 案例一致，外层 1.865 秒 | 1554 案例一致，外层 1.946 秒 |

全部退出码 0；静态与格式、导出残留保护及编译通过。初验树 `a4a850dfd010362dc4062b2763cb056339c7a97d`，导出 `/tmp/apsgo-surface-initial-iDiady`。独立复核高表面及映射共 101 项通过（0.22 秒）。未出现本修复的失败测试。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_high_surface_run_count.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q --ignore=tests/app/test_input_normalizer.py --ignore=tests/core/test_problem_fingerprint.py
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -c 'import runpy,sys; sys.path.insert(0,"src"); sys.argv=["reference_differential.py", "/Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py"]; runpy.run_path("docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_01_high_surface_run_count/reference_differential.py", run_name="__main__")'
```

累计命令两处 `--ignore` 仅隔离功能 6 正在编写、未纳入本提交的两份新测试（干净导出不存在这两个文件），未跳过任何本提交或已跟踪测试。功能 6 提交必须恢复其完整累计回归。参考脚本哈希守卫通过，纯虚拟链 8 个不合法主体按原脚本既定范围排除；已比较的 1554 个正常案例无差异。缺值允许输入是相对原参考前置校验的已确认变化，不宣称整个输入合同与原参考完全等价。

补齐本文后最终暂存树重新导出，按同集合复测、共享保护检查通过并核对提交树身份后提交。原始输入、配置、门槛及历史对照未修改；未运行排程或性能验收。
