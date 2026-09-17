# 阶段 6.2：月计划订单、日期与诊断响应

- 2026-09-17，实施前 `48e77e8`；平台、Conda、数据库/YAML 和前端用户文件保护同 [6.1](stage6_1.md)，摘要复核一致。不操作正式库、配置或用户服务。
- 从两层审核派生 `audit_summary.integrity_passed`，保留原综合 `passed`；独立核心审核的 `writeback_blocking_violation_count` 即使提前结果被拦截也照实返回。没有可信审核时数量为 `null`，业务合规标记同样为 `null`，不伪造零违规。
- 带普通业务违规的新状态返回完整订单、评分、指标、违规、正式交期报告和同一结果的 `latest_dates`；正式日期仍要求 `audited_release` 和原请求/版本/结果绑定，不从诊断候选生成可写日期。
- 列表增加可选扩展 `business_warning`，覆盖非宽厚温节点/边/期违规；链及链内片段仍带警告。支持对象评价和数值评价两种既有主体编码；数值宽厚温边定位到承接订单，方案级全局违规保留完整明细，不随意归咎某单。未知非全局主体仍拒绝，不能静默丢告警。
- 日志新增正确性与最早开工阻断数；诊断 JSON/CSV 和 HTTP 保留同一真实状态/违规。无新地址、表、参数、依赖，不改 v1/v2/v3 请求字段；`scheduling.py` 与日期接口原有委托/绑定无需重复实现。

## 必要验证

执行前缀同 6.1，末尾 `-q --tb=short`，以下 7 文件共 112 项：

```text
tests/service/test_business_violation_writeback.py
tests/service/test_month_scheduling.py
tests/service/test_earliest_process_start_integration.py
tests/service/test_month_delivery_integration.py
tests/service/test_month_scheduling_http.py
tests/service/test_solver_diagnostics.py
tests/service/test_diagnostic_lifecycle.py
```

新增 11 项覆盖真实规则评价的普通违规/混合提前 HTTP 回包、数值边违规的日期释放、所有主体范围和全局警告；均使用临时库和进程内测试客户端，不监听用户端口。

共享树首轮 **110 通过、2 个旧诊断预期失败，154.16 秒**：原“窄钢违规无订单、CSV 不可发布”的预期与批准语义不符。更新后聚焦诊断日志和实际产物 **5 项通过，0.65 秒，退出 0**；日志保留 `core_audit_passed=False`，同时 `integrity_passed=True` / 阻断数 0。启动验证时曾误写不存在的 `test_diagnostics.py`，退出 4、未执行测试，已改为上列实际路径。

精确提交树按以上 112 项一次复验及干净导出残留检查收口，实际结果和树身份见本项提交正文。共享残留检查仍只有已记录 8 个旧文件缺失，不修改清单。不把接口小例当作真实 GQGA4 可回写；下一项 6.3 Web 接入，真实对照归 6.4。
