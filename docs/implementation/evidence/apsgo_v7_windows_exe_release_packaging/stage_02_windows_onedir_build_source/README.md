# 阶段 2：建立 Windows 目录式 EXE 构建——源码实施证据

## 1. 范围

| 项目 | 结果 |
|---|---|
| 日期 | 2026-09-08 |
| 实施前提交 | `4bebd5f0e4a21668e923760fd16ec094cf90469a` |
| 当前平台 | macOS ARM64，zsh；不能生成或验证 Windows EXE |
| Python | Conda `aps_3.10.18`，Python 3.10.18 |
| 本项新增 | 冻结入口、薄 BAT、PowerShell 构建脚本、固定构建依赖及静态回归 |
| 明确未修改 | 生产服务、正式数据库、YAML、规则、求解算法、评分和 HTTP 契约 |

## 2. V3 参考与 V7 取舍

只读复核 `/Users/miles/dev/dev-py/apsgo-v3/release` 后，V7 只吸收 BAT 薄转调、PowerShell
单一编排、解析 Conda 前缀后直调 `python.exe`、收集 Uvicorn 子模块和明确输出检查。

V3 实际使用单文件模式，缺少 PyInstaller 时会在脚本内安装，未固定版本、未显式核验
Windows x64/Python 3.10.18，并扫描整个 Conda 环境的 DLL/PYD；冻结入口还包含多进程和其他
产线逻辑。这些做法均未带入 V7。V7 使用目录式控制台程序，不启用 UPX，不复制 V3 配置、
训练数据、日计划数据库、GQGA5 Worker 或 `openpyxl`。

## 3. 构建契约

- `release/apsgo_v7_service_entry.py` 只调用现有服务 `main()`。
- `release/build_exe.bat` 只转调同目录 PowerShell，并原样传递参数和退出码。
- `release/build_exe.ps1` 固定 Windows x64、Conda `aps_3.10.18`、Python 3.10.18；正式构建
  接受源码验证器确认的干净普通分支或分离提交状态。
- `-CheckOnly` 与 `-Clean` 互斥；前者在所有构建目录写入前退出。后者只清理
  `release/build` 和 `release/dist/APSGoV7`，并拒绝重解析点。只读检查设置
  `GIT_OPTIONAL_LOCKS=0`，避免 Git 为刷新索引状态取得可选写锁。
- PyInstaller 临时输出位于 `release/build/pyinstaller_dist`，验证 EXE 与 `_internal` 后才
  整体移动为 `release/dist/APSGoV7`。
- 构建参数为 `onedir + console + noupx + --paths src`，只显式收集 Uvicorn 子模块。

## 4. PyInstaller 候选边界

