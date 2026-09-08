# APSGo V7 Windows EXE 发布打包实施计划

## 1. 文档信息

| 项目 | 内容 |
|---|---|
| 版本 / 日期 | v0.3 / 2026-09-08 |
| 状态 | 阶段 0～1 已完成；下一项为阶段 2“建立 Windows 目录式 EXE 构建”；Windows EXE 尚未生成 |
| 实施基线 | `codex/rule-setting-api-integration@90c78d2f460fd24add0f078508fdbb14cbc486dd` |
| 权威设计 | [APSGo V7 Windows EXE 发布打包详细设计](../design/apsgo_v7_windows_exe_release_packaging_design.md) |
| 构建平台 | 64 位 Windows；当前 macOS ARM64 只执行文档和源码侧验证 |
| Python 环境 | Conda `aps_3.10.18`，Python 3.10.18 |
| 后续证据目录 | `docs/implementation/evidence/apsgo_v7_windows_exe_release_packaging/` |

本计划把 APSGo V7 当前规则服务和 GQGA4 月计划求解服务打包为 Windows 可执行程序。实施只改变发布入口、构建与部署辅助文件，不改变现有 HTTP 契约、业务规则、求解算法、评分、预算或 C# 基础业务流程。

## 2. 已确认决策

1. `data/apsgo_v7_rules.sqlite3` 可以并且需要由 Git 跟踪，作为新安装发布数据的权威来源。
2. SQLite 的 `-journal`、`-shm`、`-wal` 运行时文件继续忽略，禁止进入提交和发布包。
3. 首版采用 PyInstaller 目录式 `onedir` 控制台程序；Windows EXE 必须在 Windows x64 构建。
4. 服务继续使用现有 `config/apsgo_v7_service.yaml`，不新增环境变量；发布包将它作为不同文件名的模板，首次启动才生成现场配置，已有现场配置永不覆盖。
5. 发布包只包含 `apsgo_v7_rules_seed.sqlite3` 种子库；首次启动才生成 `apsgo_v7_rules.sqlite3` 运行库，已有运行库永不覆盖。
6. 只生成一个服务 EXE；初始化、迁移、恢复和安装器不分别打成 EXE。
7. 继续使用单 Uvicorn worker；打包不得顺带改成多进程。
8. 每个实施阶段完成验证后建立独立 Git 提交，提交信息使用中文并以 `#feat` 或 `#fix` 开头。

## 3. 实施边界

### 3.1 本计划包含

- Windows 冻结入口、PyInstaller 构建脚本和构建依赖固定；
- 受跟踪 YAML、规则数据库及 Git 提交身份的发布输入检查；
- SQLite 一致性种子生成、首次运行初始化和已有运行库保护；
- 启动、精确停止、发布清单和目录完整性检查；
- Windows 空白机、规则读写重启、完整 GQGA4、局域网、升级和回退验收；
- 发布说明、实施证据和当前项目文档收口。

### 3.2 本计划不包含

- 修改规则、求解流程、七级目标、300 秒搜索时限或 20 万次候选检查；
- 修改三个现有 HTTP 接口或 C# `PipelineV7ApiBaseUrl` 契约；
- MSI 安装器、Windows Service、自动升级器、托盘程序、代码签名和新健康接口；
- 登录鉴权、TLS 或公网暴露；
- 复制 V3 的 GQGA5 Worker、日计划库、训练数据、整套 Conda DLL 扫描；
- 在当前 macOS 上把源码检查写成 Windows 实包验收通过。

## 4. 当前输入基线

以下值是 2026-09-08 的只读现场快照，仅用于发现构建输入漂移；实现不得硬编码活动版本或指纹，每次构建都必须重新读取：

