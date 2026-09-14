# 阶段 5.1：秒级评分与欠重优先接线

2026-09-14；macOS / Darwin，Conda `aps_3.10.18` / Python 3.10.18。实施前 `codex/delivery-objective-optimization@4fa0f98`，工作区干净。本项只实现和验证已确认的评分组合，**未执行新变体完整 GQGA4 求解**；真实样例是历史中间方案诊断。

## 实际实现与身份

- 完整九级为禁止数、禁止严重度、欠重链数、欠重缺口、旧欠清空、累计负担、宽差、虚拟重量、链数；晚交吨位仅统计。
- 唯一工时累计保持 Decimal 28 位。评分在原单完成映射后按半偶取整秒，原重量乘整秒的吨·秒精确求和，统一转吨·小时；原始日期、亚秒晚交统计不变。
- 复用原规则、完整候选和审计；只增加显式新声明，未改候选生成、阶段流程、4/64 轮转、拆单或预算。Ponytail Lite 采用现有 Decimal、计时和评价入口，不增依赖或第二套求解框架。
- 新规则版本 `3`，参数 `include_backlog_clearance=true`、`score_time_unit="second"`；后端准备 `second_precision=True`；CLI `--variant delivery-backlog-seconds`。新规则集后缀 `+delivery-backlog-seconds-v1`。默认和历史版本不改。
- 百万次输入派生规则指纹：`e809e2eafbcd426527fe8f55d7971579ec8290b0b848cb8d1d6cae0f9f8a40dd`；请求指纹：`aa3a16e54631a69b9720629acea9981ac81b0440888239ed9352afddcf012cff`。原物理输入、顺序、工时、起排、种子、策略及工艺规则逐值保持；比较工具仅允许已批准的声明差异。

## 集中验证

| 检查 | 实际结果 | 边界 |
|---|---|---|
| 相关测试 | 114 项通过，1.44 秒，退出 0 | 秒级半偶边界、分片完成、先相减再量化、原始晚交、声明/顺序、实际接受与审计篡改、新旧报告及小型公共入口 |
| 含服务累计 | 4023 项通过，50.88 秒，退出 0 | `tests/architecture tests/api tests/app tests/core tests/service`；不冒充完整质量或性能验收 |
| 历史结果回读 | 原 40 万长时限、阶段 3、历史百万次，三份完整评价和各 531 单日期/守恒原样匹配 | 没有重写请求、评价、报告或身份 |
| 真实轨迹复现 | 435086 次检查处第 47 次接受；前 47 次接受逐值等于历史轨迹；29.154133 秒 / CPU 29.129593 秒，退出 0 | 在此主动停止诊断，不签发未完成的排程结果 |
| 原完整候选入口复核 | 新顺序前五项相同，第六项负担增加；拒绝且原方案保持 | 样例当时仍有 1 项禁止，不能称最终可发布方案 |
| 保护检查 | 正式 YAML/SQLite、六项保护/输入、百万次来源及历史产物只读保持；共享检查仍为历史 8 个旧残留缺失 | 不补回旧 V6 文件、不修改清单；精确导出单独核验 |

实际测试命令（统一前缀 `PYTHONDONTWRITEBYTECODE=1`）：

```sh
/Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/core/test_delivery_second_precision.py tests/app/test_delivery_second_preparation.py tests/core/test_backlog_priority_objective.py tests/app/test_backlog_priority_preparation.py tests/app/test_backlog_search_comparison.py
/Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service
/Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/replay_second_score_witness.py --output-dir diagnostics/backlog_priority_search/stage5_1_second_precision_witness_04
```

重复样例须用新的输出目录；该工具只读取冻结历史数据，调用原公共求解和完整候选，不含新的生产动作。首次临时命令因遗漏导入失败；前两次工具试跑分别被速度必须为 Decimal、JSON 不接受 float 的既有契约拒绝。修正诊断调用/耗时编码后成功，**没有放松生产校验**。失败目录 `_01`～`_03` 保留，不伪装成成功记录。

## 第 47 次真实样例

动作是整链移位，链 `initial-000004`。禁止 `1 / 670.3`、欠重 `5 / 1456.16`、宽差 `12645`、虚拟 `120`、链数 `29` 均不变。

| 口径 | 移位前 | 移位后 | 判断 |
|---|---|---|---|
| 历史原始旧欠清空小时 | 554.0501654228165877213707082 | 554.0501654228165877213707080 | 仅减少 `2E-25` 小时，历史接受 |
| 新整秒清空小时 | 554.0502777777777777777777778 | 554.0502777777777777777777778 | 相同 |
| 新量化累计负担（吨·小时） | 1852208.311461111111111111111 | 1853797.105177777777777777778 | 增加；新完整候选入口拒绝 |

完整有序方案与轨迹保存在 `diagnostics/backlog_priority_search/stage5_1_second_precision_witness_04/`：

| 文件 | SHA-256 |
|---|---|
| `summary.json` | `f39d540d964f99246e87729f54e133e85c6cbdfadf9a898288a68851f40e2e07` |
| `physical_witness.json` | `585754b467e1481c2fae1b6bf9ee397681d9eb6bdd2bd177dd37fcecf1d39112` |

本项独立提交，精确暂存树、干净导出同范围测试、链接/编码与保护检查记录在提交正文。后续阶段 5.2 运行百万次新变体，阶段 6 才最终收口；未修改正式配置/数据库，未部署、合并或推送。
