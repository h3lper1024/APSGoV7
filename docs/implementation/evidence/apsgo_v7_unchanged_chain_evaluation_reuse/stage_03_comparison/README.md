# 阶段 3：完整对照、性能和函数分析

实施前、受测生产提交 `52798855ab8b2613f5034539f86f078abf1ae321`。macOS ARM64 / Conda `aps_3.10.18` / Python 3.10.18。本项生产不再修改，仅补测量脚本、测试及证据；所有原始运行完整保留，不覆盖阶段 0 或前一专项结果。

## 1. 源码与输入边界

| 项目 | 固定身份 |
|---|---|
| 旧生产提交 / 树 | `1bc5bf322f8cdf34c20d947af0bacb36d954c818` / `2c994c428c181892974c09715c54ce50f16b64a9` |
| 新生产提交 / 树 | `52798855ab8b2613f5034539f86f078abf1ae321` / `3c9146abcde75ac1112a77f430d54eebe43e26c7` |
| 旧 / 新源码导出 | `/tmp/apsgo-v7-reuse-pairs.UlRN2t/old` / 同根 `new`，分别由上述提交 `git archive` 新建 |
| 旧生产汇总 SHA-256 | `7cd98513e1f1d0b1ea15b62b59a7fc25ae1644b40ef5576ac712f6d7c511c392` |
| 新生产汇总 SHA-256 | `e11bdea69285f3c260353eb54560a6769bbe8f1198098a13d22a038aa0f77602` |
| 最新绑定输入字节 SHA-256 | `11bdd096717414a25b14e7a219b860e6e84953f17e9c02bbf8e298da9266829c` |
| 两侧共用测量工具 SHA-256 | `822239430ee9c06a4d0db87d5da9ea039b219b660737089fe819292ab08cc581` |
| 五对运行器 SHA-256 | `1f34e154f745d80b05e0dc74ab774574ee4be243aab9f6ef854fa5de683d043d` |
| 分析探针 SHA-256 | `3e29523eb8c5d55132262cbcb347022b83b00b744a8f98d5fc069c4d6a4007eb` |

生产差异严格只有 `evaluation.py`、`neighborhoods.py`。每个样本检查源码、工具、输入前后身份；提交/树与导出绑定另存 `diagnostics/unchanged_chain_evaluation_reuse/exports.json`，SHA-256 为 `3f50a1deae9ef19fbbaeda595917f70ab6b9c1fd355553aff30838fdda7da7cb`。相同内容同时保存在本目录 [statistics.json](statistics.json)，不能把导出中的 `git_available=false` 当作未记录源码身份。

最新输入原始顺序和数值、900 秒总时限、10 秒收尾、200000 次上限、种子 590531 均未覆盖；不从现场数据库重新组装。原始订单及大文件只在忽略目录，不纳入 Git。公开运行清单仍实际记录 `code_revision=unversioned`；不修改或豁免这一字段，受测真实源码由上述独立身份说明。

## 2. 正确性结论

五对十次完整运行全部退出 0、逐字段对照通过。只清除明确位置的计时值，保留计时字段的存在性；初始、首轮和最终的有序方案、完整评价、违规、质量、轨迹、业务计数、公开边缓存、资源、拆单、审计和整个非计时运行清单均一致。首次旧样本另与阶段 0 完整基线比较，无差异。

- 首轮：64226 次候选检查、2263 次完整评价、34 次采纳。
- 完整：200000 次候选检查、3697 次完整评价、54 次采纳；两类拆单各 1 次。
- 最终质量：`(0,0,0,0,11726,660,23)`，零禁止、零欠重、23 链、660 吨虚拟材。
- 停止原因均为 `candidate_limit_reached`，不是墙钟截断；均发布且核心/契约审计通过，来源和资源事实完全一致。
- 最终完整评价：`8856c354afc0da5a4cae3e27d7c4d6aa63bb131e2ea4bfa6f0e5030b0c5816ff`。
- 最终轨迹：`1a15b6e4fd636b85bbc23f1794f9d2049a3d30d5e52497583dc6f31063ecdd22`；公开求解结果：`1c2820ae2fd5b19273636de8b7d33453db9089c3b600067958a79325e3c127bb`。

