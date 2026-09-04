# 步骤 0：链间宽差优化基线与反例

本记录固定后续算法的对照，不表示新动作已实现或 20 次性能验收已完成。实施前提交 `5de1d1d`；实际求解代码为 V7 `main@bb2a31bf49e40e739edc7b6b11be620f6561549d`。

## 执行

平台 macOS / Darwin arm64 / zsh；Conda `apsgo_v6_3.10.18`，Python 3.10.18。独立导出路径 `/tmp/apsgo-v7-width-baseline.SoDSSz`，执行前原残留检查 `clean_export` 通过、退出 0。

在导出目录运行以下命令，输出目录原先不存在；结束退出 0：

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py --code-repository /Users/miles/dev/dev-py/APSGOV7 --code-revision bb2a31bf49e40e739edc7b6b11be620f6561549d --output-dir /tmp/apsgo-v7-width-baseline.SoDSSz/baseline_run
```

本目录六个数据文件从该次输出逐字节复制。工具在求解前后校验生产文件、正式配置及原参考脚本/五输入/静态输出；完整哈希见 [quality_report.json](quality_report.json) 的 `protected_hashes`、`reference_hashes`、`artifacts_sha256`。

## 已确认结果

| 指标 | 本次结果 |
|---|---:|
| 七级质量 | `(0,0,0,0,11218,320,22)` |
| 结束原因 | 自然完成 |
| 原订单 / 真实片段 / 虚拟节点 | 531 / 533 / 16 |
| 真实重量 / 虚拟重量 | 29333.91 / 320 吨 |
| 链 / 边界数量 | 22 / 21 |
| 总宽差 / 最大边界宽差 | 11218 / 820 mm |
| 候选检查 / 完整评价 / 接受 | 94518 / 2627 / 50 |
| 同期间拆单 / 未来归还拆单 | 2 / 0 |
| 公共服务耗时 | 109.671247 秒，单次观测 |
| 正式质量门 / 两层审计 / 来源及重量守恒 | 全部通过 |

与历史 22.6 的方案、接受轨迹、计数及质量一致，接受轨迹、排程节点、逐链明细和来源明细四份文件逐字节相同；代码标记与运行时间等记录按本次事实保留。没有更改 200000 次检查、180 秒总时间、10 秒收尾预留或任何质量门槛。

从本次 `schedule_detail.csv` 按实际链序重算：首宽之和 33291，尾宽之和 21086，21 个边界总和 11218、最大 820。固定链内容、方向和期归属下的数学下界推导仍按专项设计第 3.2 节；不是完整结构搜索的最优证明。

## 身份与产物

| 身份 | 值 |
|---|---|
| 问题 | `cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea` |
| 规则 | `d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0` |
| 策略 | `b74e8ea92660994a71d96cb42f4717ee7e0514206ca0378458003acf51ab99ba` |
| 接受轨迹 | `e97fa663be97b8289b102be0afcd23e76b23fb90b1f5a05bdd014189192ae50d` |

- [完整公开结果](public_result.canonical.json)：方案、逐链评价、资源事实及两层审计。
- [接受轨迹](accepted_trace.canonical.json)、[全部排程节点](schedule_detail.csv)、[逐链明细](chain_detail.csv)、[来源守恒](source_conservation.csv)。
- `quality_report.json` 文件 SHA256：`9d94a110da2d3e510243968e229ab4b227ad7cf08796625d30a48fd8dccbee02`。

## 反例与验证边界

`tests/core/search/test_width_optimization_baseline.py` 使用真实链重、逆宽和宽差规则的合成小样本，非完整 GQGA4 规则集：

1. 旧固定搜索在仍有额度时自然完成，宽差维持 700；未生成所需结构候选。
2. 原位切链 `700→1100` 被拒绝且状态不变；一次联合安置 `700→400` 被接受，2 条链变 3 条。
3. 当前两链均合规时，旧真实节点移动动作检查数为零；手工完整尾节点移动 `700→300` 可被接受。

节点完整相等、唯一性、2500 吨原始重量守恒、虚拟及订单拆分计数不变均断言。前两项证明不能把评分能力当成实际生成能力；真实新枚举器的断言在步骤 2～4 实施。没有 `xfail` 或跳过测试。

专项最终 3 项通过、0.53 秒；首轮 2 项测试字段误名失败已修正为现有字段，不涉及生产缺陷。累计及最终暂存树验证的实际指令、数量、耗时、树身份由本项提交正文记录，不在产物中自引用提交 SHA。
