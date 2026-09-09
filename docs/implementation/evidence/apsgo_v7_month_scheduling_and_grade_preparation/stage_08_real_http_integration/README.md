# 阶段 8：真实 HTTP 复测、拆单边界重建与虚拟原型补正

## 范围

- 实施前 V7 提交：`ae3e111`；分支 `codex/rule-setting-api-integration`。
- 平台：macOS / Darwin arm64，zsh；Python 使用 Conda `aps_3.10.18`。
- 使用 C# 业务库版本 `20260805100613` 的 GQGA4 531 条原始订单、V3 GQGA4 230 条软硬钢字典、V7 正式规则库的临时副本，以及正式 200000 次候选检查和 180 秒核心预算。
- 正式 V7 库只复制到临时目录并在副本中迁移；C#、V7、V3 三个源数据库及边车文件验证前后不变。
- 本项不执行 Windows 构建、Designer、页面点击或真实回写，不迁移正式 V7 数据库。
- 拆单边界重建已由 `ab31244 #fix 重建受控拆单后的原链边界` 提交；后续原型补正使用独立提交。

## 首轮真实请求

`run_real_http_acceptance.py` 通过动态回环端口启动真实 Uvicorn，先在临时 V7 副本完成 schema v1→v2 和字典导入，再调用正式月计划 GET/POST 路由。请求构造只读取 C# 原始列，不从测试夹具预填 `soft_hard_class`。

后续复测可通过 `--service-config config/apsgo_v7_service.yaml` 直接读取当前运行策略；该参数覆盖历史 JSON 策略输入，HTTP 等待时间按“配置总时限 + 60 秒”计算。未传该参数时仍使用原冻结 JSON，`run_01`～`run_05` 产物及其 180 秒口径保持不变。

`run_01` 的请求 SHA-256 为 `fe652234a1299b480f1d7862b88afb82f646f11fe6d69260119020c9ae6386f6`，响应 SHA-256 为 `2bd58da9fe610728b72828e3f536999c34efee8279ae29260346200cacaa458e`。数据准备为 531 条输入、529 条命中字典、2 条 `HC220YD+Z-GL` 未命中，与冻结事实一致。

搜索自然结束而非预算截断，但结果为 `complete_not_publishable`：

| 指标 | 实际值 |
|---|---:|
| 候选检查 | 100021 |
| 完整候选评价 | 3003 |
| 接受动作 | 49 |
| 同计划期拆单 / 未来借入归还拆单 | 1 / 1 |
| 初始链 / 最终链 | 31 / 23 |
| 核心求解 | 147.747688 秒 |
| 最终问题 | `initial-000024` 的位置 17～20 有 4 个连续虚拟材，上限为 2 |

最终无缓存审计与搜索评价一致并拒绝发布，证明发布保护正常工作；`rows` 为空，没有把违规方案交给 C# 回写。

## 根因

定点重放定位到第 2 次 `controlled_order_split`。拆分对象 `0002002073-000010` 位于以下结构中：

```text
virtual-000008 → virtual-000009 → 0002002073-000010
→ virtual-000010 → virtual-000011
```

这四个虚拟材都是此前已经接受的边连接材料。本次拆单移除中间真实订单后，旧实现直接重建剩余链，将左右两段各 2 个虚拟材拼成 4 连。完整评价正确检出该违规，但质量从 `(1,100,3,464.57,11186,500,23)` 变为 `(1,2,4,544.57,11251,520,24)`；禁止违规数量同为 1，第二级禁止严重度从 100 降到 2，因此按七级字典序被接受。拆单后重放没有删除虚拟材的动作，无法再消除此结构。

## 拒绝式保护复测

第一版保护在父订单移除会直接拼成超限虚拟段时拒绝拆单。相同请求生成的 `run_02` 请求 SHA-256 仍为 `fe652234a1299b480f1d7862b88afb82f646f11fe6d69260119020c9ae6386f6`，响应 SHA-256 为 `5d22e3cc975d23607c5e7a5e12320118379b20289bbfe1759650608fc2adba26`。

`run_02` 同样以 `local_search_complete` 自然结束，连续 4 个虚拟材已经消失，但结果仍不可发布：

