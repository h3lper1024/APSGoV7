# 阶段 4：月计划接口、规则模板与诊断

## 实现边界

- 月计划同一 URL 支持 `v7-month-solve-v3`，订单可携带 `earliest_start_at`；仍严格拒绝未知字段，v1/v2 启用新规则时明确返回配置不匹配，不剥除字段重试。
- 新规则模板复用九项目标和既有规则，仅增加方案级空参数最早开工规则；原七级/九级历史模板不改写。沿用逐条保存、整体校验编译、版本冲突和幂等事务，无新数据库表、无启动时写库。
- 缺失下界启用时报前置错误；停用可缺省、有值仍解析。未发布结果的原始候选只进入 `diagnostic_violations` 和标明不可发布的交期报告；正式 `rows` 保持空，不生成 `latest_dates`。
- 提供下界但停用时，报告可统计实际提前量，并显式标明 `earliest_start_constraint_enabled=false`，不将统计误当硬违规。

## 验证

工作区实际编译模式：以下四个文件共 **47 项通过，153.49 秒，退出 0**。随后补充历史恢复与报告启停标记断言；历史恢复聚焦 **1 项通过，0.64 秒**；最终精确树按同四文件复验结果见本阶段提交正文。

```text
tests/service/test_earliest_process_start_integration.py
tests/service/test_month_delivery_integration.py
tests/service/test_month_scheduling.py
tests/service/test_month_scheduling_http.py
```

命令前缀：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider`。只使用临时 SQLite 和 FastAPI 隔离测试客户端，不占用用户服务。

覆盖：v3 有效/缺失/非法时间；v2 对新字段仍拒绝；旧契约与启用规则不匹配返回 409；启用缺失返回 422 顶层问题列表；合格九级结果能发布；提前结果不能回写；停用后缺省与带下界统计均能发布；保存回读、幂等、版本冲突、历史备份与恢复为新活动版本。

首次新增断言错误地按管理接口错误信封读取求解 422，核对既有求解契约后改为读取顶层 `issues`；未改变既有响应形状以迎合测试。小例测试参数不替代正式 40 万候选和约 300 秒搜索配置。

正式数据库、YAML 和用户服务不动。真实逐单下界仍待提供，接口测试不等于真实求解已发布；现场规则启用属于阶段 7 单独授权。
