# 步骤 7：确定性与全流程性能验收

## 范围与当前状态

本项验证代码 `35642779e4e5bcd4d0c9681b9825b2e4262dd5d1`，生产实现与步骤 6 的 `6902be0` 相同。初始干净导出 `/tmp/apsgo-v7-width-step7-start.jA5zZa` 的原残留检查以 `clean_export` 模式通过；macOS / Darwin arm64，Python 3.10.18。

本项不修改生产代码、规则、输入、种子、检查额度、时间限制或验收门槛；只新增证据收集脚本及其边界测试。连续 3 次确定性验证、用户授权的保持唤醒整组性能复测均通过；首批受休眠干扰的记录保留但不作为正式性能依据。最终暂存树回归与提交核验按计划执行，结果记入本项提交正文。

## 正式复测结论

| 指标 | 新实现（20 次） | 参考控制（20 次） |
|---|---:|---:|
| 中位数 | 171.498582021 秒 | 22.845218520 秒 |
| 第 95 百分位 | 171.586066416 秒 | 23.035479166 秒 |
| 最大单次，仅记录 | 171.629234834 秒 | 23.050618041 秒 |
| 参考相对冻结值的绝对漂移 | 不适用 | 中位数 3.2048%、第 95 百分位 2.8527% |

参考控制漂移均小于原 20% 限制，新实现中位数及第 95 百分位均小于 180 秒；全部 20 对通过各自质量或输出一致性检查。预热未计入统计；没有筛选、替换或删除样本。

20 次新结果均为七级 `(0,0,0,0,10912,600,22)`，零禁止、零欠重，531 个来源及 29333.91 吨真实重量守恒。与原宽差 11218 相比减少 306 mm，约 2.73%；22 链不变，虚拟重量从 320 增至 600 吨，符合已确认的目标优先级，但不能夸大收益。

**停止边界：这 20 次全部触发搜索时间上限，实际检查 166591～171789 次，没有跑满 200000，也没有自然穷尽候选。** 轨迹及最终质量相同不意味着完整运行身份或检查数相同；它们用于性能与质量验收，不代替另外三次未时间截断的确定性证据。三次确定性运行均为 200000 次额度停止、稳定语义完全一致。

本批实际时间段未检出系统休眠事件，UTC 总历时与子进程计时合计相差约 3.77 秒，详见 [环境复核](environment_interruption.md)。排程图表仍见 [步骤 6 报告](../step_06_real_run/report/report.html)，保持原发布顺序和独立证据身份，不覆盖旧报告。

## 固定判定方式

- 确定性：连续 3 次新进程，比较结果、发布、两层审计及轨迹身份、全部计数、停止原因、质量和稳定明细文件。耗时字段及包含耗时的公开结果文件整体哈希不参与相等比较，但原文件完整保留。
- 候选检查额度耗尽属于确定性可比较的停止；墙钟时间或取消停止的样本保留，但不能作为确定性通过证据，不重新抽样补齐。
- 性能：每方预热 1 次，再运行 20 对，奇数对参考先、偶数对新实现先；从启动子进程前计到子进程退出，包含原入口的输入、保护检查、求解、审计和输出。正式运行不使用步骤 6 的热路径观察工具。
- 先验证参考中位数及第 95 百分位相对冻结值的绝对漂移均不超过 20%；控制无效则不判新实现通过。控制有效时，新实现的中位数及第 95 百分位均须不超过 180 秒。第 95 百分位用原最近秩法，20 个样本取排序后第 19 个；最大单次耗时只记录，不另设门槛。
- 每次尝试结束立即原子保存总记录，包括退出码、标准输出、错误、耗时和产物身份。失败先落盘再停止，禁止删除不利样本、补跑替换或修改冻结门槛。

`run_acceptance.py` 复用原 `quality_precheck.py`、参考测量和统计函数，不建立新求解入口或第二套验收公式。每个新实现样本保留原六份输出；参考测量沿用原临时目录流程，保留标准输出、错误、数值、产物哈希和差异记录，**不声称保留其临时原始文件**。

## 执行命令

在 V7 根目录，以独立输出目录执行，不复用已有目录。两种模式不得并行，也不与其他求解或累计测试并行：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_07_acceptance/run_acceptance.py --mode determinism --code-root /tmp/apsgo-v7-width-step7-start.jA5zZa --code-repository /Users/miles/dev/dev-py/APSGOV7 --code-revision 35642779e4e5bcd4d0c9681b9825b2e4262dd5d1 --output-dir docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_07_acceptance/determinism
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_07_acceptance/run_acceptance.py --mode performance --code-root /tmp/apsgo-v7-width-step7-start.jA5zZa --code-repository /Users/miles/dev/dev-py/APSGOV7 --code-revision 35642779e4e5bcd4d0c9681b9825b2e4262dd5d1 --output-dir docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_07_acceptance/performance
```

用户确认后的整组复测实际命令如下。该目录同样不得复用；再次手工测量需使用新的目录，并在整个过程保持电脑唤醒、不合盖：

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/caffeinate -i /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_07_acceptance/run_acceptance.py --mode performance --code-root /tmp/apsgo-v7-width-step7-start.jA5zZa --code-repository /Users/miles/dev/dev-py/APSGOV7 --code-revision 35642779e4e5bcd4d0c9681b9825b2e4262dd5d1 --output-dir docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/step_07_acceptance/performance_awake_rerun
```