| 项目 | 当前值 |
|---|---|
| Git 分支 / 提交 | `codex/rule-setting-api-integration@90c78d2f460fd24add0f078508fdbb14cbc486dd` |
| 数据库受跟踪路径 | `data/apsgo_v7_rules.sqlite3` |
| 数据库 SHA-256 | `8ed693ac2a1c2be5866c7b3ba8596eab58cbd9db6619a4be61c6c6af175b5288` |
| SQLite schema | `2` |
| 活动版本 | `5` |
| 规则 | 17 条，其中 16 条启用 |
| 虚拟原型 | 27 个 |
| 软硬钢字典 | 230 条 |
| 规则指纹 | `841c7c61895ddda0f2fb25ccc1f18fe7e3843e14da472bb816f4253d09a33028` |
| 字典指纹 | `d292d5efb53ee541f4d3900b2295ababfecffb1912fd0bafe4cb8bbb7b90ec14` |
| 服务配置 | `0.0.0.0:8001`；310 秒总时限、10 秒收尾、200000 次候选检查 |

当前 `README.md` 已在阶段 0 修正为“主数据库受 Git 跟踪、三类运行时辅助文件继续忽略”。
历史证据中记录的活动版本 3 保留当时事实，不批量改写；阶段 6 只补最终发布说明与历史证据链接。

## 5. 最小文件规划

| 文件 | 职责 |
|---|---|
| `release/build_exe.bat` | 双击与命令行薄入口，只转调 PowerShell 构建脚本并传递退出码 |
| `release/build_exe.ps1` | 唯一构建编排：只读检查、干净构建、PyInstaller、配置模板、种子、脚本和清单组装 |
| `release/apsgo_v7_service_entry.py` | 极小冻结入口，只调用现有 `apsgo_v7_service.app:main` |
| `release/verify_release.py` | 共用发布验证：源输入、SQLite 身份、种子、目录和清单；不复制业务规则校验 |
| `release/requirements-build.txt` | 固定 Windows 实测通过的 PyInstaller 精确版本 |
| `release/README.md` | Windows 构建、首次部署、升级、回退和故障定位说明 |
| `tests/release/test_release_packaging.py` | 发布验证器和目录契约的最小自动化回归 |

`start_apsgo_v7_service.bat`、`stop_apsgo_v7_service.bat`、PyInstaller `.spec`、发布清单和 EXE 都由构建过程生成，不在源码中再维护第二份模板。若 PowerShell 内生成批处理导致转义复杂且难测，再把两个批处理模板提升为受跟踪文件；没有真实问题前不提前增加。

## 6. 实施与提交总览

| 阶段 | 完整名称 | 主要交付 | 建议提交信息 |
|---:|---|---|---|
| 0 | 固定发布输入与数据库跟踪边界 | `.gitignore`、README 当前口径、数据库身份证据 | `#feat 固定V7发布输入基线` |
| 1 | 建立发布输入与数据库验证 | 一个共用验证器及测试 | `#feat 增加V7发布输入校验` |
| 2 | 建立 Windows 目录式 EXE 构建 | 冻结入口、BAT、PowerShell、构建依赖 | `#feat 增加V7 Windows目录式打包` |
| 3 | 建立启停与规则数据库保护 | 种子生成、首次复制、精确停止 | `#feat 增加V7发布包启停与数据库保护` |
| 4 | 建立发布清单与包级冒烟 | 清单、临时副本 GET/POST/重启验证 | `#feat 增加V7发布包完整性验收` |
| 5 | 完成 Windows 真实业务与局域网验收 | 531 单求解、第二台电脑访问、证据 | `#feat 完成V7 Windows发布包验收` |
| 6 | 完成升级、回退与文档收口 | 不覆盖演练、恢复演练、最终说明 | `#feat 完成V7发布升级与回退验收` |

阶段号每次都必须与上表完整中文名称一起出现，不能只说“进入第 3 阶段”。

## 7. 全阶段共同执行规则

1. 开始时记录 `git status --short`、分支、提交、平台和 Python 版本。
2. 当前用户未提交的 `.gitignore` 修改要保留；阶段 0 才按已确认语义单独纳入提交，本次文档提交不得夹带。
3. 每个阶段只暂存该阶段白名单，不使用 `git add .` 或 `git add -A`。
4. 正式构建必须来自精确 Git 提交创建的全新分离 Git worktree，不能从混有本地修改的工作目录直接生成；普通提交测试仍使用 `git archive` 干净导出。
5. 所有规则写入和 HTTP 冒烟都使用发布目录的临时副本；不得修改仓库中受跟踪的数据库。
6. 构建前拒绝数据库 sidecar 文件；构建后和测试后也不得把 sidecar 带入发布包。
7. 每个阶段提交前执行 `git diff --cached --check`，使用 `git write-tree` 和 `git archive` 导出暂存树，在导出目录完成对应测试。
8. Python 测试使用 Conda `aps_3.10.18`、`PYTHONDONTWRITEBYTECODE=1` 与 `pytest -p no:cacheprovider`。
9. Windows 专属步骤若当前没有 Windows 环境，状态必须记为“待 Windows 验证”，不能用 macOS 静态检查替代。
10. 证据至少记录输入提交、命令、退出码、耗时、文件摘要和首个失败；修复另建 `#fix` 提交，不改验收门槛掩盖问题。