比较工具首次手工导入漏设 `tools` 搜索路径，未运行求解且报导入错误；补齐路径后原数据对照成功。没有重跑或替换任何性能样本。

## 3. 五对完整计时

全部为独立进程、同机器/解释器/输入/日志设置，顺序为旧→新、新→旧交替；未同时执行重测试或分析器。临时 `caffeinate -i` 随测量进程退出，没有修改永久电源配置。每次外部起止时间与被测调用耗时差为 0.713049～0.851433 秒，未观察到休眠或明显等待样本；全部样本保留，无筛选、补齐或剔除。

| 对次 | 旧首轮 / 新首轮（秒） | 旧完整 / 新完整（秒） | 旧完整 CPU / 新完整 CPU（秒） |
|---|---:|---:|---:|
| 1 | 66.182502 / 37.699187 | 130.912156 / 83.352402 | 130.808109 / 83.238728 |
| 2 | 67.010535 / 37.186333 | 131.603380 / 82.831287 | 131.514919 / 82.767867 |
| 3 | 66.527548 / 37.217192 | 131.241016 / 83.371054 | 131.129282 / 83.304231 |
| 4 | 67.047590 / 37.251942 | 131.753497 / 83.016678 | 131.639636 / 82.958070 |
| 5 | 66.152820 / 37.312560 | 130.811528 / 83.180424 | 130.720529 / 83.078666 |

| 口径 | 旧中位数 | 新中位数 | 减少 |
|---|---:|---:|---:|
| 首轮经过时间 | 66.527548 秒 | 37.251942 秒 | 44.005239% |
| 首轮进程 CPU | 66.461069 秒 | 37.225061 秒 | 43.989675% |
| 完整观测调用经过时间 | 131.241016 秒 | 83.180424 秒 | 36.620100% |
| 完整观测调用进程 CPU | 131.129282 秒 | 83.078666 秒 | 36.643696% |

主要阶段经过时间中位数：拆单及重放 22.664557→12.746171 秒，宽差优化 38.270208→29.303330 秒；构图 2.089885→2.113622 秒，未针对构图提速。完整调用包含观察快照、封装及审计开销，不是网络到前端的端到端时延。CPU/经过时间约 99.86%～99.93%，仍主要使用一个逻辑核，未引入并行。

原始位置 `diagnostics/unchanged_chain_evaluation_reuse/stage_03_pairs/`，每次的输入身份、完整测量、起止记录、退出码和日志均独立保存。[statistics.json](statistics.json) 给出所有样本索引及文件哈希；原 `summary.json` SHA-256 为 `22ef3944ac110a75a7ed841f7ab450c485455c7705ca0d65f10e75764e8b4228`。

**这是五对专项收益验证，不替代历史 180 秒 / 20 对正式性能门，也不能直接推算 Windows 耗时。** 没有调整任何质量或性能门槛。

## 4. 独立函数分析与保留规模

另起进程运行首轮 `cProfile`（Python 函数耗时分析器）和入口计数探针；该样本不进入五对统计。完整首轮快照与 `pair_01_new` 逐字段一致，仍为 64226/2263/34，无墙钟截断；探针计数守恒通过。带观察的首轮为 81.832069 秒、CPU 81.768170 秒，不能用这一带分析开销的时间代表正常性能。

| 完整候选内部评价入口 | 同一批候选无复用所需次数 | 实际调用 | 复用省去 |
|---|---:|---:|---:|
| 完整链评价 | 51937 | 4136 | 47801，92.036506% |
| 节点评价 | 1235956 | 85225 | 1150731，93.104528% |
| 方案级评价 | 2263 | 2263 | 0，每个完整候选仍重算 |

这里是完整链/节点/方案**评价入口次数**，不是各个具体规则方法的总调用数。首轮 78 个冷候选（也为零命中候选），没有未知规则回落；首次接受前拒绝不缓存的设计保持。包含初始构造和快速检查的总分派数另为链 6275、节点 85756、方案 2264，不能与上表混用。

