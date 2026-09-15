# 阶段 0：数值重构基线与覆盖清单

## 1. 范围与验证

- 2026-09-15，实施前 `codex/delivery-objective-optimization@3a43ae8`，macOS arm64，Conda `aps_3.10.18` / Python 3.10.18。
- 用户授权按新实施计划持续执行。本项只读取冻结输入/历史结果、登记覆盖与替换清单；未进行搜索、性能实验、配置/数据库写入或服务操作。
- `tools/check_workspace_residuals.py --verify` 退出 1，仍为原八项残留缺失及汇总；不新增异常，不恢复旧文件。
- 用 `tools.profile_solver_search.load_request()` 读取冻结请求，再经 `load_rule_set()`、`normalize_input()` 验证，退出 0：531 原单、27 原型、18 配置/17 启用、20 个公开注册规则类型。归一化问题身份 `e391e275a2285adebbc2ca9d7a7f95d9a6d27def3cfb6fd795562b8ec4b7f993`。
- 用 `tools.verify_numeric_search.signature(..., allow_evaluation_count_change=True)` 回读 `stage2_full_01` 和 `stage2_repeat_01`，检查原始方案/评价/轨迹、原单日期、身份、逻辑额度、正式编号和已存双审计标志；`first_difference` 返回 `None`。签名摘要 `79a0aefffd0304d20b096f90333ae8954c528bf23b33e36abb53538ad2663f2d`。这是历史产物回读，不是重新执行独立审核或新数值求解。
- 命令均为 `PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python` 内联调用上述函数；请求/规则加载及签名对照合计约 1.02 秒。没有运行累计测试。

## 2. 冻结身份与对照选择

| 材料 | 身份 / 说明 |
|---|---|
| 绑定请求 | `diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json`；SHA-256 `b94d979b3d5d6545a0b7bac9a01b49b89f4feda0582914a4ff07d4d35088ae6b` |
| 请求业务身份 | `1e89085f7bbb964273b3a0c6b81dfcde6661cf0dcd655f4cb4b126645a388d79` |
| 规则身份 | `e809e2eafbcd426527fe8f55d7971579ec8290b0b848cb8d1d6cae0f9f8a40dd` |
| 策略身份 | `bef01303dc535608168597566cfeb182b831d6beca72e315f7c53a53524d2aca`；40 万额度、9999＋10 秒、种子 590531 |
| 大辊期 | `BR_00000001 → BR_00000002 → BR_00000003 → BR_00000006`；任务提供，不固定为四期 |
| 原组合结果 | `combined_01/measurement.json` SHA-256 `812db75f90ffa2ef0d449a5dcc2234ce1b832ecdc3666c6ac227b41d0120afbb`；同目录日期报告 `6b64061d6d94a1f06abcc20a35859729dae3a7d2f4f834ffce545354243b193e` |
| 固定方案主参照 | `diagnostics/numeric_search_incremental_parallel/stage2_full_01/measurement.json` SHA-256 `2bf44eefd1b6d53bcb35e6ada2562297741f20239f41b7a3a7a096c860b4063e` |
| 固定方案重复 | `stage2_repeat_01/measurement.json` SHA-256 `297cf9ffb2c81faf3613fd8dfb1ee6a28e787d48acbe8f0fe28a49db520d0933` |
| 共同最终方案 | `c7a12ee989d00c6c869a28981ddfabd12a74712aca19ddcbe921650ce0f3f749`，400000 次消费、804 接受；历史耗时 720.47 / 739.98 秒不当作新计时 |
| 前序单线程批次结果 | `stage3_full_t1_01/measurement.json` SHA-256 `b3281e3f47654dfb0c2f2edea02f5cdcf41fe9ce668e3a204b83af8717e11fa1`，同最终方案；580.26 秒仅单个历史试验，不算当前串行基准 |

阶段 5 如需旧源码运行，使用 `3a43ae8` 的精确 Git 导出而非新引擎中的旧模式；阶段 2 的成对产物用于固定方案/阶段快照对照。没有把旧结果的测量代码身份替换为 `3a43ae8`。正式配置和规则库摘要、四份既有工作区文档保护见实施计划第 2.3 节。

