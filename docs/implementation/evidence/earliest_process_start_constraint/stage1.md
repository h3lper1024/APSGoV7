# 阶段 1：公共时间输入与数值下界

- 实施前：`6b61f22`；平台 macOS，Conda `aps_3.10.18`。
- `OrderTimingInput` / `OrderDeliveryTiming` 增加可缺省 `earliest_start_at`；有值仅接受显式北京时间、秒级规范字符串。公共请求准备和诊断回放共用；旧输入不输出缺省新键。
- 原单数组新增 `earliest_start_ms` / `has_earliest_start`，按本次开始时间换算一次。数组只读，拆片/私有候选通过原来源访问，任务身份包括已提供的新时间。缺省旧任务身份保持。
- 本阶段不注册或启用新规则，启用后的必填及执行模式校验在阶段 2；未写正式库、未改服务配置或运行服务。

## 验证

环境：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src`，Python `/Users/miles/anaconda3/envs/aps_3.10.18/bin/python`，`pytest -p no:cacheprovider`。

集中范围：

```text
tests/core/test_earliest_process_start.py
tests/core/test_delivery_timing.py
tests/core/test_numeric_state.py
tests/app/test_delivery_preparation.py
tests/core/test_numeric_private_resources.py
tests/core/test_numeric_boundary_audit.py
```

- 新增 15 项独立输入测试通过（0.57 秒）。初次集中测试 68 通过/3 失败（166.27 秒）：新增回放样例漏填指纹已修正；旧服务小例的 12 秒时限被首次编译消耗。未放宽原测试断言或正式时限。
- 同解释器先用 `tests.core.test_numeric_boundary_audit.numeric_request()` 的零候选小例预热（仅预热副本时限改为 300 秒/10 秒收尾），然后执行上述原测试：**71 通过，35.67 秒，退出 0**。预热返回成功且因零候选额度停止。
- 精确暂存树重复上述流程，结果记录在本阶段提交正文；并检查干净导出的残留约束。
- 共享残留检查仍报告既有 8 个旧 V6 文件缺失，不修改清单；不将此既有问题写成检查通过。

尚未验证真实求解；真实逐单最早时间仍待取得，不能用本阶段边界测试替代阶段 6。