## 8. 阶段 0：固定发布输入与数据库跟踪边界

### 8.1 目的

使正式构建只接受已提交、可审计且逻辑有效的 YAML 和 SQLite，先消除“数据库已跟踪但忽略规则和 README 仍说未跟踪”的冲突。

### 8.2 实施

1. 核对 `data/apsgo_v7_rules.sqlite3` 已在 Git 索引中，且工作树内容与准备发布的提交一致。
2. 将用户已确认的 `.gitignore` 修改作为本阶段白名单：允许主数据库受跟踪，继续忽略三类 sidecar。
3. 修订 README 的数据库版本管理说明；不批量改写历史证据中的当时事实。
4. 只读执行 SQLite `quick_check`、外键检查、schema、活动版本、规则加载、规则指纹和字典指纹检查。
5. 确认 YAML 中的数据库相对路径仍解析到仓库基准库，配置不含本机绝对路径。
6. 使用 `git check-ignore` 实测 `release/build/`、`release/dist/` 和三类数据库 sidecar 的忽略结果，不能只根据规则文本推断。
7. 保存基线证据；数字来自现场读取，不在后续代码中硬编码。

### 8.3 验收

- 主数据库、YAML、`.gitignore` 和 README 的当前口径一致；
- `quick_check=ok`，外键检查为空，活动规则可由生产加载器读取；
- 仓库中没有数据库 sidecar；
- 既有累计测试通过；
- 本阶段不创建 EXE 或发布目录。

### 8.4 实际结果

- 实施前提交为 `90c78d2f460fd24add0f078508fdbb14cbc486dd`；只纳入用户已确认的
  `.gitignore` 修改、当前 README 口径、计划状态、项目约定和阶段证据。
- `data/apsgo_v7_rules.sqlite3` 已受 Git 跟踪，工作树内容与 HEAD 一致，SHA-256 为
  `8ed693ac2a1c2be5866c7b3ba8596eab58cbd9db6619a4be61c6c6af175b5288`；检查前后未变化。
- SQLite `quick_check=ok`、外键违规为 0、schema 为 2；生产加载器读取活动版本 5（基于版本 4）、
  17 条规则（16 条启用）、27 个虚拟原型和 230 条字典。
- 当前规则指纹为 `841c7c61895ddda0f2fb25ccc1f18fe7e3843e14da472bb816f4253d09a33028`，
  字典指纹为 `d292d5efb53ee541f4d3900b2295ababfecffb1912fd0bafe4cb8bbb7b90ec14`。
- 从仓库外目录加载 YAML 仍解析到受跟踪基准库；`release/build/`、`release/dist/` 和三类
  SQLite 辅助文件的忽略规则均经 `git check-ignore` 实测生效，仓库中无辅助文件。
- 本阶段没有修改数据库、YAML、生产代码或业务契约，也没有创建 EXE 或发布目录。
- 详细命令和首轮诊断修正见[阶段 0 证据](evidence/apsgo_v7_windows_exe_release_packaging/stage_00_release_input_baseline/README.md)；
  最终精确暂存树测试结果记录在本阶段 Git 提交正文中。

## 9. 阶段 1：建立发布输入与数据库验证

### 9.1 实施

1. 新增一个 `release/verify_release.py`，复用现有配置加载器、`RuleStore`、活动规则加载和指纹逻辑。
2. 提供两个必要模式：
   - `source`：只读验证 Git 提交、工作树、YAML、源数据库和 sidecar；
   - `package`：验证发布目录、种子、清单和禁止文件。
