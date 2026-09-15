# 第二阶段：缓存、精确增量与按需评价

## 2.1 资源与连续段复用

- 开始提交：`9ad3289`；macOS / Conda `aps_3.10.18`，不改配置、SQLite、依赖或原产物。
- 接入既有接受方案缓存：未变链复用资源分类，按候选新链序重组借用/虚拟/分区明细。重量使用原 `sum_weights` 的精确求和，可安全按链合并；不使用 28 位减旧加新。
- 高表面/连续虚拟数量、窄钢/同规格连续重量复用实际节点对象绑定的分类和完整连续段重量。变化后的分组和违规位置仍按新序构造；连接、逆宽基准和跨虚拟端点继续原检查。不是把所有状态规则改为常数时间合并。
- 缓存仅保留接受快照和当前候选事实，候选返回前切断历史引用；拒绝不提升。未知规则继续原完整评价，独立审计无缓存。
- 共享必要检查：`python -m pytest -q -p no:cacheprovider tests/core/test_incremental_evaluation.py tests/core/test_plan_evaluation.py tests/core/rules/test_high_surface_run_count.py tests/core/rules/test_same_spec_continuous_real_weight.py tests/core/rules/test_continuous_narrow_steel_weight.py tests/core/rules/test_consecutive_virtual_material.py`，退出 0，228 项 / 0.27 秒。均使用 `PYTHONDONTWRITEBYTECODE=1` 和指定 Conda 解释器。
- 暂存树导出同范围检查随提交正文记录。累计回归、交期边界与完整 40 万影子/性能重复留 2.3～2.4；当前不宣称整体提速。

## 2.2 精确交期增量

- 开始提交：`38c946a`。原公开 `evaluate_delivery` 和最终审计保持无缓存；候选入口仅对已核验内置交期规则接入私有复用。
- 相同实际节点复用原 28 位上下文的乘法结果；前缀原序相同才复用时钟，后缀还要求进入时钟的 Decimal 数值及表示完全相同。不对工时取整，不作浮点/定点近似，不重组生产累加顺序。
- 原单完成按全部片段最后时刻更新；实际完成值不变才复用等待/延期贡献，仍按原单序汇总。节点集合相同可复用已核验守恒，虚拟增删/拆片变化重新核验；未知身份、重复节点和缺失工时仍报错。
- 共享必要检查：新增增量用例及 `test_plan_evaluation.py`、`test_delivery_timing.py`、`test_delivery_second_precision.py`、`test_delivery_objective.py`、`test_delivery_search_audit.py`，退出 0，108 项 / 0.55 秒。包含 240 个排列的完整时间逐值对照、半秒附近、超 28 位累计、最后拆片、虚拟回收、上下文失效和守恒失败。
- 导出同范围验证随提交正文记录。完整影子、固定 40 万与重复仍待 2.4；不宣称整体性能已通过。

## 2.3 按需评价与集中检查

- 开始提交：`8fa738e`。先对第二阶段接线树运行累计回归，4108 项 / 53.24 秒通过；之后补按需判断专用边界及统计，230 项 / 1.54 秒通过，最终同范围检查以收口记录为准。
- 快速路径首批限于已核验内置规则、秒级交期模式和已验证接受快照。按声明前缀计算禁止/欠重，精确更差即拒绝；前缀相等才算精确交期，再比较完整质量。可能改善仍调用原完整评价及原动作保护，不直接接受。
- 快筛不生成完整 `PlanEvaluation` 和变化链 `ChainEvaluation`；原候选结构、桥接/拆单资格和有序资源保护仍先执行，因此不是所有 `Chain/SchedulePlan` 构造均被消除。连续段/规则贡献保留原公共公式；状态规则仍检查变化链。此覆盖限制作为后续热点解释，不冒称整个评价层已数值编译。
- 原七级、非秒级交期、未知规则、冷快照、新真实节点/拆片或 Decimal 指数可能溢出的边界明确回落完整路径。支持节点的工时/原单覆盖先证明有效，不通过快筛掩盖缺失工时或重复身份。
- `--screen-mode shadow` 对所有进入评价的候选额外使用原完整评价核验，快速拒绝核对质量前缀及不能改善，其他候选核对完整评价。参考路径只保留第一阶段已有的未变链复用，独立维护原评价的接受条目，不读取本次资源/连续段/交期缓存；最终审计仍完全无搜索缓存。第一处不一致抛错并记录候选序号/动作/质量，不通过改基线继续。
- 完整评价计数记录真正完整计算（包括影子额外精算）；快速拒绝、回落原因、连续段/资源/工时/时间区间复用分别计数。比较工具新增显式 `--allow-evaluation-count-change`，只允许物理完整评价数不同，逻辑额度、方案/全部评价/轨迹、编号、拆单/审计和原单精确日期仍逐项核对。

## 2.4 影子与完整测量

### 固定代码与集中检查