## 执行记录

- 运行前，导出目录的外部参考身份核验 10 项通过；未启动参考求解。
- 工具复核发现测量适配器抛出 `KeyError` / `TypeError` 时失败尝试可能未进入样本列表，已纳入统一留痕并由独立探针复核；这属于证据工具修正，不是生产求解器问题。
- 新验收工具 37 项小型测试通过（0.28 秒），不启动真实求解；Ruff 检查与格式检查通过。实际运行前，验收工具、新观察工具、原质量预检及参考身份聚焦 131 项通过（2.10 秒）。
- 扩展检查旧观察工具测试时出现 2 失败、141 通过（2.39 秒），已在未修改的 `3564277` 导出独立复现该文件 2 失败、10 通过（0.82 秒）：旧观察器只累计旧阶段的 3 次检查，实际新流程为 8 次，因此拒绝生成不完整补充证据。测试现明确区分旧阶段正例与新流程拒绝反例；原计数/轨迹/文件哈希断言保留，新增反例要求原六文件不变且不生成旧补充文件。历史工具及产物不改，新流程成功观察仍由步骤 6 工具验证；修订后回归待真实连续运行结束后执行。
- 定位上述测试时，两次只读检索误用了不存在的测试/契约文件名，退出 2；随后通过 `rg --files` 找到实际文件完成核对，不涉及代码或数据变更。
- 上述测试修订后，含旧观察器兼容边界的聚焦 144 项通过（2.56 秒）；Ruff 检查通过，格式检查指出该测试文件需整理，按原格式化器整理后 3 文件格式检查通过。没有修改任何原观察脚本或削弱正式门槛。
- `determinism/acceptance_report.json`：连续 3 次通过，退出 0；外层耗时依次 167.363644542、167.495808500、167.889500792 秒。全部为 200000 次候选检查后额度停止，七级 `(0,0,0,0,10912,600,22)`，质量门、两层审计、身份保护及稳定语义比较均通过，无首个差异。
- 三次公开结果身份均为 `3d1689274bfa5f06d30411613e2fccdc14a9adc32464d9f4e7e8163fe0f04f67`，轨迹均为 `aa9b16a7713d9f00e36c741600c483be9a5bcbbb6772e1d03caa850f122c12ae`，也与步骤 6 一致。验收运行器 SHA-256 为 `7dbb123c71bce9b92fe9739be22de08c1e55d97ec4a72a1e0274ff941a1a035e`；执行中未修改。
- 首批 `performance/` 完整保存 42 行，脚本返回 0、统计公式通过；最终核验确认系统休眠重叠，因此不将该批直接标为正式性能通过。[休眠证据与整组复测授权](environment_interruption.md)说明差额、结果分布及原始记录保留边界。用户确认后，新批使用独立目录 `performance_awake_rerun/`。
- 首批结束后，共享树完整集成测试 220 项通过、1 个可选绘图项跳过（2.82 秒）；已有绘图库环境补验两份图表测试 77 项通过（0.62 秒）。共享树规定累计 2947 项通过（176.06 秒），不与性能采样并行运行。
- 保持唤醒复测完整 42 行，退出 0；结果见上方正式结论及 `performance_awake_rerun/acceptance_report.json`。最终暂存树验证的实际命令、退出码、数量、耗时和树身份记入本项提交正文，避免提交内容自引用。本项不自动合并 `main`，不自动关闭原功能 21 的其他未完成验收。
- 独立只读核验三份采样记录的 3/42/42 行、45 份新结果的 270 个原始文件哈希、保护文件身份及统计公式，均一致；逐来源精确重量守恒和 21 处实际链边界复算通过。保持唤醒复测的 20 次轨迹及三份明细 CSV 相同，但检查数不同导致 20 个完整结果身份不同；首批第 19/20 个提前停止结果也完整保留。此复核不是重新求解或重新执行规则。
- 保持唤醒复测后，共享树完整集成 220 项通过、1 个可选绘图项跳过（3.01 秒），已有绘图库环境补验 77 项通过（0.63 秒）；规定累计 2947 项通过（185.13 秒），退出均为 0。Ruff 检查及格式检查 3 个变动 Python 文件通过，`git diff --check` 通过。2026-09-05 继续完成最终暂存树同范围验证及提交，真实采样仍为上述 2026-09-04 批次。
