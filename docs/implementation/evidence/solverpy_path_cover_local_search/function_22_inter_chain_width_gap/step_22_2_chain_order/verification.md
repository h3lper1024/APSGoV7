# 功能 22.2：生产链序与同期整链移位验证

- 日期：2026-09-04；macOS / Darwin arm64 / zsh。
- 实施前：`7445a339cb94c89c8b4d321a70a94fd9473a586a`。
- 依据：设计 v0.15 第 20.7、22.2 节，计划 v0.58 第 8.23.7 节。
- 范围：三个生产文件、两份新测试、实施计划、AGENTS 及本文，共八文件；正式配置、历史输入输出、质量及性能门槛不变。

## 已实现边界

共用纯函数处理规则启用判断、动态质量位置及稳定期序分组。初始路径内容断言保留；合并、移位或拆分引起合法改期后，先完成授权和期锁，再按任务期序稳定分组。评价器不排序；规则已启用但仅作诊断时只分组、不做移位优化。

纯移位只搬动同期间的整条链，不改链内容、身份、所属期、拆分和虚拟序号。每个实际非原位候选消费一次既有额度，完整评价一次，第一个改善即接受并重扫本阶段；取消或额度耗尽不补排。此前评分项必须完全相等，不把浮点加法换序的差异当收益；完整质量键仍要求严格改善。首轮三类之后及拆后唯一完整重放接入同一个新阶段，无额外外循环。

## 验证命令

共享工作区与最终暂存树的干净导出使用同一环境和以下集合：

```bash
# 专项
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_chain_order.py tests/core/search/test_chain_order_integration.py -q
# 聚焦（含原初始构造、候选授权及拆后重放）
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_inter_chain_width_gap.py tests/core/construction/test_initial_solution.py tests/core/search/test_chain_order.py tests/core/search/test_chain_order_integration.py tests/core/search/test_complete_candidate_lifecycle.py tests/core/search/test_local_search_orchestration.py tests/core/search/test_local_search_stop_semantics.py tests/core/search/test_post_split_single_replay.py -q
# 仓内累计
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
git diff --check
# 仅在干净导出执行
conda run --no-capture-output -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
```

| 范围 | 共享树 | 最终暂存树干净导出 |
|---|---|---|
| 新专项 | 45 通过，0.55 秒，退出 0 | 最终验证后记入提交正文 |
| 聚焦 | 266 通过，91.51 秒，退出 0 | 最终验证后记入提交正文 |
| 仓内累计 | 2691 通过，170.64 秒，退出 0 | 最终验证后记入提交正文 |

静态与格式检查覆盖 107 文件，文档 UTF-8/围栏/58 个本地链接检查通过；退出码均 0。独立只读代码复核无生产缺陷，旧搜索初始范围另有 491 项通过（131.92 秒），不替代上述最终集合。

最终导出由 `git write-tree` / `git archive` 固定；实际树、路径、退出码、数量和耗时记入本项提交正文，不自引用本提交身份。最终八文件须与被验证暂存树完全一致。

## 手算、独立复核与差异

- 三条同期间链宽度 1000、1500、1200：初值 800，检查 1 接受为 700，检查 3 接受为 500；最终共 9 次检查/9 次完整评价、2 次接受。三条等宽链实际枚举 6 次并全部拒绝，不额外去重改变预算含义。
- 三个有链计划期：1000 / [1600, 1200] / 2000，中期两链移位后总宽差 1800→1000，两个跨期边界都重算，链内容不变。一个或多个空计划期不制造边界。
- 初始构造覆盖非字典序六个期、空期、同组保序和混合来源期链；整链合并真实动作、节点移出改期、同期/未来拆单归还，以及返回链分组后不在全局末尾都通过授权检查。拆片期锁不能被分组绕过。
- 浮点严重度案例使用真实评价，候选在完整质量元组上看似改善，但前置项漂移，因此纯移位拒绝；直接入口额外验证布尔类型、启停/仅诊断、重复身份、原始及规范化内容不变。
- 取消、时间和候选额度截断、拆后有接受/无接受/停用、第三阶段或拆分提交后取消均检查不泄漏、不免费补跑。新阶段以质量声明定位目标，不假设固定七项或 GQGA4 身份。
- 早期五项集成测试夹具错误（把路径覆盖浮点耗时放入不支持浮点的指纹、尾节点缺少启用规则所需属性）只修正测试夹具；未修改生产逻辑或业务期望掩盖失败。最终结果与首个未解释差异记录于本项提交。

本项只有合成启用配置；已有冻结六级搜索回归不等于七级 GQGA4 质量/性能验收。下一项 22.3 审计与正式配置接线；完整复测及新边界报告在 22.4。
