# 阶段 8：真实 HTTP 复测与拆单边界重建

## 范围

- 实施前 V7 提交：`ae3e111`；分支 `codex/rule-setting-api-integration`。
- 平台：macOS / Darwin arm64，zsh；Python 使用 Conda `aps_3.10.18`。
- 使用 C# 业务库版本 `20260805100613` 的 GQGA4 531 条原始订单、V3 GQGA4 230 条软硬钢字典、V7 正式规则库的临时副本，以及正式 200000 次候选检查和 180 秒核心预算。
- 正式 V7 库只复制到临时目录并在副本中迁移；C#、V7、V3 三个源数据库及边车文件验证前后不变。
- 本项不执行 Windows 构建、Designer、页面点击或真实回写，不迁移正式 V7 数据库。

## 首轮真实请求

`run_real_http_acceptance.py` 通过动态回环端口启动真实 Uvicorn，先在临时 V7 副本完成 schema v1→v2 和字典导入，再调用正式月计划 GET/POST 路由。请求构造只读取 C# 原始列，不从测试夹具预填 `soft_hard_class`。

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

首轮精确暂存树的全新干净导出先通过残留门禁，再执行同一累计范围，结果为 `3298 passed in 177.90s`。随后在该导出中完成 `compileall`，并使用同一 Conda 环境已有的 `pip wheel --no-deps --no-build-isolation` 成功构建 wheel；最终暂存树按相同范围复验后提交。此处不沿用第一版拒绝式保护的 3297 项结果。

共享工作树的旧 V6 残留清单仍因已不存在的 `.claude`、旧 `dist` 和 `apsgo.egg-info` 报告失败；未重建或提交这些残留。正式提交以精确暂存树的干净导出检查和同范围回归为准。

## 待完成

1. 完成最终暂存树复验并独立提交边界重建修复。
2. 使用同一数据来源、订单顺序、规则、种子和预算生成独立 `run_03`，不得覆盖 `run_01` 或 `run_02`。
3. 验证零禁止违规、零欠重、来源覆盖与重量守恒、拆单谱系、双审计、响应行和源数据库不变。
4. Python/HTTP 子项通过后，阶段 8 仍需 Windows Debug/Release、Designer、真实页面和失败回滚验证；未执行前不得把整个阶段标记完成。
