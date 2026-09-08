# APSGo V7 Windows 发布包

本目录是 Windows x64 的目录式发布包。请完整保留 `APSGoV7Service.exe`、`_internal/`、`config/`、`data/`、两个启停脚本和 `release_manifest.json`，不要只复制 EXE。

## 首次启动

双击或在命令行运行 `start_apsgo_v7_service.bat`。首次启动仅在文件缺失时执行以下初始化：

- 将 `config/apsgo_v7_service.example.yaml` 初始化为 `config/apsgo_v7_service.yaml`；
- 将 `data/apsgo_v7_rules_seed.sqlite3` 初始化为 `data/apsgo_v7_rules.sqlite3`。

现场 YAML 和运行数据库一旦存在，启动脚本不会覆盖。服务在当前窗口前台运行，关闭窗口会结束服务。

## 配置与停止

监听地址、端口、数据库路径和求解预算只从 `config/apsgo_v7_service.yaml` 读取。需要其他电脑访问时，可将 `listen_host` 配为 `0.0.0.0`，并由部署人员按现场安全要求配置 Windows 防火墙；当前接口没有登录鉴权，不应直接暴露到不受信任网络。

运行 `stop_apsgo_v7_service.bat` 会按当前目录中 EXE 的绝对路径精确停止本实例，不会按端口或名称停止其他目录中的实例。

## 数据保护

- `data/apsgo_v7_rules_seed.sqlite3` 是只读初始化种子，不是现场运行库；
- 保存并启用规则只写入 `data/apsgo_v7_rules.sqlite3`；
- 升级或重新解压时，不得覆盖现场 YAML 和运行数据库；
- 维护前先停止服务，并备份现场 YAML 与运行数据库。

`release_manifest.json` 记录发布文件、构建环境和数据库身份，可发现文件与清单不一致；它不是数字签名，不能防止文件和清单同时被恶意替换。

## 常见失败

- 提示端口被占用：停止占用 YAML 配置端口的程序，或修改现场 YAML 后重启；
- 提示数据库不可写：将完整发布目录放到运行账号可写位置，不建议放在普通用户不可写的 `Program Files`；
- 提示缺少文件：重新部署完整目录，不要从其他版本单独补 EXE 或 `_internal` 文件；
- 启动失败：保留控制台错误信息、`release_manifest.json`、现场 YAML 和运行数据库，以便定位；不要删除或用种子覆盖运行数据库。

## 求解不可发布时的诊断资料

新版示例配置默认开启文件诊断。已有现场 YAML 不会被升级覆盖；需要在现场配置中手工补入：

```yaml
diagnostics:
  enabled: true
  output_directory: ../diagnostics
```

重启后，文件位于 `<发布目录>\diagnostics`，与从哪个盘符或目录启动无关。
所有日志行带本机日期、时间、毫秒和时区；求解另记录请求累计耗时、阶段耗时及实际采纳动作。
`service.log` 为共享日志，单文件 10 MiB，保留 3 个轮转文件。
每次有效求解另存 `runs\<请求标识>\<运行时间_唯一后缀>\`，同请求重试不覆盖旧资料。

人工排查顺序：

1. 通过最终日志的 `diagnostic_directory` 找到本次目录。
2. 打开 `diagnostic_summary.json`，检查 `bound_result.result.stop_reason`、`issues` 和双审计报告；不可发布不一定是规则违规，也可能是超时或异常。
3. 在 `candidate_rows.csv` 定位违规链和订单，对照 `prepared_request.json` 的真实输入、规则与策略，再看 `solve.log` 中的初始状态和搜索过程。
4. `request.json`、`response.json` 分别保留本次原请求与实际生成的响应。失败前尚未形成候选或绑定输入时，相应文件不会伪造。

CSV 是诊断候选，不是获准回写的正式结果。Excel 使用“数据导入”、UTF-8 编码，订单号指定文本；
文本公式前缀已保护，原始值保留在 JSON。搜索评价位于 `diagnostic_candidate.search_evaluation`，正式发布评价位于 `release.evaluation`，两者不得混称。

显式启用但启动时目录不可写，会打印带时间的错误并停止启动；运行期间单个文件失败只影响诊断，不改变求解结果。
此时看 `diagnostic_write_failed`、摘要内 `write_failures`；强杀进程允许留下不完整记录。
完整线程收尾总耗时在共享日志 `month_solve_worker_finished`，不包含网络传输或 C# 回写。

将 `enabled` 改为 `false` 并重启可关闭文件输出，旧证据保留。目录含业务数据，只在本地保存、人工归档清理，
不要放入正式发布包或上传到未经授权的位置。若回退到不支持诊断块的旧程序，先备份现场 YAML 并删除其中新增的 `diagnostics` 块，
再恢复整套旧程序；运行数据库及诊断目录均保留。