## 3. 字段到数值标准与使用位置

| 字段组 | 权威数值标准 | 使用位置 / 待替换点 |
|---|---|---|
| 宽、厚、温区及有效位 | 按任务、字段精确定点；温区共用倍率 | `compatibility`、`path_cover`、`rules/concrete`、`_bridge_numeric`、`width_optimization`；删除 `_finite_projection` 权威比较及宽差双口径 |
| 节点/原单/原型重量 | 相同精确重量倍率，至少 0.01 吨可表达 | `evaluation`、`resource_facts`、`neighborhoods`、`controlled_split`；替换 Decimal 汇总和浮点排序 |
| 原单总工时、原型工时、起排、交期 | 原请求小时一次量化毫秒；交期当天 24 点；评分整秒 | `delivery_timing`、`chain_order`、输入准备及输出。必须从请求总工时取得，不能从旧 `hours_per_tonne * weight` 反推 |
| 角色、牌号/热轧/软硬/钢种/表面 | 独立整数码和缺值标记；现有文本规范化保持 | 规则编译、同规格/窄钢/高表面、连接、初始排序；不得以编码大小当业务顺序 |
| 客户名称/客户等级/执行标准 | 只在边界识别/预计算角色与规则优先级 | `input_normalizer`、规则优先级；搜索不重复查文本 |
| 节点/原单/资源/期/原型/链身份 | 稳定身份与数组位置分离，期序由任务提供 | `_search_numeric`、候选编辑、原单全部片段索引、独立审核 |
| 虚拟用途/拆片与授权 | 数值用途码、拆分组、稳定片序；父重/父时守恒 | `virtual_material`、`controlled_split`、普通桥回收、最终审核 |
| 规则参数与评分 | 精确阈值/比例；严重度逐条六位、欠重逐链两位、交期整秒 | 20 类型支持表、规则声明顺序；停用与未知配置前置检查 |
| 访问/匹配/队列/栈/候选游标 | 整数/布尔数组；确定性次序与代次 | `path_cover`、`initial_solution`、`width_optimization`；静态可连接与动态可用分开 |

## 4. 注册规则覆盖清单

目标数值函数按职责落在规则编译/评价模块，名称在阶段 2 实现时冻结；本表不预建空函数。下列类型均需实现或针对未支持参数前置拒绝，不能因为固定输入没启用就跳过测试。

| 规则类型 | 冻结输入 | 参数和最低验证范围 |
|---|---|---|
| ChainWeightRangeRule | 启用，700/2000/2000 | 最小/最大/目标；恰等、欠重、超重、逐链舍入 |
| ConsecutiveReverseWidthRule | 停用 | 启用时相邻连续逆宽；停用零贡献 |
| ReverseWidthCountRule | 启用，2 | 保留基准而非相邻增宽数；缺值清基准 |
| ConsecutiveVirtualMaterialRule | 启用，2 | 真实过渡打断、多段与最长段 |
| WidthTransitionRule | 启用，20/200 | 有向真实增宽、虚拟绝对差、多个虚拟跨真实净增宽；共享身份启停 |
| ThicknessTransitionRule | 启用，较薄基准/四档 | 较厚与较薄、绝对/相对、开闭区间原序首命中和默认档 |
| TemperatureOverlapRule | 启用，10/不忽略/虚拟自适应 | 缺值、等号、忽略及自适应开关 |
| SoftHardConnectionRule | 启用 | 虚拟/真实过渡优先；缺类别回退同热轧牌号和其他合法策略 |
| HighSurfaceRunCountRule | 启用，FC/FD/5 | 空表面断段；角色打断、逐段和最长统计 |
| ContinuousNarrowSteelWeightRule | 启用，IF/小于1400/500 | 严格宽上界、普通角色、多个规则参数各自命中 |
| SameSpecContinuousRealWeightRule | 启用，厚宽牌号/1000 | 各合法有序字段子集；缺值、组变、角色打断 |
| StrategicCustomerPriorityRule | 启用 | 原关键词顺序/命中等级/默认等级、空客户；不新增评分 |
| LateOriginalPeriodMoveRule | 启用 | 实际期序、同期间/提前/延后、拆片、虚拟跳过 |
| FutureFillWeightTargetRule | 启用，1200 | 全节点重量、非负缺口，非新增评分或违规 |
| VirtualOutputRatioRule | 启用，0.05 | 分母真实＋虚拟、严格交叉相乘、零阈值严重度1 |
| ControlledOrderSplitRule | 启用，两种模式 | IF/宽界、片重1～500、次数10000、分隔2个/40吨；授权/来源期/原安排期及禁重复拆分 |
| InterChainWidthGapRule | 启用 | 实际虚拟首尾、跨大辊期、无首尾闭环 |
| DeliveryDuePerformanceRule | 启用，清空/整秒 | 原单最后片段、无旧欠/停用、声明组合；旧小数模式不静默执行 |
| SyntheticWidthLimitRule | 不在此请求 | 公共合成规则的宽度限制、零值/边界/缺值；用于通用测试，不写产线常量 |
| SyntheticNodePriorityRule | 不在此请求 | 公共合成优先级字段及默认值，稳定排序；不能遗漏注册类型 |

