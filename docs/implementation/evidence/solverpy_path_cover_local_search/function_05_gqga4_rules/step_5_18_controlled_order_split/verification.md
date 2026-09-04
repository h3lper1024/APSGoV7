# 功能 5.18 受控拆单资格验证

- 实施前 `a5ca6abb3ef0eaed71320b136c66c1aa22d71df5`；Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 九文件：具体规则、唯一注册、新测试、两份旧测试夹具、本目录差分/记录、计划及 AGENTS。设计、原始基线、公共契约和门槛不改。
- 复用既有决策类型、指纹辅助函数、唯一动作入口；窄材条件提取同文件纯函数，原 5.2 行为不变。两种模式只在资格/目标期分支，不构建实际分片。详见计划第 8.6.13 节。

| 检查 | 共享树，退出 0 | 干净导出 |
|---|---|---|
| 专用测试 | 73 项，0.11 秒 | 随聚焦执行 |
| 聚焦 | 292 项，0.40 秒 | 292 项，0.33 秒 |
| 规则累计 | 967 项，1.08 秒 | 967 项，1.00 秒 |
| 仓内累计 | 1206 项，1.40 秒 | 1206 项，1.30 秒 |
| 拆单参考入口 | 61 次分组通过，1.860 秒 | 同分组，1.993 秒 |
| 原窄钢差分 | 5613 项一致，2.336 秒 | 同样一致，2.414 秒 |

初验导出 `/tmp/apsgo-split-LixG7D`，树 `3948deee9ea80edbb128f80216840deaaf709ecd`；同集合、静态、残留及编译全部退出 0。独立复核聚焦 292 项（0.35 秒）、仓内累计 1206 项（1.40 秒）及两份专项脚本通过。补齐证据后再次导出最终暂存树，同集合通过才提交并核对树身份。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_controlled_order_split.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_18_controlled_order_split/reference_differential.py --reference /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n apsgo_v6_3.10.18 python docs/implementation/evidence/solverpy_path_cover_local_search/function_05_gqga4_rules/step_5_02_continuous_narrow_steel_weight/reference_differential.py /Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py
```

参考探针真实进入 `final_cross_period_split_return()`，仅用 `sys.settrace` 观察原第 1959 行首次分片计算：到达表示通过原资格前缀，正例在执行分片前停止；不替换谓词或模块全局，结束恢复旧跟踪器。小计划显式带更早期锚单，保证参考归一期后与目标拆前期一致，不冒充任意计划等价。

61 次包括 51 正常资格一致、2 停用一致、3 同期间扩展、2 精确阈值差异和 3 资格通过但动作自然未接受。前缀共观测 9 次，其中 6 次主动停止，55 次原入口自然返回；候选检查与接受均为零。另验 8 项目标上下文/谱系保护和 138 次不同 Decimal 上下文。未解释差异为空。

首个业务差异为同期间 501 吨窄 IF：参考未来过滤不进入分片，目标授权同期间。首个精度差异为最大重量 `500.00000000000000000000000001`、父重量 `500.000001000000000000000000005`：参考 28 位阈值加法舍入后进入分片，目标精确阈值拒绝。正常 GQGA4 参数仍为 500，精度差异明确列出，不宣称所有精度逐位等价。

三个自然未接受案例分别是末片不足、需要太多隔离节点和没有隔离原型；均说明资格不是实际动作可行性。功能 17 仍须真正生成片段、校验隔离材料和完整候选；本项没有完成拆单动作或搜索。

开发期失败如实记录：首轮规则累计 1 失败/893 通过，旧测试仍断言未注册，更新为精确类注册后 894 项通过；新测试两段写入中间态使用 pytest 保留参数 `request` 导致收集失败，作者改名；首次专用 71 通过/2 失败，错误消息正则大小写不符，按实际 `RuleEvaluationContext` 修正后 73 项通过。旧窄钢脚本误用选项参数一次退出，按原位置参数重跑通过。没有改门槛、跳过测试或修改参考脚本。

独立复核确认材料短路、期序、规范拒绝、共享窄材行为及授权/动作分离。Ruff 检查和格式检查通过（49 文件）；源 SHA `87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318` 及冻结输入前后不变。精确暂存后运行同集合、残留保护及导出编译，补齐记录后复测最终暂存树。

不运行输入标准化、全场求解、性能或 V3 清单，不宣称 GQGA4 验收。独立提交后继续 5.19；必要回退仅反向恢复本项文件。
