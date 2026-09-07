# 阶段 6：C# V7 专属配置和月计划求解客户端

## 1. 完成边界

- C# 基线：`codex/v7-rule-client-integration@4b7668c70935282a04222d06e73d6a3bbc478e41`。
- C# 提交：`ac5f6b78125fcbf1ce43f1e3123c443c68f4f2d1`，树 `8836dc1dfd18c069ef7dd26f4c2920c8b7b7cf22`。
- 平台：macOS / Darwin 27.0.0 arm64；Python 3.10.18，Conda 环境 `aps_3.10.18`。
- 本阶段只建立 V7 共享配置、月计划传输对象和异步客户端；不接页面命令、不构造或回写 `SchedRecord`，不改 V3 客户端和服务。
- 用户已有 `licenses.licx`、根 `.gitignore` 未进入提交；配置地址保持 `http://192.168.4.42:8001`。

## 2. 实现结果

1. `ApsgoV7Configuration` 严格读取 V7 基础地址、月计划求解路径和 240 秒客户端超时；规则客户端只读取基础地址，不依赖求解专属键。
2. `ApsgoV7SchedulingApiClient` 完整声明请求、响应、结果行、拆单谱系、虚拟材谱系和错误对象，JSON 小数使用 `decimal`，请求和响应任务标识必须一致。
3. 客户端核对响应活动规则版本等于请求的 `expected_active_version_id`，完整接收所有 rows；网络请求异步、支持 `CancellationToken`，不自动重试。
4. HTTP 422 按响应外形区分协议/准备错误对象和 `failed/input_invalid` 完整求解结果；可空流程诊断 `field_path` 与 Python 契约一致。
5. `App.config` 和项目文件随改动统一为 UTF-8 无 BOM；新增源码均恰好注册一次。

## 3. 验证

运行：

```bash
cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  docs/implementation/evidence/apsgo_v7_month_scheduling_and_grade_preparation/stage_06_csharp_client/verify_client_contract.py \
  --csharp-root /Users/miles/dev/dev-cs/aps-code-0806
```

结果为 `stage_06_csharp_client_contract: pass`。同一检查也对 C# 精确暂存树的干净导出执行通过；另行验证两份 XML 可解析、五份修改源码/配置均为 UTF-8 无 BOM，`git diff --cached --check` 通过。

固定请求覆盖 UUID、中文、`null` 分类和多位小数，并由当前 Python `loads_month_solve_request()` 成功解析。响应侧因当前 macOS 没有 `dotnet/msbuild/xbuild/csc/mcs/mono`，以逐类型 `JsonProperty` 精确集合与分支静态检查代替运行期反序列化；**Windows/.NET Framework 4.7.2 构建、真实 C# JSON 往返和页面运行仍未验证，不把静态检查称为已联调。**

## 4. 下一步

阶段 7 新建 GQGA4 专属求解服务：从现有月计划数据构造请求，POST 前即时读取活动规则版本，完整验证结果后以短事务原子回写。该阶段再处理来源覆盖、重量守恒、可发布状态、告警元数据和页面命令切换。
