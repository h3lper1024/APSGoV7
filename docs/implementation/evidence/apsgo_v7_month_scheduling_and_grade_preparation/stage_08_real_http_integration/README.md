# 阶段 8：真实 HTTP 首轮与拆单结构修复

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

## 修复边界

`_prepare_split()` 在生成分片和隔离材之前，读取启用的连续虚拟材规则上限并统计父订单左右紧邻的虚拟段。两侧均存在且删除父订单后的合计长度超过上限时，放弃该拆单候选；合计恰好等于上限仍允许。

这是拆单动作新形成拼接的结构资格检查，不是对所有完整候选增加隐藏硬门；没有修改虚拟填充、边桥工厂、七级评分、规则阈值、预算或发布门槛。

## 当前验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider \
  tests/core/search/test_controlled_order_split.py::test_parent_removal_respects_enabled_virtual_run_limit -q
```

结果：`2 passed`。上限 2 时拒绝 2+2 拼接，上限 4 时允许，避免误伤合法边界。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider tests/core/search -q
```

结果：`588 passed in 134.98s`。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service -q
```

结果：`3297 passed in 181.09s`。

精确暂存树的全新干净导出先通过残留门禁，再执行同一累计范围，结果为 `3297 passed in 184.87s`。之后在该导出中完成 `compileall`；Conda 环境没有安装 `build` 模块，因此没有切换 Python 或安装依赖，改用同一环境已有的 `pip wheel --no-deps --no-build-isolation` 构建成功。

共享工作树的旧 V6 残留清单仍因已不存在的 `.claude`、旧 `dist` 和 `apsgo.egg-info` 报告失败；未重建或提交这些残留。正式提交以精确暂存树的干净导出检查和同范围回归为准。

## 待完成

1. 使用同一 `run_01` 数据来源、订单顺序、规则、种子和预算生成独立 `run_02`，不得覆盖失败样本。
2. 验证零禁止违规、零欠重、来源覆盖与重量守恒、拆单谱系、双审计、响应行和源数据库不变。
3. Python/HTTP 子项通过后，阶段 8 仍需 Windows Debug/Release、Designer、真实页面和失败回滚验证；未执行前不得把整个阶段标记完成。
