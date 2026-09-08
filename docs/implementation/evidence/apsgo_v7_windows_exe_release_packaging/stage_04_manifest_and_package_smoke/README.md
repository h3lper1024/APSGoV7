# 阶段 4：建立发布清单与包级冒烟——源码实施证据

## 1. 范围与环境

| 项目 | 结果 |
|---|---|
| 日期 | 2026-09-08 |
| 实施前提交 | `b2f683b0d569154f44892b29070e09b43199f295` |
| 当前平台 | macOS ARM64，zsh；不能替代 Windows BAT、PowerShell 和 EXE 验证 |
| Python | Conda `aps_3.10.18`，Python 3.10.18 |
| 本阶段新增 | 规范发布清单、发布说明、临时包规则读写重启冒烟及回归 |
| 明确未修改 | 生产服务、正式数据库、正式 YAML、规则、算法、评分、预算和 HTTP 契约 |

## 2. 实现结果

- `release_manifest.json` 由共用验证器生成，记录应用/项目版本、Git 提交、UTC 构建时间、实际构建依赖、目录式单进程入口、源库与种子物理/逻辑身份、规则/原型/软硬钢字典身份、验证状态和清单外全部文件 SHA-256。清单自身不计算自身摘要。
- 清单采用规范 UTF-8 JSON、无 BOM、固定键序和单个 LF 结尾；严格拒绝未知字段、错误类型、绝对/越界路径、摘要不匹配、符号链接、Windows reparse point、现场 YAML、运行库、SQLite sidecar、源码、测试、缓存和日志。
- PowerShell 构建顺序为：生成 `smoke=pending` 清单 → 允许待冒烟状态的完整包预检 → 运行临时副本冒烟 → 生成 `smoke=pass` 清单 → 默认严格完整包终检。任一环节非零都不会输出“构建完成”。
- 冒烟只操作正式包的新临时副本。正式配置模板保持清单保护和字节不变；临时副本另生成只监听 `127.0.0.1` 动态端口的现场 YAML，运行库仍由启动脚本从种子初始化，避免占用或暴露正式 `0.0.0.0:8001`。
- 回环 HTTP 显式禁用系统代理。规则流程为首次 GET → 同内容 POST 保存并启用 → GET → 停止 → 重启 → GET；保存前后逐项比较产线、工序、场景、17 条规则、七级评分、允许偏差和 27 个虚拟原型，只允许版本、来源版本、备注、激活信息及版本相关规则指纹变化。
- 停服后通过现有只读数据库入口核对 `quick_check`、外键、schema、活动/来源版本、规则总数、启用数、虚拟原型数、230 条软硬钢字典及两个指纹。源库、正式种子、临时副本种子和正式配置摘要必须保持不变。
- 正常停止使用交付停止脚本；若脚本失败或超时，构建期冒烟只对本次启动进程的精确 PID 及其子进程调用 Windows 应急清理。该兜底不进入交付停止脚本；清理仍失败时明确报错并保留临时目录。
- `release/README.md` 说明完整目录部署、首次初始化、YAML 配置、精确停止、种子/运行库分离、升级保护、无鉴权局域网边界、清单不是数字签名及常见失败处理。

## 3. 源码侧验证

```sh
ruff check \
  release/verify_release.py \
  release/smoke_release.py \
  tests/release/test_release_packaging.py

PYTHONDONTWRITEBYTECODE=1 conda run -n aps_3.10.18 \
  python -m pytest -p no:cacheprovider \
  tests/release/test_release_packaging.py -q

PYTHONDONTWRITEBYTECODE=1 conda run -n aps_3.10.18 \
  python -m pytest -p no:cacheprovider \
  tests/architecture tests/api tests/app tests/core tests/service tests/release -q
```

| 检查 | 结果 |
|---|---|
| Ruff | 退出码 0，`All checks passed!` |
| 发布专项 | 退出码 0，51 项通过，8.79 秒（最终源码复核后） |
| 仓内累计 | 退出码 0，3349 项通过，200.26 秒 |

Conda `aps_3.10.18` 中没有安装 Ruff，因此 Ruff 只作为当前开发机的附加静态检查；所有正式 Python 测试仍使用用户指定的 Conda 环境，没有静默切换 Python 环境。精确暂存树将在提交前通过 `git write-tree`、`git archive` 导出，再执行残留门禁、同一累计回归和 `compileall -q src release`；最终结果记录在提交正文。

## 4. 复核与首轮问题

- 首轮清单测试因测试模块缺少 `Decimal` 导入失败，补齐测试依赖后通过；生产逻辑不受该测试导入问题影响。
- 第二轮复核发现“POST 响应与随后 GET 相同”不能证明保存前后规则业务内容未变，已增加首次 GET 与保存后 GET 的稳定字段对照，并补充运行库规则/字典完整身份核对。
- 第二轮还发现直接使用正式 `0.0.0.0:8001` 会冲突并短暂暴露无鉴权接口，且默认 `urlopen` 可能继承企业代理；已改为临时回环动态端口和禁用代理的专用客户端。
- 停止失败原先可能只返回构建失败而留下临时进程，现已增加精确 PID 进程树应急清理，并覆盖停止非零、停止超时和进程已自行退出竞态。
- 共享工作树残留检查退出码为 1，仍只因 V7 已不存在的 V6 历史稳定残留：`.claude/settings.local.json`、两个旧 `dist/apsgo-*` 产物和五个 `src/apsgo.egg-info/*` 文件。没有为通过检查重建这些旧文件；干净导出检查仍是正式门禁。

## 5. 未关闭门禁

当前平台没有运行 Windows BAT/PowerShell，也没有生成 Windows EXE。以下必须在本阶段源码提交的 Windows x64 detached worktree 和真实发布目录中验证：

- PyInstaller 候选版本能成功生成完整 `onedir` 包，清单中的实际依赖和文件摘要正确；
- BAT/PowerShell 在带空格路径中运行，配置模板自动初始化、运行库初始化和已有文件保护有效；
- 动态回环端口、禁用代理、规则 GET/POST/停止/重启、精确停止与应急清理真实生效；
- 清单由 `pending` 变为 `pass` 后，正式包没有现场文件、日志、缓存、源码或清单外文件；
- 无 Python/Conda 的空白 Windows 目标机可以执行 EXE；完整 531 单和局域网验收归阶段 5。

因此当前只能称“阶段 4 源码实现完成”，不能称“Windows 发布包完整性验收完成”。
