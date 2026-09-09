# 阶段 8：C# V7 规则客户端验证

## 1. 结论

GQGA4 月计划规则页面所需的 V7 专用客户端已在 C# 工程独立提交：

- C# 分支：`codex/v7-rule-client-integration`
- 实施前提交：`d207cc2`
- 实施提交：`cefc3f1 #feat 建立月计划V7规则客户端`
- 精确暂存树：`9fc6b607e8ff0b6b7f5bdf48725d9f6fd56a788a`

本阶段只新增 `SchedApp/ApsgoV7RuleApiClient.cs`，并在旧式 `SchedApp.csproj` 中登记一次；未修改 `PipelineV3ApiClient`、GQGA4 页面、`App.config`、数据库或业务数据。

## 2. 客户端边界

- 只提供一次 GET 和一次 POST 所需的数据对象与异步方法，不创建接口、工厂、缓存或通用 HTTP 框架。
- 两个端点是固定常量，只有端点最后一段使用 camelCase；JSON 字段全部保持 snake_case。
- POST 顶层只允许 `save_operation_id`、`expected_active_version_id`、`rules`、`virtual_prototypes`、`remark`；单条规则只允许 `rule_id`、`enabled`、`parameters`。
- `BACKEND_ALGORITHM_URL` 必须由现有 `App.config` 提供，当前发布默认值仍为 `http://127.0.0.1:8001`；缺失、空白或非法绝对地址明确失败，不增加第二个地址键。
- 规则接口使用固定 60 秒超时。主动取消保持取消语义；服务超时、连接失败、空响应和无效 JSON分别给出明确错误。
- 非成功响应解析 V7 统一 `error` 对象，保留 HTTP 状态、错误代码、期望/当前活动版本和字段级问题，供页面区分 `409` 并发冲突与 `422` 字段错误。

## 3. 十进制与缺失字段

Json.NET 明确设置 `FloatParseHandling.Decimal` 和 `DateParseHandling.None`。已知业务数值使用 `decimal` / `decimal?`，规则参数及原型属性使用 `JObject`；POST 前递归拒绝由 `double` 或 `float` 构造的小数节点，避免修改后回传二进制浮点近似值。

服务端必返字段均使用 `Required.Always`；字段必须出现但允许 JSON `null` 的版本、宽厚温度及错误主体使用 `Required.AllowNull`。这样，缺失的 `enabled`、`idempotent_replay` 或数值不会被静默解释为 `false` 或 `0`。

.NET `decimal` 不能表示服务端通用解析器允许的任意大 `Decimal`，例如 `1e10000`。客户端对超出自身表示范围的值明确失败，不回退 `double`；当前正式 GQGA4 规则和 27 个原型均位于可表示范围内。若未来业务确需超出范围，应先形成跨端统一数值边界，不能只在客户端增加隐式转换。

## 4. 实际验证

| 检查 | 结果 |
|---|---|
| V7 临时数据库 GET | HTTP 200，17 条规则、16 条启用、7 项评分、27 个虚拟原型 |
| V7 临时数据库 POST | HTTP 200，返回完整活动快照及 5 个保存结果字段 |
| 旧版本并发提交 | HTTP 409，`active_version_conflict` 和版本字段完整 |
| 缺少固定规则 | HTTP 422，字段问题路径完整 |
| POST 字段静态核对 | 顶层 5 项、单规则 3 项，无服务端只读字段 |
| 小数静态核对 | `FloatParseHandling.Decimal`、原型 `decimal` 字段和出站二进制浮点拒绝均存在；客户端中无 `double` |
| 工程与 XML | 新文件在 `SchedApp.csproj` 恰好登记一次；项目、包配置及应用配置 XML 均有效 |
| 编码与差异 | 新 C# 文件为 UTF-8 无 BOM；`git diff --check` 通过 |
| 用户本地文件 | 未跟踪 `.gitignore` 未修改、未暂存，SHA-256 仍为 `8d83935bf7d0b0cefa4909e338427b956c6db20dc360204f32421da3ea97cfc9` |
| V7 HTTP 专项 | `tests/service/test_rule_http_api.py` 共 42 项通过，1.16 秒，退出码 0 |
| V7 共享树累计回归 | `tests/architecture tests/api tests/app tests/core tests/service` 共 3159 项通过，172.22 秒，退出码 0 |
| V7 精确暂存树 | 树 `82b9b801c46f6ca994c0ab69d68f90b29a9b7aa5` 的全新导出通过 `clean_export` 残留检查、42 项 HTTP 专项和 3159 项累计回归；累计回归 172.98 秒，退出码 0 |
| V7 精确暂存树构建 | 同一导出的 `compileall`、源码包和 wheel 构建通过，退出码 0 |

静态契约检查可重复运行：

```bash
/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_rule_setting_api/stage_08_csharp_rule_client/verify_client_contract.py \
  --client-root /Users/miles/dev/dev-cs/aps-code-0806
```

服务端真实成功、并发和字段错误契约由 `tests/service/test_rule_http_api.py` 的临时 SQLite 路径生成并与上述静态检查交叉核对；没有写入默认 V7 规则数据库。

## 5. 尚未完成的环境验收

当前 macOS 没有 `dotnet`、`msbuild`、`csc`、Mono 或 NuGet 命令，也没有该工程所需的 Windows DevExpress 构建环境。因此本阶段**没有声称 C# 编译或运行时反序列化已经通过**。最终必须在 Windows、Visual Studio 2019、.NET Framework 4.7.2 和 DevExpress 20.1.3 环境执行：

```powershell
nuget restore AutoSchedule.sln
msbuild AutoSchedule.sln /m /t:Build /p:Configuration=Debug /p:Platform="Any CPU"
```

页面 Designer 与交互验证属于后续页面加载和保存阶段；Windows 构建是最终联调关闭前的强制门禁。