`release/requirements-build.txt` 固定 `PyInstaller==6.22.2`。截至 2026-09-08，
[官方 PyPI](https://pypi.org/project/pyinstaller/)显示该版本发布于 2026-08-17，支持
Python 3.8～3.15，并提供 Windows x86-64 wheel；
[官方变更记录](https://pyinstaller.org/en/latest/CHANGES.html)也列出 6.22.2。

这只说明候选与 Python 3.10/Windows x64 的公开元数据相容。当前项目 Conda 环境没有
PyInstaller，macOS 也没有 PowerShell；未在 Windows 构建成功前，不把 6.22.2 称为
“V7 实测通过版本”。构建脚本只校验版本，不自动安装或升级。

## 5. 源码侧验证

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n aps_3.10.18 \
  python -m pytest -p no:cacheprovider tests/release/test_release_packaging.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src conda run -n aps_3.10.18 \
  python release/apsgo_v7_service_entry.py --help

PYTHONDONTWRITEBYTECODE=1 conda run -n aps_3.10.18 \
  python -m compileall -q release
```

结果：26 项发布专项通过，最小冻结入口帮助正常，发布 Python 文件编译通过。专项覆盖入口
职责、精确版本、BAT 参数/退出码、Windows x64 与固定环境门禁、分离提交状态、严格只读
`-CheckOnly`、受控清理、目录式参数，以及禁止自动安装和全环境二进制扫描。

同一共享工作树使用规定范围
`tests/architecture tests/api tests/app tests/core tests/service tests/release` 执行累计回归，
结果为 3324 项通过、0 项跳过、0 项失败，Pytest 报告耗时 189.93 秒。精确暂存树的干净导出
仍按项目提交门禁重新执行，最终结果记录在本阶段提交正文中。

入口若省略 `PYTHONPATH=src` 会因项目采用 `src` 布局而无法导入；补上后退出码为 0。这与
PyInstaller 命令必须使用 `--paths src` 的设计一致，不是通过修改冻结入口绕过包结构。

## 6. 未关闭门禁

以下项目必须在 Windows x64、当前干净提交的 Git 工作树上完成；普通分支和分离提交状态均可：

```powershell
conda run -n aps_3.10.18 python -m pip install -r release\requirements-build.txt
release\build_exe.bat -CheckOnly
release\build_exe.bat -Clean
release\dist\APSGoV7\APSGoV7Service.exe --help
```

还须在未安装 Python/Conda 的 Windows 测试目录验证同一 EXE。当前证据只能称“阶段 2 源码
实现完成”，不能称“阶段 2 完成”或“Windows EXE 已验证”。

## 7. 首次 Windows 检查失败与修复

用户在 Windows 路径 `Y:\dev-py\APSGOV7` 执行构建入口后提供的日志显示，脚本已通过
Windows 与 64 位进程门禁，也已定位 Conda 环境 `aps_3.10.18` 的 `python.exe`，随后在
`build_exe.ps1` 的运行时信息检查处失败：

```text
File "<string>", line 8
  version: ..join(str(part) for part in sys.version_info[:3]),
           ^
SyntaxError: invalid syntax
Cannot inspect Python in Conda environment 'aps_3.10.18'.
```

源码原本是 `"version": ".".join(...)`。`build_exe.bat` 使用 Windows PowerShell 调用原生
`python.exe -c`；该参数传递过程没有保留多行 Python 源码中的字面双引号，因而实收代码与
日志完全对应。后续 PyInstaller 版本检查和数据库种子备份也使用了相同方式，只修改报错行会
让构建在下一段继续失败。

修复将三段多行 Python 都改为 `$Code | & $CondaPython -`：源码经标准输入传递，文件路径等
参数仍使用 `sys.argv`，不生成临时文件，也不改变检查和构建顺序。新增回归测试要求三段都走
标准输入，并禁止恢复原有的多行 `-c` 形式。

修复后的共享工作树验证如下：

| 检查 | 结果 |
|---|---|
| `git diff --check` | 退出码 0 |
| 发布专项 | 退出码 0，52 项通过，7.95 秒 |
| 仓内累计 | 退出码 0，3350 项通过，186.86 秒 |
| `compileall -q release` | 退出码 0 |
| 共享树残留检查 | 退出码 1；仍只因 V7 已不存在的 V6 历史 `.claude`、`dist/apsgo-*` 和 `src/apsgo.egg-info` 稳定残留，不重建这些旧文件 |
| 首轮精确暂存树干净导出 | 树 `861c3e432da900377b64ac8c79254e6a40666c79`；残留检查退出码 0，3350 项累计通过，199.63 秒，`compileall -q src release` 退出码 0 |

当前开发机仍为 macOS ARM64，没有执行 Windows PowerShell。修复提交后须在该新提交的
干净 Git 工作树重新执行 `release\build_exe.bat -CheckOnly`；通过后再执行 `-Clean`，
该次失败不能计为 Windows 构建门禁通过。

## 8. 普通分支源树门禁修正

用户在普通分支重试时，源码校验因未跟踪的 `.vscode/settings.json` 返回
`source_tree_not_clean`。该文件是编辑器本地配置，不影响构建内容；用户已确认在根
`.gitignore` 中加入 `/.vscode/`。门禁据此只做以下收紧后的放宽：

- 普通分支和分离提交状态均可构建，清单分别记录实际本地分支名或 `null`；
- 完整 40 位提交号仍是权威发布身份；
- 全部已跟踪文件改动和其他未忽略的未跟踪文件仍失败；
- SQLite 辅助文件即使被忽略也继续由专门检查拒绝；
- 构建结束前复核分支、提交、配置和数据库身份，任一变化都使构建失败。

本次没有放宽配置、数据库、清单、包目录或业务门禁。当前 macOS 只能完成源码回归，仍须由
用户在 Windows x64 的干净提交上重新执行 `-CheckOnly` 和 `-Clean`。

源码修正后的共享工作树验证：`git diff --check` 与 Ruff 退出码均为 0，发布专项 55 项通过
（8.64 秒），仓内累计 3353 项通过（185.48 秒）。共享残留检查仍只因 V7 已不存在的 8 个
V6 历史稳定残留失败；不重建这些旧文件，精确暂存树的干净导出检查仍是正式提交门禁。