所有类型：验证范围、参数类型/形状、启用/停用、错误定位、数值范围以及原因/主体/严重度；有理数严重度按物理单位量化。当前尚无新数值规则实现，不把此覆盖表记成测试通过。

## 5. 动作、状态与旧路径替换清单

| 调用位置 | 冻结算法语义 / 后续核查 |
|---|---|
| `solver.solve` | 构造→首轮→受控拆分及唯一重放→符合入口条件的后置精修→核心审核；公共服务另作应用审核 |
| `run_local_search` | 整链→真实节点移动→虚拟填充→目标启用时整链调序；共享预算 |
| `_round_robin`、`_backlog_iterators` | 每原单4提案，直接插入位置优先且保留桥接位置；稳定身份续访 |
| `_scan_critical_families` | 先一次普通桥回收，重点/常规64步轮转；重点3类/常规6类；接受后旧迭代器失效，保留合法续访提示 |
| `_try_intra_move`、`_try_segment_edit`、`_try_chain_cut`、`_try_chain_order`、`_try_bridge_reclamation` | 链内/跨链节点与片段、切链、整链调序、普通桥回收；不恢复已放弃的统一搜索流程 |
| `_prepare_split`、`_authorized_split_replacement` | 同期间与未来借入归还；分隔、私有片段、授权、父重/工时和正式编号 |
| `try_complete_candidate`、`_candidate_edit` | 逐候选构造完整对象、节点字典和对象恢复待移除；唯一接受/取消/代次/错误边界保留 |
| `_search_numeric.NodeColumns`、`PlanNumericView` | 目前保留对象节点、浮点投影、Decimal重量及多个字典；替换为权威列，不能以已有数组名称宣称完成 |
| `delivery_timing`、`chain_order`、`evaluation` | Decimal时钟、排序、精算回落和旧投影待替换；保留同新标准全量独立参考 |
| `_bridge_numeric`、`_delivery_parallel` | 前者浮点规格统一为整数，后者旧上下界辅助路径在新完整内核可用后删除/替换 |
| `final_audit`、`contract_audit`、`result_assembler`、服务绑定 | 审核重建新事实；明确声明/版本拒绝，正式数据库不自动迁移 |

允许的新请求差异仅为数值标准身份、其要求的评分声明/规则版本/指纹及绑定结果；原单序、物理字段、材料分类、规则业务参数、起排/交期/速度、40万额度与时限不变。新指纹待代码建立后实算，不能现在填虚构值。

## 6. 结论与下一项

阶段 0 的基线回读、覆盖及替换清单完成；实施提交身份与精确导出检查见 Git 提交正文。下一项 1.1 建立单位/四舍五入/范围/身份基础，1.2 后集中做转换和数组检查，不逐小步运行完整求解。