| 指标 | 实际值 |
|---|---:|
| 候选检查 | 98984 |
| 完整候选评价 | 2973 |
| 接受动作 | 43 |
| 同计划期拆单 / 未来借入归还拆单 | 1 / 0 |
| 初始链 / 最终链 | 31 / 22 |
| 核心求解 | 135.118873 秒 |
| 最终问题 | 600 吨 `0002002073-000010` 未拆，IF 窄钢连续真实重量超过 500 吨上限 |

核心审计的结构不变量和动作授权失败码均为空，且无缓存评价与搜索评价一致；它准确说明拒绝式保护解决了 4 连症状，却阻止了消除原 600 吨禁止违规所需的未来借入归还拆单，因此不能作为最终修复。

## 最终修复边界

受控拆单现在先定位被拆父订单，只删除其左右紧邻、用途为 `EDGE_BRIDGE` 且没有拆单分区关联的旧连接虚拟材，再复用既有 `VirtualFactory.bridge()` 重建剩余原链边界：

- 两端可直接连接时不新增虚拟材；需要时生成 1～2 个新连接虚拟材。
- 链首或链尾移除父订单时只保留另一侧真实节点，不生成悬空桥。
- 遇到 `WEIGHT_FILL`、`SPLIT_SEPARATOR` 或其他非桥接虚拟材时不删除，整个拆单候选失败。
- 新连接虚拟材先续接全局序号，拆单隔离材紧随其后；删除的历史序号不复用。
- 共享完整候选授权独立重算新边界，精确核验旧父订单和失效旧桥已删除、其他节点不变、原链及返回分片链均不可伪造。

该修复仍使用现有虚拟材原型、连接缓存、规则、评分、预算和接受条件，没有新建第二套桥接算法。

## 第三轮真实请求与新发现

边界重建提交后，`run_03` 使用同一 531 条 C# 原始订单、同一软硬钢字典、种子和正式预算再次通过真实 HTTP 求解。请求 SHA-256 仍为 `fe652234a1299b480f1d7862b88afb82f646f11fe6d69260119020c9ae6386f6`，响应 SHA-256 为 `e7deb2ea0ad11da4d571f4f35289b872356d0661abc514b3ed96ddefb24e0c46`。

求解器本身已产生可发布结果：

| 指标 | 实际值 |
|---|---:|
| 状态 / 可发布 | `success` / `true` |
| 结果行 / 真实行 / 虚拟行 | 561 / 533 / 28 |
| 禁止违规 / 欠重链 / 问题诊断 | 0 / 0 / 0 |
| 候选检查 / 完整候选评价 / 接受动作 | 139177 / 3516 / 52 |
| 同计划期拆单 / 未来借入归还拆单 | 1 / 1 |
| 初始链 / 最终链 | 31 / 23 |
| 七级质量 | `(0, 0, 0, 0, 11746, 560, 23)` |
| 核心求解 | 170.204668 秒；`search_time_limit_reached` |
| 审计 | 核心审计、结果审计及来源重量守恒均通过 |

外层验收仍以退出码 1 结束：28 个生成型虚拟行的 `hot_roll_grade` 均为 null。C# 回写契约要求该字段非空并写入 `HMGrade`；V3 与参考 `solver.py` 也都把生成型虚拟材热轧牌号设为 `SPHC`。因此不能删除验收字段检查。

根因是旧 GQGA4 虚拟原型虽然 `grade=SPHC`，但 `rule_attributes` 为空；结果转换按模型原样读取 `rule_attributes.hot_roll_grade`，没有数据可输出。正确语义为：

- GQGA4 虚拟原型显式携带 `hot_roll_grade=SPHC`；
- 生成型虚拟材不查订单牌号字典，`soft_hard_class` 继续为 null；
- Python 新库种子和 C# 新增/重新启用规格使用相同属性；
- 已有数据库通过向前新版本补属性，不改历史版本，不在响应层兜底。

曾为验证假设临时放宽验收脚本并启动 `run_04`，在确认 C# 与参考实现均要求热轧牌号后立即中断；该目录只保留规范请求，未生成响应，不作为求解结果。

## 第五轮最终复测