首轮实测：当前方案最多 30 个条目、候选最多 30 个，两者按条目对象去重后最多 31 个；当前最多 555 个节点对象，两者去重最多 556 个。只是本次首轮观察，不是全进程内存字节数或全流程固定上限。通用有界性还由阶段 2 的连续拒绝与接受替换回归保护，不保存候选历史。

剩余热点（累计时间包含下层，**不能相加**）：

| 函数 | 本次分析累计时间 | 解释 |
|---|---:|---|
| `VirtualFactory.bridge` | 57.711 秒 | 虚拟桥接搜索，含下面边查询与身份计算 |
| `RuleEdgeDecisionCache._decision` | 39.680 秒 | 边决策查询，含节点身份计算 |
| `semantic_fingerprint` | 35.880 秒 | 节点语义身份的准备/查询 |
| `canonical_json` | 26.505 秒 | 身份编码，属于上述调用链的一部分 |
| `_evaluate_plan` | 14.913 秒 | 候选原始贡献组装、全局重算及评分；已不是最大热点 |

后续若继续优化，应先针对桥接/边查询的对象和身份重复成本重新设计、测量；本项不自动改 BFS/DFS、CSR，不增加 NumPy/Numba 或历史缓存。原始分析位于 `diagnostics/unchanged_chain_evaluation_reuse/stage_03_profile/`；[profile_summary.json](profile_summary.json) 保存调用次数、范围、比较结论和原文件哈希。

## 5. 复现命令

从第 1 节提交分别新建干净导出；临时目录改变时替换 `--old-root/--new-root/--code-root`。下列目录是实际已生成证据，复跑须使用新的输出目录，不允许覆盖。

```sh
/usr/bin/caffeinate -i env PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_unchanged_chain_evaluation_reuse/stage_03_comparison/run_pairs.py \
  --old-root /tmp/apsgo-v7-reuse-pairs.UlRN2t/old --new-root /tmp/apsgo-v7-reuse-pairs.UlRN2t/new \
  --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/unchanged_chain_evaluation_reuse/frozen/20260908_205419_4a73iqo7/prepared_request.json \
  --output /Users/miles/dev/dev-py/APSGOV7/diagnostics/unchanged_chain_evaluation_reuse/stage_03_pairs

/usr/bin/caffeinate -i env PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_unchanged_chain_evaluation_reuse/stage_03_comparison/probe_reuse.py \
  --code-root /tmp/apsgo-v7-reuse-pairs.UlRN2t/new \
  --prepared-request /Users/miles/dev/dev-py/APSGOV7/diagnostics/unchanged_chain_evaluation_reuse/frozen/20260908_205419_4a73iqo7/prepared_request.json \
  --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/unchanged_chain_evaluation_reuse/stage_03_profile
```

## 6. 工具回归与完成边界

新增薄配对运行器和只观察的首轮探针，继续使用原公开求解工具，不复制算法。新增 38 项测量测试通过（0.44 秒）；包含仅忽略明确计时位置、完整有序业务字段、固定工作量、真实小请求探针的结果不变/恢复补丁/已有目录不覆盖。Ruff 与独立只读工具复核通过；共享及最终精确暂存树累计以本阶段提交正文为准。

最终共享树按实施计划第 6 节累计范围 **3560 项通过，97.75 秒，退出 0**。独立复核再次逐个核对 10 次原始测量、阶段 0 基线、探针完整快照、哈希和正式质量报告，未发现首个业务差异。`git diff --check` 与 Ruff 静态检查通过。额外的格式预览提示 `probe_reuse.py` 有纯排版差异；为保持本次实际执行的探针字节和哈希不变，没有在测量后格式化它，不将这项预览记作通过。最终精确暂存树累计和残留检查仍单独执行并记入提交正文。

本机正式质量回归与 Windows 待验状态见下续执行记录。Windows 实包仍需要明确发布清单、同输入/配置/匹配规则库和真实运行诊断；当前 macOS 不能代验，不部署、停服、合并主分支或推送。

## 7. 原正式 GQGA4 质量回归

使用既有 `quality_precheck.py`、原正式输入及原质量门，源码绑定 `52798855ab8b2613f5034539f86f078abf1ae321`；保护路径哈希校验通过，没有修改原门槛或借用现场 YAML。完整报告 `passed=true`、失败列表为空。

