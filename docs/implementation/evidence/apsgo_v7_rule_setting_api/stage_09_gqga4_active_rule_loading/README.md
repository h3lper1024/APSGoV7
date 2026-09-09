# 阶段 9：GQGA4 活动规则页面加载验证

## 1. 结论

GQGA4 月计划规则页面已改为只执行一次 V7 活动规则查询，并在 C# 工程独立提交：

- C# 分支：`codex/v7-rule-client-integration`
- 实施前提交：`cefc3f1`
- 实施提交：`8b5a883 #feat 接入GQGA4活动规则查询`
- 精确暂存树：`35490b06a9378a070cabcde7aa7654ec68877e53`
- 干净导出：`/private/tmp/aps-code-stage9.sIE0mf`

本阶段未接通保存。页面主按钮、旧“保存草稿”和“启用草稿”均不可用；只有阶段 10 的单次 V7 POST 完成后，主按钮才允许启用。

## 2. 实现边界

- `Shown` 事件只调用一次 `GET /api/v1/rule-sets/GQGA4/default/month/getActiveRules`，不再进入 V3 目录、当前规则、表单规范或预览加载链。
- 按大小写敏感的固定 `rule_id` 和顺序校验 17 条规则，完整缓存响应、规则索引、虚拟材料原型及活动版本。
- 查询前清空成功状态并禁用编辑和保存；查询、映射或窗口生命周期失败时不使用页面默认值冒充活动配置。
- 窗口关闭会取消在途查询并释放 V7 客户端；关闭、取消或加载失败不再复制排程记录，也不推进月计划步骤。
- 普通逆宽、虚拟材料连接宽度、逆宽次数、连续逆宽开关、厚度基准/区间/兜底容差、温度、链重、连续虚拟材料、虚拟比例、未来填充及四项固定规则均有明确回显位置。
- 加载路径的业务小数、厚度表和共享虚拟规格编辑器统一使用 `decimal`，不经过 `double`、整数取整或三位小数截断。
- 停用规则允许服务端合法的空参数或空数组。前端只校验控件显示所需的基础形状，最终业务校验仍由 V7 服务端负责。
- 旧虚拟规格控件只能表示“统一单重的宽度与厚度完整组合”。合法活动原型无法无损投影时，页面隐藏该编辑器并提示只读保留；完整原型仍留在活动响应快照，不能因此判定整页加载失败。

共享 `VirtualSphcSpecificationsEditor` 和对话框仅将内部数值从 `double` 改为 `decimal`；其他产线使用的公开 `BuildSetting(): JObject` 和 `ApplySetting(JToken)` 入口未改变。全仓调用扫描未发现依赖原内部数值类型的其他调用者。

## 3. 实际验证

| 检查 | 结果 |
|---|---|
| 阶段 8 客户端契约复测 | 2 条固定路由、活动响应 14 字段、POST 请求 5 字段、保存响应 19 字段及 Decimal 传输均通过，退出码 0 |
| 阶段 9 页面静态检查 | 1 次 GET、17 条规则、完整快照、取消边界及排程步骤保护通过，退出码 0 |
| 新增兼容反例 | 不可投影原型非致命、停用规则空数组、厚度较厚侧与兜底容差、精确 Decimal、虚拟连接宽度映射均通过 |
| 精确暂存树复测 | 在树 `35490b06a9378a070cabcde7aa7654ec68877e53` 的全新导出中重复执行阶段 8、9 检查，均退出 0 |
| V7 共享树累计回归 | `tests/architecture tests/api tests/app tests/core tests/service` 共 3159 项通过，171.27 秒，退出码 0 |
| V7 干净导出累计回归 | `/private/tmp/apsgo-v7-stage9-verified.zFaLSF` 中同一范围 3159 项通过，173.60 秒；先测试后构建，退出码 0 |
| XML 与差异 | `SchedApp.csproj`、`App.config` 通过 `xmllint`；`git diff --check` 通过 |
| 编码 | 5 个修改文件均为 UTF-8 无 BOM、LF |
| 用户本地文件 | 未跟踪 `.gitignore` 未修改、未暂存，SHA-256 仍为 `8d83935bf7d0b0cefa4909e338427b956c6db20dc360204f32421da3ea97cfc9` |
| 数据变化 | 无数据库、规则版本、排程记录或业务数据写入 |

静态检查可重复运行：

```bash
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_09_gqga4_active_rule_loading/verify_page_loading.py \
  --client-root /Users/miles/dev/dev-cs/aps-code-0806
```

## 4. 尚未完成的环境验收

当前 macOS 27.0 arm64 没有 `dotnet`、`msbuild`、`csc`、Mono、NuGet 或 DevExpress 构建环境，因此本阶段没有声称 C# 编译或 WinForms 页面实测通过。最终仍须在 Windows、Visual Studio 2019、.NET Framework 4.7.2 和 DevExpress 20.1.3 环境验证：

```powershell
nuget restore AutoSchedule.sln
msbuild AutoSchedule.sln /m /t:Build /p:Configuration=Debug /p:Platform="Any CPU"
```

页面手测至少覆盖：成功加载、查询失败、关闭时取消、不同 DPI 下新增控件无遮挡，以及非笛卡尔虚拟原型的只读提示。

验证过程中曾在另一个全新导出中先构建后测试；构建生成的
`src/apsgo_scheduler.egg-info` 被源码边界测试正确拒绝，结果为 3158 通过、1 失败。
该结果属于验证命令顺序错误，不是项目源码失败；上述正式干净导出改为先运行累计测试、
再执行构建，并保留原失败日志 `/private/tmp/apsgo-v7-stage9-final-validation.log`。