- 开始提交 `85dcd22`。补强影子路径：所有进入评价的候选均与独立原路径比较，而不是只查快速拒绝；原未变链条目独立维护，仅接受时提升。
- 实际测试/测量源码来自精确暂存树 `834189d12e81dd2d5891e69ee31e2e21c0d71233`，导出 `/tmp/apsgo-v7-stage24-final.N3VIbl`；后续只补文档证据，提交前按路径证明源码、工具和测试没有变化。
- 必要累计命令：`PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service`。共享 4110 项 / 56.34 秒、上述导出 4110 项 / 57.41 秒，均退出 0；干净导出原残留检查及 `python -m compileall -q src tools` 退出 0。
- 更早一次累计为 4109 通过、1 失败：架构检查误将影子缓存 tuple 的下标 `3` 视为历史基准数字。缓存改用具名字段解决，未放宽架构规则。旧源码导出的 `stage2_shadow_01` 运行主动中断并保留，不能登记为完整核验；实际从 `stage2_shadow_02` 重新执行。
- 额外扩大到 `tests/release` 的检查为 4189 通过、1 失败 / 73.03 秒：`test_package_mode_accepts_complete_manifest_without_writing` 固定期待数据库活动版本 5，仓库实际版本 14。在第二阶段前 `9ad3289` 独立导出复现同一失败（0.77 秒），且 `release/`、`tests/release/`、数据库与该提交无差异。本次不改数据库或发布测试，不冒称发布累计全部通过。
- 共享残留检查仅既有八项 V6 缺失及汇总，没有新增异常；不改历史清单。配置、数据库、原请求与旧结果不变，两份无关未跟踪技术说明保持不动。

### 有界热点复测

目录 `diagnostics/numeric_search_incremental_parallel/stage2_refinement_profile_02/`。从 `stage1_full_01` 的 `pre_refinement` 回读并重算方案/评价指纹；用既有 `load_case`、`read_plan`、`dataclasses.replace` 恢复首轮和拆后已完成的接受数 414、虚拟序号 27。保持原规则/方案，仅新探针预算设 2000，执行原 `run_width_optimization`，以标准库 `cProfile` 保存二进制和排序函数表。

原单及方案恢复、评价复算在计时外；搜索使用冷缓存，包含一次 Numba 编译。它是额外诊断，不是完整 40 万性能样本，也没有修改正式请求。首次探针 `_01` 在恢复时误用计数字段名、尚未搜索便退出，空目录保留；修正为复用原状态的 `replace` 后在 `_02` 重新执行。

- 实际 2000 次逻辑检查，794 个候选到评价入口，其中 712 个快速拒绝、82 次完整评价；15 次接受（累计 429）。快速拒绝中 472 个前级更差、240 个完整精确评分不能改善；68 次冷上下文回落，14 次可能改善须精算确认。
- 首批缓存统计：连续分类复用 26656 次、完整连续段重量复用 7045 次、未变链资源复用 15961 次；工时复用 141221 个、不变前缀 12890 个节点、完全相同入口时钟的后缀 37346 个节点、原单贡献 57470 次。它们是多候选累计命中，不是独立订单数量。

| 剩余计算项 | 累计时间 / 秒 | 解释 |
|---|---:|---|
| 直接插入位置生成 | 3.483 | 同一方案仍会反复查询候选位置；不是交期评分 |
| 变化链规则 | 2.858 | 完整变化链及状态规则仍扫描，连续段只复用分类/完整段重量 |
| 精确交期增量 | 1.620 | 不变工时/前后缀复用后，仍计算受影响时钟和贡献 |
| 候选节点与连接材核验 | 1.530 | 入口保护仍保留，不能用减少检查来冒充同算法提速 |

父级快速判断 4.491 秒、完整候选评价 3.693 秒均包含部分上表调用，不可重复相加；一次 Numba 编译 2.827 秒不代表内核扫描成本。前 2000 次形状和缓存分布不能外推整个精修阶段，完整耗时以下述正常运行与重复为准。

### 完整影子与正常测量

所有完整样本仍使用原 400000 次、9999＋10 秒请求、种子及规则。影子双算只验证，正常完整与重复独立顺序运行，不和测试/探针并发；第三阶段未进入。

