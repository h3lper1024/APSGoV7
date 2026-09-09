# APSGo V7 Windows 发布包

本目录是 Windows x64 的目录式发布包。请完整保留 `APSGoV7Service.exe`、`_internal/`、`config/`、`data/`、两个启停脚本和 `release_manifest.json`，不要只复制 EXE。

## 构建人员：数值桥接依赖与验证

以下命令在 **Windows 源码仓库根目录**执行，使用 Conda `aps_3.10.18`（Python 3.10.18 x64），不是要求现场运行人员安装 Python：

```powershell
conda run -n aps_3.10.18 python -m pip install -r release/requirements-build.txt
.\release\build_exe.bat -CheckOnly
.\release\build_exe.bat
```

该文件包含PyInstaller和本次数值桥接新增依赖的固定版本；其他服务依赖仍按项目安装说明准备。构建脚本只检查版本，不自动安装；`-CheckOnly` 只做前置检查，不生成 EXE 或证明运行通过。已有本脚本输出目录时，确认保留需要的旧包后，再按原约定使用 `-Clean` 重建。

NumPy 保存桥接数值，Numba 在实际首次桥接时编译计算循环，llvmlite 提供编译运行库；`pyinstaller-hooks-contrib` 是打包工具收集这些依赖所用的辅助规则。发布清单记录四项精确版本，不能混用其他包的运行库。首次请求含编译成本，服务启动及读取规则不预热编译，Numba不写磁盘编译缓存。

完整构建还会在**临时发布副本**执行原有规则读写/重启检查，然后通过月计划接口实际运行单桥和双桥，各包含首次、同进程重复及重启后请求。检查预期订单、虚拟原型、重量和双审计，并核对 `solve.log` 中真实 `nopython=True`（已执行编译后的数值函数）记录。它会用 Windows 文件访问权限将临时程序目录设为不可写，配置、数据库和诊断使用目录外可写副本；子进程移除 Python/Conda 路径，验证不依赖外部解释器。

成功时控制台输出 `evidence_directory`，保留该临时目录中的请求、响应、诊断、清单及权限检查日志；仅清理临时程序副本。失败也保留现场，先查看第一条错误。不要通过全量收集源码或放宽文件禁入检查跳过缺依赖问题。

以上小输入用于验证发布包可运行，**不等于真实订单性能验收，也不等于已在另一台无 Python 的电脑验证**。正式复测仍需保留完整包、清单、现场 YAML、匹配的规则数据库及新诊断。macOS 源码测试不能代替 Windows 实包验证。

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
