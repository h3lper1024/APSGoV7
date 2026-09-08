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
