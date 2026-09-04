# 取消独立牌号连接策略验证

- 实施前提交：`c5e0a91b066f6f59fb72938067eb407bb15f23c5`。
- 环境：macOS / Darwin 27.0.0 arm64、zsh、Conda `apsgo_v6_3.10.18`。
- 用户决定：删除功能 5.10，而非暂缓；GQGA4 使用已实现的软硬材连接规则。
- 范围：目标设计、实施计划、AGENTS、本记录，以及已有 `test_process_rule_set.py` 的取消规则参数化回归；生产代码没有改动。
- 目标为原始 17 条中排除借用比例、牌号连接策略后的 15 条（14 启用、1 停用）加独立拆单；原始输入、参考证据、六级评分和门槛不改。

## 检查与结果

取消回归共四例，新增两例：两个已取消类型均未注册，误启用报可定位错误，通用历史停用配置仍参与指纹且不进入执行索引；软硬材类仍注册。复用现有测试，无占位规则或生产黑名单。

| 验证 | 共享树 | 干净暂存树导出 |
|---|---|---|
| 聚焦及软硬材回归 | 263 项，0.30 秒，退出 0 | 263 项，0.34 秒，退出 0 |
| 规则累计 | 633 项，0.64 秒，退出 0 | 633 项，0.68 秒，退出 0 |
| 仓内累计 | 872 项，0.96 秒，退出 0 | 872 项，0.97 秒，退出 0 |

初验导出：`/tmp/apsgo-grade-removal-bEcXGH`，暂存树 `8728e8dd18800ddf141519f9b15b3426390ee76e`。静态、格式、残留和编译均退出 0；独立复核取消守卫及软硬材测试 48 项通过（0.10 秒）。补齐本记录后重新导出最终暂存树并运行同一测试集合，再检查提交树一致。

```bash
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules/test_soft_hard_connection.py tests/core/rules/test_rule_base.py tests/core/rules/test_process_rule_set.py tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/rules tests/app/test_rule_set_loader.py -q
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
/Users/miles/anaconda3/bin/ruff check --no-cache src tests tools
/Users/miles/anaconda3/bin/ruff format --check --no-cache src tests tools
PYTHONDONTWRITEBYTECODE=1 conda run -n apsgo_v6_3.10.18 python tools/check_workspace_residuals.py --verify
# compileall 只在干净导出中执行
conda run -n apsgo_v6_3.10.18 python -m compileall -q src
```

共享树静态检查、格式及差异检查通过，41 个文件格式正确。稳定受保护残留 16 个未变，4 个既有 Ruff 缓存增项保留、不暂存。无测试失败或未解释差异；本次未执行完整求解、规则差分或 V3 目录核验，不宣称功能 5.19 映射或 GQGA4 验收完成。

提交前核对 `src`、原始基线、工具及工程配置未改；最终提交树必须与最后一次干净导出复测树相同。恢复可基于上述实施前提交反向恢复本项五个文件，不触碰其他工作区文件。取消项不计为已实现功能，下一项为 5.11“连续虚拟材料规则”。
