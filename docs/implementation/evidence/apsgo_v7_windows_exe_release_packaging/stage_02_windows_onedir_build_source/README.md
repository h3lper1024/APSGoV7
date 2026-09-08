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
  只接受源码验证器确认的干净 detached HEAD。
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
职责、精确版本、BAT 参数/退出码、Windows x64 与固定环境门禁、detached HEAD、严格只读
`-CheckOnly`、受控清理、目录式参数，以及禁止自动安装和全环境二进制扫描。

同一共享工作树使用规定范围
`tests/architecture tests/api tests/app tests/core tests/service tests/release` 执行累计回归，
结果为 3324 项通过、0 项跳过、0 项失败，Pytest 报告耗时 189.93 秒。精确暂存树的干净导出
仍按项目提交门禁重新执行，最终结果记录在本阶段提交正文中。

入口若省略 `PYTHONPATH=src` 会因项目采用 `src` 布局而无法导入；补上后退出码为 0。这与
PyInstaller 命令必须使用 `--paths src` 的设计一致，不是通过修改冻结入口绕过包结构。

## 6. 未关闭门禁

以下项目必须在 Windows x64、当前阶段提交的 detached worktree 上完成：

```powershell
conda run -n aps_3.10.18 python -m pip install -r release\requirements-build.txt
release\build_exe.bat -CheckOnly
release\build_exe.bat -Clean
release\dist\APSGoV7\APSGoV7Service.exe --help
```

还须在未安装 Python/Conda 的 Windows 测试目录验证同一 EXE。当前证据只能称“阶段 2 源码
实现完成”，不能称“阶段 2 完成”或“Windows EXE 已验证”。