原型补正提交 `c8d2ba8 #fix 补齐虚拟材热轧牌号契约` 和报告身份补正 `fccd89e #fix 修正阶段八活动版本报告` 后，`run_05` 完整通过。临时规则库先从正式 schema v1 副本迁入 230 条字典并建立版本 2，再复用正常保存并启用事务，仅给版本 2 的现有原型合并 `hot_roll_grade=SPHC`，建立活动版本 3；未用默认目录覆盖原型的重量、宽厚、温区或其他属性。

`run_05` 的规范请求文件 SHA-256 为 `63ce9aa91cf47687426e2d4866c24307f2a2665fda5b1eb901a33bb71115a2c5`，规范响应文件为 `84088bdc282fd7e9f362f7d5c99893a9d6b69a5231a7d76799f937c9a5374d31`。请求的业务订单、期序、种子和预算与前轮相同；因预期活动版本从 2 变为 3，请求身份按契约变化。

| 指标 | 实际值 |
|---|---:|
| HTTP / 状态 / 可发布 | 200 / `success` / `true` |
| 外层 HTTP 求解耗时 | 170.840363 秒；单样本低于 180 秒 |
| 核心求解 | 170.218809 秒；`search_time_limit_reached` |
| 候选检查 / 完整候选评价 / 接受动作 | 140529 / 3536 / 53 |
| 初始链 / 最终链 | 31 / 23 |
| 同计划期拆单 / 未来借入归还拆单 | 1 / 1 |
| 结果行 / 真实行 / 虚拟行 | 565 / 533 / 32 |
| 真实来源 / 真实重量 | 531 / 29333.91 吨 |
| 虚拟重量 / 占比 | 640 吨 / 2.1351902371% |
| 七级质量 | `(0, 0, 0, 0, 11741, 640, 23)` |
| 连续虚拟材最大值 | 2 |

最终权威违规 0、流程问题 0、欠重链 0；核心与结果双审计均通过，搜索评价和无缓存审计一致，来源覆盖、重量守恒及 2 个拆单分区全部通过。32 个虚拟行的 `hot_roll_grade` 均为 `SPHC`，`soft_hard_class` 均为 null，符合“原型提供热轧牌号、虚拟材不参与订单软硬钢字典”的设计。三个源数据库及边车文件验证前后完全一致；正式 V7 数据库仍为 schema v1。

这是一轮真实 HTTP 功能验收和一个性能单样本，不替代既定的 20 对性能样本，也不替代 Windows 页面与真实回写验收。

## 第六轮 300 秒搜索预算复测

搜索预算补正提交 `d98ff45 #fix 将V7搜索时限提高至300秒` 后，验收工具提交 `b902f0d #fix 支持按当前服务预算执行真实复测` 改为直接读取受跟踪 YAML，并按配置总时限派生 HTTP 等待时间。`run_06` 继续使用正式 V7 数据库的临时副本完成字典迁移和原型补正，没有修改三个源数据库或 `run_01`～`run_05`。

执行命令：

```bash
caffeinate -dimsu /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_month_scheduling_and_grade_preparation/stage_08_real_http_integration/run_real_http_acceptance.py \
  --csharp-database /Users/miles/dev/dev-cs/aps-code-0806/SchedApp/Data/SchedDatas.db \
  --csharp-version 20260805100613 \
  --v7-rule-database /Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3 \
  --v3-rule-database /Users/miles/dev/dev-py/apsgo-v3/data/aps_rule_dsl.sqlite3 \
  --frozen-input /Users/miles/dev/dev-py/APSGOV7/tests/baselines/gqga4/inputs/input_orders.csv \
  --service-config /Users/miles/dev/dev-py/APSGOV7/config/apsgo_v7_service.yaml \
  --quality-gate /Users/miles/dev/dev-py/APSGOV7/tests/baselines/gqga4/quality_gate.json \
  --request-id 00000000-0000-4000-8000-000000000606 \
  --output-dir /Users/miles/dev/dev-py/APSGOV7/docs/implementation/evidence/apsgo_v7_month_scheduling_and_grade_preparation/stage_08_real_http_integration/run_06
```

结果：