3. `source` 模式在有 `.git` 的分离 worktree 中运行，以 JSON 输出提交、可空分支、配置摘要、源数据库 SHA-256、schema、活动版本、规则/原型/字典数量及两个业务指纹，供 PowerShell 直接读取；提交号是权威身份，分支只作辅助信息。
4. 验证器只做发布边界检查，不重新实现规则编译、配置解析或 SQLite schema 逻辑。
5. 任何验证失败返回非零退出码，并用稳定错误代码指出具体文件或身份；不得修改、备份或恢复源数据库。

### 9.2 最小测试

- 当前合法 YAML 与临时数据库副本通过；
- 缺文件、损坏数据库、外键错误、没有活动版本、规则指纹错误分别失败；
- 配置未指向受控源库、出现 `-wal/-shm/-journal`、种子或清单摘要不符分别失败；
- 验证前后源数据库 SHA-256 不变；
- JSON 输出不包含本机用户名、订单数据或密钥。

### 9.3 验收

- Windows 构建脚本不再用 PowerShell 重写 SQLite/规则检查；
- 同一验证入口同时服务构建前和发布后；
- 生产服务代码零修改。

### 9.4 实际结果

- 新增 `release/verify_release.py`，提供 `source --repository-root` 和
  `package --package-root` 两个命令；退出码 0 表示通过、1 表示稳定验证失败、2 保留给
  `argparse` 参数错误。
- 源码模式验证 Git 提交及干净状态、受跟踪 YAML/SQLite、相对数据库路径、SQLite 辅助文件、
  文件摘要、schema、活动规则、虚拟原型和软硬钢字典身份；输出只使用仓库相对路径。
- 数据库检查使用 `mode=ro&immutable=1` 和 `query_only` 的冻结连接，在同一连接上复用既有
  `RuleStore` schema 验证及活动排程快照读取，不调用初始化、迁移、保存、恢复或备份入口。
- 包模式验证固定目录边界、全部普通文件摘要、配置模板、数据库种子及业务身份；拒绝符号链接、
  现场 YAML、运行库、SQLite 辅助文件、源码、测试或清单外文件。
- 新增 21 项专项回归，覆盖干净 detached HEAD、工作树/跟踪/配置边界、三类辅助文件、损坏
  数据库、错误 schema、活动身份不一致、合法模拟包、摘要篡改、运行库混入、种子损坏和符号链接。
- 生产服务代码、正式数据库、YAML、规则、求解算法和 HTTP 契约均未修改；详细记录见
  [阶段 1 证据](evidence/apsgo_v7_windows_exe_release_packaging/stage_01_source_and_database_validator/README.md)。

## 10. 阶段 2：建立 Windows 目录式 EXE 构建

### 10.1 实施

1. 增加只调用 `apsgo_v7_service.app:main` 的冻结入口，不复制配置或服务启动逻辑。
2. `build_exe.bat` 只定位自身目录、转调 PowerShell 并传回退出码。
3. `build_exe.ps1` 支持：
   - `-CheckOnly`：严格只读，不安装依赖、不创建目录、不清理产物；
   - `-Clean`：仅删除明确的 `release/build` 和 `release/dist/APSGoV7` 后重新构建；
   - 默认构建：输入不合法时立即停止。
4. 在首次试构建前把一个精确 PyInstaller 候选版本写入 `requirements-build.txt`，由开发人员显式安装；构建脚本校验 Windows x64、Conda 环境名 `aps_3.10.18`、Python 3.10.18 和该精确版本，但不得自动安装或升级。候选失败时明确修改版本并重新试构建，不能先成功后才补写版本。
5. 以 `--paths src`、`onedir`、`console` 构建，显式收集 Uvicorn 动态子模块；先不扫描整个 Conda 环境 DLL/PYD。
6. 构建目录只包含 V7 运行所需模块；禁止测试、文档、V3 资源、本机绝对路径和仓库正式数据库进入 EXE 内部。
7. 先将构建脚本与精确候选版本作为本阶段实现提交，再从该提交创建分离 worktree 做 Windows 构建；构建通过后用独立证据提交标记本阶段完成。若失败，使用 `#fix` 提交修正并从新提交完整重建，不改写已发生的失败证据。