- `stage2_shadow_02` 完成 400000 次，接受 804 次，生成上界 95；参考路径完整评价 172312 次，新路径完整评价 881 次，物理完整评价总计 173193，全部如实计入。它的 1705.520229 秒 / CPU 1700.415949 秒只作核验记录，不计作正常性能。
- 171431 个快速拒绝均与原精确决定一致：79572 个前级更差，91859 个完整精确评分不能改善。801 个要求精确确认（45 个前级改善、756 个后级可能改善）；80 个不支持回落（78 个冷上下文、2 个新拆片）。这些总计 172312 个进入评价的候选，不等于包含其他检查的 400000 次逻辑额度。
- `comparison.json` 退出 0，`first_difference: null`；首轮结束、精修入口和最终方案/评价/接受轨迹、逻辑额度、拆单数、生成上界及逐单精确日期均与 `stage1_full_01` 一致；核心及应用审计通过。保留零禁止/零欠重、26 链、820 吨虚拟材、14954 宽差，以及原交期结果。
- 正常第一轮 `stage2_full_01` 为 720.471532 秒 / CPU 717.405582 秒；重复 `stage2_repeat_01` 为 739.976318 秒 / CPU 737.543862 秒，均为 400000 次逻辑额度、881 次完整候选评价、804 次接受。两次均与第一阶段完整方案、评价、接受轨迹、计数和原单日期对照通过；两次之间不豁免完整评价计数的比较同样通过。
- 第一阶段两次均值 1137.670152 秒，第二阶段均值 730.223925 秒，下降约 35.81%；相对原版单次 1126.143692 秒下降约 35.16%。两次本机观察都有改善，但不是 Windows 或正式多样本稳定性能验收。
- 五份比较均退出 0、首个差异为空：影子对第一阶段、正常/重复各对第一阶段、两次正常互比、正常首次对原版。三处快照的连接、数值布局非计时计数及两次正常的全部筛选统计相同；只允许实际耗时/准备秒数变化。生产源码 SHA-256 为 `36431d57d872364aaaab5037efe17e7bf0fbf122b8bfeded2f264226d9fd9edb`；实际数字、原材料摘要和比较摘要见[测量记录](stage2_measurements.json)。
- 不能只依据完整评价次数减少约 99.49% 就宣称整轮同幅度提速。原候选计划实际仍组装 180032 次；已取消的是不必要的完整评价对象，不是所有结构对象和检查。

| 候选分类 | 个数 | 实际省略 / 保留的工作 |
|---|---:|---|
| 前级已经更差 | 79572 | 保留必要链/节点规则和前级评分，不再计算交期，不封装完整评价 |
| 含交期的精确评分仍不能改善 | 91859 | 已计算全部质量分数，但不再生成完整链摘要/方案评价；不能称这些候选没有执行评分 |
| 可能改善 | 801 | 继续完整评价及原动作保护后决定接受，不由快速结果直接提交 |
| 不支持快速路径 | 80 | 保留原精确确认，不能隐藏异常或拆单验证 |

因此 881 是完整候选评价次数，不是全部规则计算或所有精确评分的次数；40 万逻辑额度也不是 40 万次完整评价。新增计数须结合实际经过时间解释。

| 同一请求的正常完整阶段 | 第一阶段首次 / 秒 | 第二阶段首次 / 秒 | 第二阶段重复 / 秒 |
|---|---:|---:|---:|
| 首轮局部搜索 | 147.931088 | 107.413829 | 109.281721 |
| 拆单及局部搜索重放 | 227.308459 | 172.008542 | 176.334060 |
| 后置交期与宽差精修 | 743.677974 | 436.630140 | 449.873321 |
| 公共完整调用（包括准备、审计和观察） | 1123.220659 | 720.471532 | 739.976318 |

表中三阶段不是全部开销，完整调用还含构造、输入及审计；不与父级核心求解时间重复相加。两层审计保留，质量、日期、来源和谱系均未改变；一分钟目标未达，Windows 实包未验。

### 实际命令与范围

以下是本次从固定导出执行的历史命令；再次运行应使用新的输出目录，不能覆盖这些已保存结果。三个完整进程顺序运行，均使用 Conda `aps_3.10.18`，没有改变请求中的 400000 次、9999＋10 秒。

```sh
cd /tmp/apsgo-v7-stage24-final.N3VIbl

PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/profile_solver_search.py --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --scope full --screen-mode shadow --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/numeric_search_incremental_parallel/stage2_shadow_02

PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/profile_solver_search.py --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --scope full --screen-mode enabled --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/numeric_search_incremental_parallel/stage2_full_01

PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/profile_solver_search.py --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json --scope full --screen-mode enabled --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/numeric_search_incremental_parallel/stage2_repeat_01
```

每份结果通过 `tools/verify_numeric_search.py --reference <stage1_full_01绝对目录> --candidate <本次绝对目录> --allow-evaluation-count-change --output <本次目录>/comparison.json` 核验；完整评价计数是唯一明确豁免项。重复正常样本另不带豁免比较，不能把影子额外计算数当作不确定性。

缓存命中、目录峰值行数及构造计数来自实际上下文；未另采集操作系统峰值内存，不能把行数当成内存字节或声称内存性能已验收。保留其他规则/非秒级模式的精确路径，状态型规则仍扫描变化链；没有浮点替代 Decimal、额外依赖或并行后端。

第二阶段到此收口。相对于第一阶段确有本机重复收益，剩余主要成本仍为后置精修中的位置生成、规则与候选保护，不能预先保证八线程或加入某个库就达到一分钟。第三阶段未进入；没有修改 YAML、SQLite、原数据、九级评分、服务、发布或前端，也未部署、合并、推送。