| 指标 | `run_05` | `run_06` |
|---|---:|---:|
| 搜索时限 / 总时限 | 170 / 180 秒 | 300 / 310 秒 |
| 停止原因 | 搜索时间达到上限 | 候选检查达到上限 |
| HTTP 求解耗时 | 170.840363 秒 | 222.596230 秒 |
| 候选检查 | 140529 | 200000 |
| 链间宽差 | 11741 | 11726 |
| 虚拟材重量 / 数量 | 640 吨 / 32 | 660 吨 / 33 |
| 非空链数 | 23 | 23 |
| 同计划期 / 未来借入归还拆单 | 1 / 1 | 1 / 1 |
| 禁止违规 / 欠重链 | 0 / 0 | 0 / 0 |
| 双审计与来源守恒 | 通过 | 通过 |

`run_06` 的七级质量为 `(0,0,0,0,11726,660,23)`。第五级链间宽差比 `run_05` 减少 15，第六级虚拟材重量增加 20 吨；由于质量按既定顺序逐级比较，宽差先于虚拟材重量，因此新结果整体更优。搜索在 222.596230 秒先达到 200000 次候选检查上限，没有用满 300 秒；若保持 20 万次上限，继续增加时间不会让本数据集执行更多候选。

另对默认服务执行了真实重启：进程成功监听 `0.0.0.0:8001`，但规则 GET 返回 HTTP 500，根因是正式 `data/apsgo_v7_rules.sqlite3` 仍为 schema v1，而当前服务要求 schema v2。该实例已正常关闭；正式库没有迁移或写入，须在阶段 9 明确备份位置和服务窗口后才能恢复普通服务健康。

## 当前验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider \
  tests/core/search/test_controlled_order_split.py \
  tests/core/search/test_split_partition_invariants.py
```

结果：`83 passed in 0.29s`。覆盖失效桥清理、中间边界重建、不可重建拒绝、非桥接虚拟材保护、分片与候选授权不变量。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider tests/core/search -q
```

结果：`589 passed in 131.74s`。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service -q
```

结果：`3298 passed in 181.71s`。

原型补正后的相关 Python 测试：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider \
  tests/app/test_rule_set_compiler.py \
  tests/service/test_rule_initialization.py \
  tests/service/test_rule_management_service.py \
  tests/service/test_gqga4_grade_dictionary_migration.py \
  tests/service/test_month_scheduling.py \
  tests/service/test_month_scheduling_http.py
```

结果：`146 passed in 4.92s`。随后运行 `tests/architecture tests/api tests/app tests/core tests/service`，共享树结果为 `3298 passed in 175.41s`。精确暂存树 `7cad4f87708b82318c27ef778b2c7814b9f366b2` 的首轮干净导出通过残留门禁，同范围为 `3298 passed in 178.16s`，`compileall` 和 wheel 构建通过。C# 阶段 9 页面加载与阶段 10 保存静态检查通过；新增或重新启用的虚拟规格已固定携带 `hot_roll_grade=SPHC`，提交 `5c3bff8 #fix 补齐虚拟原型热轧牌号`。原阶段 8 规则客户端检查仍包含已被用户确认替换的旧地址提示断言，因此未把该项失败误算为本次原型回归。

首轮精确暂存树的全新干净导出先通过残留门禁，再执行同一累计范围，结果为 `3298 passed in 177.90s`。随后在该导出中完成 `compileall`，并使用同一 Conda 环境已有的 `pip wheel --no-deps --no-build-isolation` 成功构建 wheel；最终暂存树按相同范围复验后提交。此处不沿用第一版拒绝式保护的 3297 项结果。

共享工作树的旧 V6 残留清单仍因已不存在的 `.claude`、旧 `dist` 和 `apsgo.egg-info` 报告失败；未重建或提交这些残留。正式提交以精确暂存树的干净导出检查和同范围回归为准。

## 待完成

1. 阶段 8 的 Python/真实 HTTP 子项及 300 秒策略单次复测已通过；仍需 Windows Debug/Release、Designer、真实页面和失败回滚验证，未执行前不得把整个阶段标记完成。
2. 阶段 9 迁移正式目标库前，必须确认服务停机/切换窗口和备份位置；不能把本轮临时副本操作视为正式迁移授权。
