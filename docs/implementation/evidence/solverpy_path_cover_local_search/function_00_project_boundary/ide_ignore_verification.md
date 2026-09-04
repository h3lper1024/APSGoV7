# 用户确认忽略本地 IDE 配置

- 日期：2026-09-03；macOS Darwin 27.0.0 arm64，zsh，Conda `apsgo_v6_3.10.18`。
- 实施前提交：`04152afa5307453744c12615ad1175ae318a000b`。
- 用户明确要求：`.idea` 目录加入 Git 忽略；不修改或提交其内容。
- 本单元仅 `.gitignore`、现有残留检查工具和本文。待提交 5.19 的六文件保持工作树，不混入本修复提交；相关当前实施状态随 5.19 同步。

此前共享工作树保护检查因 `.idea/APSGOV6.iml` 内容与历史摘要不同而失败；仅有字节变化证据，不认定修改者。现在根 `/.idea/` 被明确排除本地内容与存在性核验，但任何根 IDE 文件仍不得被 Git 跟踪或进入干净导出。历史 16 个稳定/992 个易变残留清单和哈希保持，非 IDE 稳定残留仍逐项保护，忽略规则不能隐藏它们。

复用原保护工具的自测，不新增测试框架。覆盖 IDE 修改与移除可接受、强制加入清单外 IDE 文件和导出污染仍拒绝，以及原非 IDE 内容变更/误暂存保护。

## 实际验证

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --self-test
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
```

共享树各命令退出 0：自测和保护检查通过；累计 1267 项、1.43 秒，包含尚未提交 5.19 的 61 项；静态/格式 49 文件通过。`git check-ignore -v .idea/APSGOV6.iml` 命中 `/.idea/`，IDE 文件未跟踪。独立只读复核通过。

初验暂存树 `6377e81cc27ff883dc01389c9762ebea5d2bdb6f`，导出 `/tmp/apsgo-ide-initial-kpKYx5`：同一仓内测试目录累计 1206 项、1.20 秒，静态/格式 48 文件、自测和保护检查均退出 0；额外执行 `conda run -n apsgo_v6_3.10.18 python -m compileall -q src tests tools` 退出 0。两个数量的差异仅为未纳入本修复提交的 61 项映射测试，不能混称双树相同累计；5.19 仍独立复测提交。补齐本文后再导出最终暂存树复测，核对提交树身份。

原清单、生产源码及输入/门槛未改；未运行求解、性能或外部 V3 清单。
