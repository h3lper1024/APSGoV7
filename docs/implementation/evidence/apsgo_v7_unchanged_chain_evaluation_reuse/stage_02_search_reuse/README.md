# 阶段 2：当前已接受方案评价复用

实施前 `be9fd7ce2ca9766df9eaa7afc113c2d62d47cac7`，macOS ARM64 / Conda `aps_3.10.18`。阶段 1 最终共享 3494 项通过（144.38 秒），同提交精确暂存树 3494 项通过（146.83 秒）且 `clean_export` 通过，作为本阶段开始基线。

## 1. 实施边界

生产仅两文件：

- `evaluation.py`：精确规则集及 20 个已核验具体类型集合，内部当前方案持有结构；未知规则或规则集子类走公共无缓存入口。
- `neighborhoods.py`：私有默认空字段，不接受构造注入；在原完整评价位置接入，绑定变化先清空，原提交成功后才提升候选条目。

身份检查全部使用对象引用，不计算新哈希、不做深比较。条目仅对应当前方案，候选拒绝时不保留任何新条目；有效旧条目不因候选失败而改变。全部可失败的规则、映射、评价及轨迹准备仍在原提交前；其后只有引用赋值及原有日志。原候选次数、预算探测、编号、拆单授权、枚举和采纳比较不改。

无缓存与复用共用一份原始贡献组装和评分公式。资源与方案规则每次重算；初始方案、正反链快速检查及最终审计始终无缓存。沒有修改公开模型、HTTP、YAML、数据库、依赖或其他生产模块。

## 2. 验证覆盖

| 检查 | 实际保护 |
|---|---|
| 同对象及变化 | 同链与调序命中；克隆、宽度、重量、属性、角色、期和节点序变化重新评价；全局仍每次调用 |
| 原始贡献 | SUM/MAX/COUNT 保留 `26/11/4`，重复违规及节点贡献按序保留；完整评价及指纹相同 |
| 数值与规则 | 直接复用已有实际规则严重度、逐项两位欠重、虚拟重量金样；完整 GQGA4 多虚拟端点、空表面打断保持 |
| 扩展兼容 | 未知启用规则、规则集子类及未核验内部派生类型全量回落；停用未知项不妨碍复用 |
| 生命周期 | 冷拒绝不暖缓存，接受才提升；连续 32 次拒绝不增长，下一次接受删除旧链并保留未变条目 |
| 绑定及故障 | state、plan、规则集、上下文换对象清空；冷/暖取消、时限、评价异常、轨迹异常、提交失败不发布候选 |
| 独立审计 | 禁止进入复用入口或构造条目仍可审计；原污染指标、质量和摘要的拒绝测试保留 |
| 原故障探针 | 七个现有测试文件迁到真实候选入口，保留原停止与未提交断言，没有删除失效测试 |

新增 28 项。命令均使用计划第 6 节的解释器、`PYTHONDONTWRITEBYTECODE=1`、`pytest -p no:cacheprovider`：

1. `tests/core/test_plan_evaluation.py tests/core/test_reference_numeric_projection.py tests/core/audit/test_final_audit_without_cache.py`：154 项通过，0.50 秒，退出 0。
2. `tests/core/search/test_complete_candidate_lifecycle.py tests/core/search/test_single_real_node_relocation.py tests/core/search/test_chain_order.py tests/core/test_solver_process_logging.py tests/core/search/test_controlled_order_split.py tests/core/search/test_virtual_weight_fill.py tests/core/search/test_width_optimization_guard.py`：269 项通过，0.86 秒，退出 0。
3. 独立只读复核两生产文件，无阻断问题；原生 Ruff 与差异检查通过。

共享和精确暂存树累计按计划第 6 节运行，最终结果见提交正文。首次聚焦执行时实现已存在，不声称先红后绿。

## 3. 后续

阶段 3 将以最新冻结请求验证完整方案、全部有序评价、轨迹、计数、资源与双审计；五对完整运行分别报告首轮和完整观测耗时。这里只说明实现及测试边界，未作真实提速结论，不替代 Windows 实包或历史正式性能门。