### 10.2 Windows 命令入口

```powershell
git worktree add --detach C:\apsgo-v7-release-build <exact-commit>
Set-Location C:\apsgo-v7-release-build
conda run -n aps_3.10.18 python --version
conda run -n aps_3.10.18 python -m pip install -r release\requirements-build.txt
release\build_exe.bat -CheckOnly
release\build_exe.bat -Clean
release\dist\APSGoV7\APSGoV7Service.exe --help
```

### 10.3 验收

- `-CheckOnly` 设置 `PYTHONDONTWRITEBYTECODE=1`，并保持项目工作树、源数据库和发布目录零写入；不对 Conda 或操作系统自身缓存作全系统零写入承诺；
- 全新 `-Clean` 构建退出码为 0；
- 未安装项目源码、未激活 Conda 的 Windows 测试目录中，EXE `--help` 成功；
- 只产生一个服务 EXE 和一个 `_internal` 目录；
- 依赖漏包按真实错误最小补充，不用全环境扫描兜底。
- Windows 构建证据对应一个真实 Git 提交，清单中的提交号与该提交完全一致。

## 11. 阶段 3：建立启停与规则数据库保护

### 11.1 实施

1. 复用现有 `backup_sqlite_database()` 从受跟踪源库生成 `data/apsgo_v7_rules_seed.sqlite3`，禁止在服务可能写入时直接复制主文件。种子按流程视为不可修改，但不设置会传播到运行库的 Windows 只读文件属性。
2. 生成种子后立即用阶段 1 验证器核对 schema、活动规则、业务指纹和逻辑内容身份。
3. 生成启动脚本：
   - 通过 `%~dp0` 定位发布根目录并支持带空格路径；
   - 检查 EXE、`config/apsgo_v7_service.example.yaml` 配置模板和种子；
   - 仅当现场配置不存在时，复制为 `config/apsgo_v7_service.yaml`；
   - 仅当 `data/apsgo_v7_rules.sqlite3` 不存在时复制种子；
   - 现场配置或运行库存在时不比较新旧、不覆盖、不合并；
   - 确认新生成的运行库可写；
   - 使用绝对 `--config` 参数启动 EXE并保留退出码。
4. 生成停止脚本：按当前发布目录中 EXE 的规范绝对路径筛选进程，只结束该实例，不按名称全杀、不结束 Python、不影响其他目录的 V7 实例。
5. 启停脚本不设置 host、port、数据库路径或求解预算；这些仍只从 YAML 读取。

### 11.2 Windows 测试

- 全新发布副本首次启动后生成现场配置和可写运行库，规则 GET 返回 200；
- 通过 POST 保存并启用新版本，停止、再次启动后版本仍存在；
- 用新模板和新种子覆盖发布副本，再启动时原现场配置、运行库 SHA 和活动版本不变；
- 缺 EXE、配置模板、种子或复制失败时明确退出，不能创建空配置或空库继续运行；
- 发布路径含空格时可启动、读取规则和停止；
- 同时启动两个不同目录实例时，第二个副本使用独立临时 YAML 和端口；停止脚本只结束目标目录实例。

### 11.3 验收

- 新版压缩包覆盖解压不会出现与现场配置、运行库同名的文件，因此不会直接覆盖现场配置或规则；
- 仓库基准库、发布种子和现场运行库职责可由路径和文件名直接区分；
- 构建及冒烟不改变仓库基准库。

## 12. 阶段 4：建立发布清单与包级冒烟

### 12.1 发布清单

生成 `release_manifest.json`，至少记录：

- 应用和项目版本、Git 提交、构建时间；
- Windows 架构、Python、Conda 环境、PyInstaller、FastAPI、Uvicorn 和 PyYAML 的实际版本；
- 构建模式、入口和单 worker 约束；
- EXE、`_internal` 文件集合、配置模板、源数据库、种子数据库、启停脚本和发布说明的 SHA-256；清单不计算自身摘要；
- SQLite schema、活动版本、规则/原型/字典数量；
- 规则指纹、字典指纹；
- 构建、静态检查和冒烟状态。