| 项目 | 实际值 |
|---|---|
| 策略 | 原正式 180 秒总时限、10 秒收尾、200000 次、种子 590531 |
| 单次公共调用 | 72.170514 秒，含原质量观察包装；未单独记录进程 CPU |
| 停止原因 | 候选上限，不是时间限制 |
| 候选 / 完整评价 / 接受 | 200000 / 3603 / 58 |
| 七级质量 | `(0,0,0,0,10912,600,22)` |
| 真实来源 | 531 单、29333.91 吨，逐单覆盖和重量守恒全部通过 |
| 拆单 | 2 次同期间拆单，未来借入归还 0 次；授权及谱系审计通过 |
| 虚拟重量比例 | `0.02004415727848450135648834382`，小于原 5% 上限 |
| 核心无缓存审计 / 结果契约审计 | 均通过 |
| 请求指纹 | `0d9ee1cfe251306bb516a12e440120923853bf9114b57df1864880ab0ffa5a35` |
| 问题指纹 | `cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea` |
| 规则指纹 | `d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0` |
| 结果指纹 | `3d1689274bfa5f06d30411613e2fccdc14a9adc32464d9f4e7e8163fe0f04f67` |
| 轨迹指纹 | `aa9b16a7713d9f00e36c741600c483be9a5bcbbb6772e1d03caa850f122c12ae` |

这与第 2 节的现场请求不是同一输入/规则身份，故 72.17 秒不能与现场输入 83.18 秒作提速比较。此单次运行只关闭本项正式质量回归，不替代原 20 对性能验收。

实际命令（复跑使用新输出目录）：

```sh
/usr/bin/caffeinate -i env PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py \
  --code-repository /Users/miles/dev/dev-py/APSGOV7 \
  --code-revision 52798855ab8b2613f5034539f86f078abf1ae321 \
  --output-dir /Users/miles/dev/dev-py/APSGOV7/diagnostics/unchanged_chain_evaluation_reuse/stage_03_formal_gqga4
```

原始文件位于上述忽略目录；保留全部结果及逐单 CSV，不纳入 Git 客户订单。

| 文件 / 输入 | SHA-256 |
|---|---|
| `quality_report.json` | `6e46c3813af80016a3c04039317a9fd0c228715bee67757071b3cbd592ab5cc6` |
| `public_result.canonical.json` | `7370742f44ddc87f051d67637ed16564c66818742ce5d168b467395ece0c7045` |
| `accepted_trace.canonical.json` | `9364d8b7c994bf7fb2944090a69ceb8e04ba2a41ce79f0ca323e43bfe5542136` |
| `schedule_detail.csv` | `bdef77cb712c34ee81a3f0a2fb274b161ff01fada52e4379c99ada1e13c56173` |
| `chain_detail.csv` | `332a0c65ec892b8af615b05a1a4f15d38467a16e528386d3b0c909b35c845421` |
| `source_conservation.csv` | `5437cd7181f807616e250a0a30120f5d7b4b52c7844ec7c4e0383674ea10336e` |
| 原质量门 | `c83c9e95b95f1918513830e328e28f6c380525ba2b698cc3a61c28c34441c4c3` |
| 原质量脚本 | `cf324e7db2b908151aebe1d648b13a277aeeb4691fc66854d39b70986dd361b3` |

## 8. 下一入口与限制

本机正确性、实际重复执行下降、五对收益及正式质量均有证据；最终累计与精确树结果由本阶段提交正文记录。当前平台 macOS ARM64，阶段 4 的 Windows EXE 无法在本机代验，最新现场归档又没有匹配 SQLite 和发布清单，因此不标记整个跨平台专项完成。

后续在 Windows 独立构建明确的本轮提交，保留发布清单，使用测试配置和匹配规则库副本，以相同订单/规则/原型/字典及 900/10/200000 策略运行，回传首启与后续完整诊断。不要用版本为 `unversioned` 且缺少发布清单的旧 EXE 推断严格新旧性能收益；单次只能说明该次耗时，需要稳定结论时按实施计划做同机成对测量。不覆盖现场数据库、不新增 EXE 重放接口、不自动部署或合并主分支。
