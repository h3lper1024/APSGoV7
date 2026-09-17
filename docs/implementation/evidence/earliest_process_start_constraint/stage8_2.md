# 阶段 8.2：月计划改用订货量

## 边界与实现

用户在阶段 8.1 执行中追加订货量切换并取消全量测试。前端开始 `62f115b`，提交 `edb6b73`（`#feat 月计划按订货量求解并核验回写重量`）；后端开始 `e7d29d5`，仅新增用时专项测试及本项文档，不改后端用时公式或接口字段。

- Web 导入、提交和回写重量核验使用 `orderQuantity`；旧步骤可从明确的原始订货量列取得。缺失、无效或非正数禁止求解，不退回欠交或通用重量。
- `coatingShortage` 与原始列不被改写。新导入 `weight` 为订货量；返回真实片段的 `weight` 为实际片段重量，来源合计必须等于订货量。
- 请求仍为既有 `orders[].weight`。后端 `_order_timing` 用该重量计算生产用时，因此链重、重量规则和时钟使用同一口径。C# 等未修改客户端不能据此视为也已切换。
- 不修改历史步骤的现有结果/日期，不增加等待；模板仅增加第 30 列“订货量”，三条演示值 120、150、100 吨，旧 29 列/4 行逐值回读一致，渲染核验通过。

## 必要验证

前端共享树及精确树 `65a70fe2888f96fe705379bd799d03df9c213af6`（`/tmp/apsgo-order-quantity-check-9gNkEj`）：

```bash
node --experimental-strip-types --test tests/orderQuantity.test.ts tests/api.test.ts tests/demo.test.ts tests/lineEligibility.test.ts tests/earliest-start-api.test.ts tests/business-writeback.test.ts
npm run build
```

两树各 31 项通过，测试退出 0；类型检查/Vite 构建退出 0，仅有已有的大文件分包提示。覆盖旧步骤原始字段、新导入、非法/缺失数量、拆单合计以及错误总重量拒绝。首轮新增拆单测试漏填契约版本，修正测试响应后通过，没有放宽生产校验。模板新增列使用表格工具编辑并检查，原始列保留。

后端共享树：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/service/test_month_delivery_integration.py -k submitted_order_quantity -q
```

退出 0，1 项通过 / 10 项未选中，0.16 秒。宽 1000 mm、厚 1 mm、速度 100 m/min 时，94.2 吨为 2 小时，47.1 吨为 1 小时，重量改变使请求身份改变。

最终代码/测试树 `1bd42876b462313b44f531819684315d327374ed` 导出到 `/tmp/apsgov7-quantity-final-blJpOd`，同例复验 1 项通过 / 10 项未选中、0.24 秒，退出 0；干净导出残留检查通过。精修源文件及其 9 项测试与阶段 8.1 已测版本逐字一致，未重复 Numba 编译或全量测试；此后只补本文记录，最终提交身份由 Git 历史确定。

## 未执行与保护

不运行全量测试，不运行新的真实 1 月完整排程，不宣称提前开工已经降至某个数量。服务/规则库/配置均未操作。前端六份既有文件差异和后端三份已有差异均未纳入提交。生效需要更新 Web 和后端代码后由用户刷新/重启并重新求解。