清单内容由程序生成，键顺序和 UTF-8 编码固定；不得包含订单、用户目录、访问令牌或数据库密码。活动版本及数量必须从本次构建输入读取，不能抄录阶段 0 的数字。

### 12.2 冒烟流程

1. 把完整发布包复制到新的临时验收目录，正式构建输出保持不运行。
2. 在临时副本首次生成现场配置和运行库并启动服务，使用现有活动规则 GET 作为就绪检查；当前没有健康接口，不新增虚假探针。
3. 执行 GET → POST 保存并启用 → GET → 停止 → 重启 → GET，确认身份和持久化。
4. 停止服务后执行 SQLite `quick_check`、外键检查和活动快照加载。
5. 比较临时运行前后的种子 SHA，必须不变；仓库源库 SHA 也必须不变。
6. 验证正式交付目录中没有冒烟生成的现场 YAML、`apsgo_v7_rules.sqlite3` 和 sidecar，只保留配置模板与种子。
7. 运行阶段 1 的 `package` 模式核对目录白名单及清单摘要。

### 12.3 验收

- 发布包可以独立启动、读取和保存规则；
- 重启后规则版本保持；
- 正式包没有测试运行库、日志、缓存、源代码或本机路径；
- 清单与发布目录逐文件一致，任意受控文件改变都会失败。

## 13. 阶段 5：完成 Windows 真实业务与局域网验收

### 13.1 环境

- 目标平台为 Windows x64；至少一台构建/服务电脑和一台同局域网访问电脑；
- 服务电脑不依赖项目源码或激活的 Conda 环境；
- 发布目录对运行账号可写，不放在普通用户不可写的 `Program Files`；
- 防火墙放行由部署人员显式执行和记录，程序不静默修改系统规则。

### 13.2 完整 GQGA4 求解

1. 使用当前 C# 月计划客户端和冻结的 531 条真实输入调用既有求解接口。
2. 保持发布 YAML 当前策略：310 秒总时限，其中 300 秒搜索、10 秒收尾，候选检查上限 200000。
3. 这次 310 秒运行是发布功能验收，HTTP 请求必须在 C# 当前 370 秒等待上限内返回；它不等同于历史冻结的 180 秒算法性能门槛，也不能用来宣称后者通过。是否继续单独执行 180 秒性能验收，按现有算法验收计划或用户后续确认处理。
4. 同时记录 EXE 与同机 Python 入口的真实耗时，打包专项不另造算法性能门槛。
5. 验证：
   - `publishable=true`；
   - 禁止违规 0 项、欠重链 0 条；
   - 531 个来源覆盖及总重量守恒；
   - 拆单谱系、虚拟资源和发布顺序可审计；
   - 生成审计与无缓存重放审计均通过；
   - 返回的规则、字典、问题和运行身份完整。
6. 若 EXE 结果与相同输入的 Python 入口不一致，先比较提交、YAML、数据库、请求和随机种子；不得直接放宽规则或验收值。

### 13.3 局域网与进程验证

- 本机 `127.0.0.1:8001` 的规则 GET 成功；
- 第二台电脑通过服务电脑真实 IPv4 地址访问规则 GET；
- 第二台电脑至少完成一次受控求解调用；
- 客户端不得使用 `0.0.0.0` 作为目标地址；
- 精确停止后端口释放，其他目录实例不受影响。

### 13.4 验收边界

本阶段完成才允许写“Windows 发布包业务验收通过”。单次 GET、EXE `--help`、macOS 测试或仅生成目录都不能替代完整验收。若 Windows 兼容问题需要修改代码，先建立独立 `#fix` 提交并重跑阶段 2～5 的受影响门禁。

## 14. 阶段 6：完成升级、回退与文档收口

### 14.1 升级演练

1. 在旧发布副本中通过规则 POST 产生一个只存在于现场运行库的新活动版本。
2. 停止服务，使用 SQLite Backup API 把运行库备份到部署目录外，并记录摘要。
3. 覆盖新版 EXE、`_internal`、配置模板、种子、启停脚本和发布说明；不复制、不删除、不重建现场 YAML 或运行库。
4. 启动新版，确认升级前现场活动版本、规则历史和字典仍存在。
5. 检查新种子只用于未来新安装，没有自动覆盖或合并现场运行库。

