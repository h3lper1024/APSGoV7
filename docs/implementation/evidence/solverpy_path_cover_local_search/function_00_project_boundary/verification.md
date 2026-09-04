# 功能 0：建立自包含工程与架构门禁

## 范围与身份

- 日期：2026-09-03；macOS 27.0 / Darwin 27.0.0 arm64，zsh。
- 分支：`codex/solverpy-path-cover-clean`。
- 实施前提交：`98f3060bfae4b5b8f88bb4d1dc50e9930e6ede8c`。
- 目标设计 v0.2 SHA-256：`db8ab0f39cc8d63a7f4304e83de07f45ce4170bbeea5bb8244f89f65115bc7c8`。
- 本次测试代码快照树：`ffb32caf70755baad399082801dbce6260be5daf`；由显式暂存文件通过 `git write-tree` 生成，再用 `git archive` 导出到新临时目录。之后只补充本文、实施计划与 AGENTS 状态，并在最终暂存树再次复核。
- 本功能提交由 Git 历史标识，本文不自引用提交 SHA。

本功能新增：`pyproject.toml`、`.gitignore`、`README.md`、`AGENTS.md`，`src/apsgo_scheduler/` 下根包及 `api/core/app` 的四个初始化文件，`tests/architecture/test_clean_room_boundaries.py`、`test_package_dependencies.py`，`tools/check_workspace_residuals.py`，本目录两份残留清单与本记录；同步实施计划。没有恢复旧源码、测试或工程配置。

## 环境

| 工具 | 实际版本 |
|---|---|
| Python | Conda apsgo_v6_3.10.18 / 3.10.18，conda-forge，Clang 18.1.8 |
| pytest | 8.4.2 |
| Ruff | 0.12.0，现有 /Users/miles/anaconda3/bin/ruff；未为本项新增安装 |
| setuptools / wheel / build | 84.0.0 / 0.48.0 / 1.5.0 |

生产依赖为空；测试与静态检查依赖仅声明在 `dev` 可选依赖组。打包发现范围明确限定新包。

## 实际命令与结果

以下命令共享树与干净导出均执行，全部退出码为 0：

```sh
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture -q
conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --self-test
ruff check --no-cache src/apsgo_scheduler tests/architecture/test_clean_room_boundaries.py tests/architecture/test_package_dependencies.py tools/check_workspace_residuals.py
ruff format --check --no-cache src/apsgo_scheduler tests/architecture/test_clean_room_boundaries.py tests/architecture/test_package_dependencies.py tools/check_workspace_residuals.py
```

| 检查 | 共享工作树 | 暂存代码快照干净导出 |
|---|---|---|
| 架构聚焦及累计回归 | 46 passed，0.05 秒 | 46 passed，0.04 秒 |
| 静态检查与格式 | 通过，7 个 Python 文件 | 通过，7 个 Python 文件 |
| 残留保护 | 16 个稳定内容一致；992 个易变路径，无新增或消失；无残留暂存 | 冻结清单身份一致；1008 个原残留均未导出 |
| 保护脚本自测 | 分类、忽略分类变化、修改、误暂存、导出污染负例通过 | 同样通过 |
| 上述命令组外层耗时 | 5.245 秒 | 包含下述编译与构建共 8.947 秒 |

仅在干净导出执行，全部退出码为 0：

```sh
conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools
conda run -n apsgo_v6_3.10.18 python -m build --no-isolation --wheel
```

构建后另以 Python `-I` 隔离进程把生成的 wheel 放入导入路径，实际导入三个子包，核对其来自该 wheel；读取 wheel 元数据确认无生产依赖、只有四个新包源码文件。验证通过，外层耗时 1.517 秒。没有在共享工作树构建或安装，没有改动已有 Python 环境。

另执行 `git diff --check`、`git diff --cached --check`、精确暂存白名单核对和 `git ls-files` 回读，均通过。没有把不存在的业务测试目录作为累计测试命令参数。

## 旧文件保护

- 稳定清单：16 行，SHA-256 `7836b324cb07b00830a08ef28aa01cc5b22ccddac4cb08787a745e5e7f853084`。
- 易变清单：992 行，SHA-256 `aaedd08fa4b8087052d6b42e7db9bf742a4758331f500db2cd99a166bdeb8e4c`。
- 序列化按 UTF-8 路径字节排序，稳定清单为“相对路径、制表符、文件 SHA-256、换行”，易变清单为“相对路径、换行”。
- 易变定义严格采用实施计划第 2.2 节；内容不冻结，路径增减记录，不因 IDE 自然改写误报业务变更。任何易变文件仍禁止进入暂存树。
- `.gitignore` 改变分类后仍取未跟踪与忽略文件并集；原受保护内容没有逃出检查。

## 结论与边界

未发现非预期差异；共享树与干净导出的测试数量一致。AST（Python 源码语法树）检查覆盖标准库依赖、旧源码标识与类型、运行时加载、绝对源码路径、产线基准常量、软链接与反向依赖；这不是任意恶意动态 Python 程序的形式化证明。

按现行设计，数值字面量 `3` 与 `37/531/29333.91` 一并禁止进入生产源码。当前空包不受影响；后续若真正的算法结构需要同值常数，应先区分算法语义与基准硬编码并按设计决策流程处理，不能静默放宽门禁。

本功能不含领域模型、规则、求解器、主搜索、质量或性能验收。下一项为功能 1“冻结参考输入、输出与阶段对照基线”；两份数值门槛文件仍需复测后由用户确认，未授权越过这一确认点。
