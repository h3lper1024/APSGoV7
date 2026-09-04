# 功能 22.5：调整目标顺序并取消链数验收上限

## 已确认变更

实施前提交：`495e4bf2105fa7aa7175b20e26e0a34a66bfac2d`。当前平台为 macOS / Darwin 27.0.0 arm64 / zsh，项目测试使用 Conda `apsgo_v6_3.10.18`。

用户先明确交换原第 5、第 7 项，再明确取消“最多 22 条链”的验收要求。本项只按这两项授权调整，不以求解结果反推门槛。

| 优先级 | 当前目标 | 口径 |
|---|---|---|
| 1 | 禁止违规记录数 | 越少越好，同一主体命中多规则分别计数 |
| 2 | 禁止违规严重度 | 按既有规则与投影汇总，越小越好 |
| 3 | 欠重链数 | 低于链重下限的链数，越少越好 |
| 4 | 欠重总缺口 | 逐链保留两位再求和，越小越好 |
| 5 | 相邻链首尾宽差 | 实际相邻端点绝对差求和，含跨大辊期及虚拟端点，无首尾闭环 |
| 6 | 生成虚拟材重量 | 全部生成虚拟材总重量，越小越好 |
| 7 | 非空链数 | 仅当前六项全部相同时，优先更少的链 |

按顺序逐项比较，不加权求和。前四项相同时，降低宽差可以优先于减少虚拟材或链数；宽差相同时，先比较虚拟重量，再比较链数。这是评分优先级变化，不是调换搜索阶段：整链合并、单订单移动、虚拟填充及纯链移位仍按已有流程执行，候选检查预算与停止逻辑不变，实际候选次数可能随搜索路径改变。

`quality_gate.json` 的 `maximum_chain_count` 显式设为 `null`，表示无链数上限。只对这一项允许空值，不接受字段缺失，也不允许其他上限使用空值；实际链数仍须有限、非负且与方案链数一致。旧数值上限的校验能力保留。

零禁止违规、零欠重、531 原订单完整覆盖与 29333.91 吨真实重量守恒、5% 虚拟产出比例、原订单不延后及双审计保持。20 万次候选上限与 180 秒性能门槛不变。通用发布契约和本次 GQGA4 零欠重验收仍分别处理。

## 实现与历史保护

生产评价器已经按质量声明顺序组装比较值，完整候选按整个元组比较，纯链移位按指标名称找到宽差位置。本项不改 `src/`，不新增评价器、规则类、依赖或隐藏链数限制；只更换正式配置中的两项位置及其身份，接通可选链数上限的证据检查器。

原“宽差末项”七级配置逐字节保存为：

`tests/baselines/gqga4/gqga4_rule_set_spec_width_last_historical.json`

其文件 SHA256 为 `d0a73350aedbb5c7dff0ed427bae8f90110df092cab32089b3905f6c4c596564`，旧规则身份仍为 `cd4e21b37e815c9100edd9f72b0dd0b76bb5d315463719c5e1fdf546ce96deef`。旧六级快照、原始五输入、参考脚本、旧观察脚本及全部旧排程/图表不变。

功能 22.4 的 `(0,0,0,0,22,320,11218)` 是旧顺序的已审计结果，不是当前配置复跑成果，不能直接把其中第 5、第 7 个数换位后称为新的求解结果。旧图表若复现，应传入上述历史配置及原冻结结果；旧观察脚本仍属于原宽差末项对照，不用它验证当前第五位目标。

| 身份 | 本项值 |
|---|---|
| 正式规则文件 SHA256 | `76654669df191b3ff15811f4ab5a1e41bca6efa9978b3571d2bdf5c5b42d31f4` |
| 规则集指纹 | `d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0` |
| 请求指纹 | `0d9ee1cfe251306bb516a12e440120923853bf9114b57df1864880ab0ffa5a35` |
| 质量门槛文件 SHA256 | `c83c9e95b95f1918513830e328e28f6c380525ba2b698cc3a61c28c34441c4c3` |
| 问题指纹（不变） | `cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea` |
| 策略指纹（不变） | `b74e8ea92660994a71d96cb42f4717ee7e0514206ca0378458003acf51ab99ba` |

## 验证

新增 13 项顺序回归从正式已签名配置读取目标声明，复用小型规则与完整候选入口，验证原始指标不变、宽差优先于虚拟/链数、虚拟优先于链数、前四项优先以及纯移位定位第五项。它们不是完整 GQGA4 业务规则或大数据求解验收。

门槛新增 19 项检查，其中真实运行一个经过双审计的 23 链小样例：无链数上限时通过，显式恢复 22 上限后仅此项失败。其余检查覆盖上限缺失、其他上限为空、非法或不一致的链数；不是绕过校验直接接受方案。

实际命令：

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_gqga4_quality_priority.py tests/app/test_gqga4_rule_set_mapping.py tests/integration/test_quality_precheck.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/test_gqga4_quality_priority.py tests/app/test_gqga4_rule_set_mapping.py tests/integration/test_quality_precheck.py tests/integration/test_reference_baseline_identity.py tests/integration/test_inter_chain_width_precheck.py tests/integration/test_inter_chain_width_visual.py tests/integration/test_gqga4_visual_report.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/bin/python -m pytest -p no:cacheprovider tests/integration/test_inter_chain_width_visual.py -q
```

共享树专项 121 项通过（1.20 秒），聚焦及历史证据 233 项通过 / 1 项可选绘图库跳过（1.88 秒）；已有绘图环境补验 41 项通过（0.47 秒），退出均为 0。未修改绘图器或图表，绘图库检查沿用项目未安装 Plotly 时的原有跳过约定，并在已有环境补验，不代表新增图表验收。113 个 Python 文件静态及格式检查通过；独立复核未发现未解释的配置或门槛变化。

共享树仓内累计 2739 项通过（177.74 秒），退出 0。最终暂存树同范围复验的数量、耗时、树身份和导出路径在本项提交正文记录，避免自引用提交。提交前精确暂存本项文件，导出暂存树后复验；原始输入、生产代码、停止条件及性能配置逐字节不变。

本项未执行新优先级的完整 GQGA4 排产，也未生成新图表或进行 20 对性能验收。不将旧结果、上述小样例或专项测试冒称新的全量质量成果。下一次完整复测须使用新配置身份与无链数上限门槛，结果另存。