### 14.2 回退演练

1. 停止新版，恢复相互匹配的旧 EXE、`_internal`、配置模板和启停脚本；现场 YAML 默认保留。若新版要求不同配置且已人工迁移，则一并恢复升级前备份的现场 YAML。
2. 若新版期间没有写库，可保留兼容的原运行库；若写过规则或 schema 变化，恢复升级前数据库备份。
3. 重启后执行规则 GET、`quick_check`、外键检查和活动快照加载。
4. 当前只验收 schema v2 到 v2 的程序升级；未来 schema 改变必须建立专项迁移计划，不能用新种子覆盖。

### 14.3 文档与证据收口

- 更新 `release/README.md` 的构建、首次部署、正常启停、升级、回退和常见错误；
- 同步项目 README 与 AGENTS.md 的最新发布入口、准确活动库身份和完成边界；
- 保存 Windows 构建日志、清单、空白机、规则持久化、531 单、局域网、升级及回退证据；
- 不在证据中提交运行库、订单原文、sidecar 或敏感网络配置；
- 除非用户另行要求，不创建 Git 标签、不上传制品、不部署生产环境。

## 15. 验证矩阵

| 验证项 | macOS 源码阶段 | Windows 构建机 | Windows 空白/部署机 |
|---|---:|---:|---:|
| 文档链接、UTF-8、Git 跟踪 | 必须 | 可复核 | 不适用 |
| Python 发布验证器单测 | 必须 | 必须 | 不适用 |
| SQLite 源库只读身份 | 必须 | 必须 | 不适用 |
| PyInstaller 构建 | 不可替代 | 必须 | 不适用 |
| EXE `--help` | 不可替代 | 必须 | 必须 |
| 首次配置/种子复制、规则持久化 | 不可替代 | 必须 | 必须 |
| 完整 GQGA4 531 单 | 源入口回归 | 必须 | 必须 |
| 第二台电脑局域网访问 | 不可替代 | 可作为服务端 | 必须 |
| 升级和回退 | 流程静态检查 | 必须 | 必须 |

## 16. 每阶段提交前命令框架

macOS 或 Windows 的项目回归都从干净导出执行；以下命令中的临时目录必须由当前平台安全生成，不能把仓库根作为清理目标。

```bash
git status --short
git diff --cached --check
release_tree=$(git write-tree)
release_temp=$(mktemp -d)
git archive --format=tar "$release_tree" | tar -xf - -C "$release_temp"
cd "$release_temp"
PYTHONDONTWRITEBYTECODE=1 conda run -n aps_3.10.18 \
  python -m pytest -p no:cacheprovider \
  tests/architecture tests/api tests/app tests/core tests/service tests/release
```

阶段 1 创建 `tests/release` 后才把它加入累计命令；阶段 0 与本次纯文档提交只运行当时已存在的测试目录。临时目录在验证结束后按其明确绝对路径清理，不得把仓库根、用户目录或未解析变量作为清理目标。

Windows 构建阶段另执行：

```powershell
release\build_exe.bat -CheckOnly
release\build_exe.bat -Clean
release\dist\APSGoV7\APSGoV7Service.exe --help
```

## 17. 完成定义

必须同时满足以下条件，本计划才可标记完成：

1. 所有阶段以独立提交完成，工作树只保留用户明确保留的无关修改。
2. 配置模板与现场 YAML、受跟踪主数据库与种子及运行库的身份明确，发布和升级均不能覆盖现场配置或规则。
3. 目录式 EXE 在无 Python/Conda 的 Windows x64 上启动、停止和重启成功。
4. 规则查询、保存并启用、重启持久化全部通过。
5. 完整 GQGA4 531 单质量、来源守恒、拆单谱系和双审计通过。
6. 第二台电脑使用真实局域网 IP 访问成功。
7. 发布清单与全部受控文件、Git 提交和数据库身份一致。
8. 升级和回退演练证明现场运行库不丢失。
9. Windows 实测证据、发布说明、项目 README 和 AGENTS.md 已同步。

任一项未完成时，只能报告已完成的具体层级，例如“源码侧发布检查通过”或“Windows EXE 基础冒烟通过”，不能概括为“发布完成”。
