# 阶段 10：GQGA4 规则保存并启用验证

## 1. 结论

GQGA4 月计划规则页面已接通一次性 V7 保存并启用流程，并在 C# 工程独立提交：

- C# 分支：`codex/v7-rule-client-integration`
- 实施前提交：`8b5a883`
- 实施提交：`4b7668c #feat 接入GQGA4规则保存并启用`
- 精确暂存树：`ccb9562eb43eaf26bddcd3b31a81fa181f1b4174`
- C# 干净导出：`/private/tmp/aps-code-stage10.BH7Ios`

本阶段完成的是代码与静态契约验证。当前 macOS 没有该旧式 WinForms 工程所需的 .NET Framework、MSBuild、C# 编译器和 DevExpress 环境，因此不把静态检查称为 Windows 构建或真实页面联调。

## 2. 实现边界

- 主按钮改为“保存并启用”，每次保存只调用一次 `POST /api/v1/rule-sets/GQGA4/default/month/setActiveRules`；GQGA4 页面不再调用 V3 的规则目录、表单规范、预览、保存草稿或单独启用。
- 请求直接从最近一次成功活动响应克隆完整有序的 17 条规则，只覆盖页面可见字段。规则类型、名称、范围、版本、固定七级评分、允许最终偏差和指纹仍由服务端控制。
- 每次新的人工保存生成一个 UUID（通用唯一标识，即本次保存的唯一编号）；网络、超时、HTTP 5xx 或成功响应身份异常时保留同一请求，用户点击“重试保存”原样重放，不产生第二个逻辑操作。
- `409` 并发冲突默认保留编辑值，只有用户明确同意才重新查询；`422` 服务端字段错误恢复编辑区并聚焦首个可识别控件；其他明确客户端错误允许修正后发起新的保存。
- 成功响应必须与请求的操作标识和基础活动版本一致；保存版本仍活动时，还必须满足活动版本标识及继承版本一致。被后续版本替代的幂等重放只刷新当前活动快照，不返回 `DialogResult.OK`，不会推进排程步骤。
- “取消修改”只恢复最近一次成功 GET/POST 的本地快照，不访问网络。保存结果不确定时也可用它放弃待重试请求。
- 外层排程命令改用两参数窗体构造；仍只在活动保存成功并返回 `DialogResult.OK` 后复制排程步骤记录。

共享 `PipelineV3ApiClient` 文件和其他产线页面没有修改。本阶段没有新增客户端接口、工厂、缓存或重试后台任务。

## 3. 虚拟材料原型语义

| 页面操作或活动快照 | POST / 回显结果 |
|---|---|
| 关闭“使用虚拟 SPHC” | 提交空 `virtual_prototypes`，新活动版本不允许生成虚拟材料 |
| 活动原型为空 | 页面显示为未启用，并预置宽度 `1000、1250、1500`，厚度 `0.4、0.5、0.6、0.8、1、1.2、1.5、2、2.5`，单个重量 `20` 吨，供再次启用 |
| 保留已有宽厚组合 | 保留原型标识、牌号、最小/最大温度和规则属性；统一单重按页面值更新 |
| 新增宽厚组合 | 生成 `virtual_sphc:{width}x{thickness}`，牌号 `SPHC`，温区为空，规则属性包含 `hot_roll_grade=SPHC`；不写软硬钢分类 |
| 非空快照无法由完整宽厚组合和统一单重无损表达 | 隐藏编辑器，保存时原样回传完整原型快照 |

共享虚拟规格编辑器的禁用摘要改为中性文字“不使用虚拟 SPHC”，避免误导为提交后仍会保留原型；其他产线的公开数据结构和保存逻辑未改变。2026-09-07 补正：原阶段提交创建的是空规则属性，新提交 `5c3bff8 #fix 补齐虚拟原型热轧牌号` 已按月计划 C# 回写契约补齐上述属性；既有原型的深克隆行为保持。

## 4. 实际验证

| 检查 | 结果 |
|---|---|
| 阶段 8 客户端契约复测 | 两条固定路由、GET/POST 字段及 Decimal 传输通过，退出码 0 |
| 阶段 9 页面加载复测 | 一次 GET、17 条规则、完整快照、Decimal、不可投影原型和关闭边界通过，退出码 0 |
| 阶段 10 保存静态检查 | 一次 POST、完整原序克隆、基础版本、同请求重试、409/422、非活动幂等、取消、虚拟原型及保存继承关系通过，退出码 0 |
| C# 精确暂存树复测 | 在树 `ccb9562eb43eaf26bddcd3b31a81fa181f1b4174` 的全新导出中重复执行阶段 8～10 检查，均退出 0 |
| 独立代码复核 | 构造调用、Designer 事件、异步关闭、冲突/重试和响应继承问题修正后，无剩余高、中优先级问题 |
| XML 与差异 | `SchedApp.csproj`、`App.config` 通过 `xmllint`；`git diff --check` 通过 |
| 编码 | 5 个 C# 提交文件均为 UTF-8 无 BOM、LF |
| 用户本地文件 | 未跟踪 `.gitignore` 未修改、未暂存；SHA-256 为 `8d83935bf7d0b0cefa4909e338427b956c6db20dc360204f32421da3ea97cfc9` |
| 数据变化 | 未启动真实 V7 服务，未创建规则版本，未修改数据库、排程记录或业务数据 |
| V7 共享树累计回归 | `tests/architecture tests/api tests/app tests/core tests/service` 共 3159 项通过，175.02 秒，退出码 0 |
| V7 精确暂存树 | `/private/tmp/apsgo-v7-stage10-verified.sHUIOJ` 通过干净导出残留检查、阶段 8～10 静态检查、同一范围 3159 项累计回归、`compileall` 及源码包/wheel 构建，退出码均为 0 |

三个阶段的静态检查可重复运行：

```bash
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_08_csharp_rule_client/verify_client_contract.py \
  --client-root /Users/miles/dev/dev-cs/aps-code-0806
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_09_gqga4_active_rule_loading/verify_page_loading.py \
  --client-root /Users/miles/dev/dev-cs/aps-code-0806
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_10_gqga4_save_and_activate/verify_save_flow.py \
  --client-root /Users/miles/dev/dev-cs/aps-code-0806
```

阶段 9 检查器只做了一项下游兼容修订：加载方法改为通过共用的“接受完整响应”方法执行映射和提交，且第 10 阶段已删除旧按钮，因此检查器跟随该真实调用链，不再要求历史按钮仅处于隐藏状态。

共享目录的残留检查仍被继承自旧 V6 的稳定残留清单阻塞：清单要求已不存在的 `.claude/settings.local.json`、旧 `dist/apsgo-*` 和 `src/apsgo.egg-info`。本阶段没有重建、删除或提交这些无关旧产物；精确暂存树的全新导出继续作为正式残留门禁。

## 5. 阶段 11 必须关闭的联调项

在 Windows、Visual Studio 2019、.NET Framework 4.7.2 和 DevExpress 20.1.3 环境执行：

```powershell
nuget restore AutoSchedule.sln
msbuild AutoSchedule.sln /m /t:Build /p:Configuration=Debug /p:Platform="Any CPU"
```

页面与真实服务至少验证：首次 GET；修改链重后 POST 并重新 GET；关闭虚拟材料得到空原型；从空原型重新启用得到 27 个标准原型；保留原型元数据；两窗口并发冲突；服务端字段错误定位；模拟响应丢失后以同一操作标识重试；不同 DPI 下按钮与提示无遮挡。
