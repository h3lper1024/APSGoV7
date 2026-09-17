# 阶段 6.1：正确性审核与最早开工回写门槛

## 范围与基线

- 2026-09-17，macOS，实施前后端 `codex/earliest-process-start-constraint@50b2b3d`；Conda `aps_3.10.18`。
- 新增 `publishable_with_violations`，但只在完整正确性审核通过且最早开工阻断数为零时签发。保留 `passed` 原综合语义，不改规则等级、明细、评分或搜索。
- `integrity_passed` 由无缓存审核的结构/授权/重算及资源事实得出；阻断数按实际启用的 `EarliestProcessStartRule` 类型和身份计算，不依赖固定 id 或提示文本。缺失/未完成时为 `null`。
- 核心收尾、核心/公共不可变结果、组装与应用映射审核共用状态判断；独立审核绑定和阻断计数在签发前复核。未知错误、取消、审核缺失、超时和身份差异仍拦截。
- 数值/非数值审核共用 `final_audit._outcome()`，数值求解共用核心结果签发；因此不复制审核算法，也不需要改 `_numeric_audit.py` 或 `app/service.py` 的既有委托流程。
- 旧允许欠重状态及其原 `confirmation_required` 保持；新状态不增加人工确认流程。月计划 HTTP/日期与 Web 接入属于后续 6.2/6.3，本提交尚不可单独作为前端部署完成。

## 验证

命令前缀：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider`，后接下列顺序，末尾 `-q --tb=short`。

```text
tests/app/test_earliest_process_start_flow.py
tests/core/test_solver_policy.py
tests/core/test_solver_status_matrix.py
tests/core/audit/test_final_audit_without_cache.py
tests/api/test_result_status_matrix.py
tests/app/test_result_assembler.py
tests/app/test_result_contract_audit.py
tests/app/test_solve_request_service.py
tests/core/test_numeric_boundary_audit.py
tests/core/test_earliest_process_start_rule.py
tests/app/test_business_violation_writeback.py
```

新增三类判定及混合错误共 21 项；复用既有毫秒/等号/拆片时钟、来源/重量/物理属性、谱系、授权、审核绑定、取消和超时测试。真实固定数值方案覆盖普通连接违规、提前违规及二者并存；允许结果完成公共映射审核，原违规/评分/资源引用保持。

初次验证发现旧状态断言和审核载体夹具未同步，按批准口径更新；新污染样例因值类型提前拒绝重单，调整为先建立正常方案再故意污染，验证独立审核。首次编译触发两个原有短时限求解小例超时，因此同一测试进程先运行已有 600 秒测试预算的完整小例预热，再执行短时限用例；不改生产时限或测试通过条件。共享树集中 462 通过、5 个新断言失败（176.13 秒）：欠重属于允许偏差，不应强断言为禁止；保留真实等级并检查实际业务审核代码，随后 5 项补验通过（0.70 秒，退出 0）。这不是一次全绿；最终精确树以相同 467 项一次复验收口，结果见本项提交正文。

## 保护与下一步

- SQLite SHA256：`6da52e57362cc6dc5bed561f952b79682535e415d7f9b5b6aed18723e90ee228`；YAML：`04cb4998cd3d8100a4f7f2ff9c62e10111ced4ae8f827df841507fa535c2f0c0`。不写库、不纳入既有数据库差异、不操作服务。
- 前端仍 `master@74235f4`，6 份用户差异摘要 `30a35d5f85ae20afd849821a5647d7693a795f76d5c3aaadb3baee3061b264b6`，不改动。
- 共享残留检查仍为已记录的 8 个旧 V6 缺失文件，退出 1；不恢复旧产物、不修改冻结清单。精确树执行同范围测试与干净导出残留检查，实际树与结果记入提交。
- 下一项 6.2。未运行完整真实求解；原 6 单提前尚未修复，不能据本项声明该批数据可回写或进入阶段 7。
