# 步骤 2：合规链间单节点移动与交换

实施前 `cddfa3a`，macOS / Darwin arm64 / zsh，Conda `apsgo_v6_3.10.18`。开工当前提交干净导出原残留检查 `clean_export` 通过；`main` 及 V6 原仓库不变。

## 范围

只在 `core/width_optimization.py` 实现单节点轻量描述和两条链共同形成候选的私有路径。现有七级目标、规则、共享预算、节点身份、桥工厂及唯一完整接受器复用；没有新依赖、第二评分器、新领域状态或公共流程接线。

移动/交换交替，候选先扣额度再做业务判断；两条链同时验证，不发布单侧半成品。真实来源期规范化在宽差预筛之前，内部订单移动导致的期归属变化也能参与。原始超重提前拒绝，原始欠重允许先尝试必要连接桥，最终仍须满足链重及全部规则。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/core/search/test_width_optimization_nodes.py tests/core/search/test_width_optimization_baseline.py tests/core/search/test_width_optimization_guard.py tests/core/search/test_width_optimization_scan.py -q
PYTHONDONTWRITEBYTECODE=1 conda run --no-capture-output -n apsgo_v6_3.10.18 python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q
```

新枚举器之外的已有保护、反例和扫描先行回归 98 项通过，0.71 秒。初次 Ruff 仅报告导入排序，使用本机已有 Ruff 自动排序和格式化，无业务修改。新增实际生成案例、聚焦/累计回归以及最终暂存树同范围结果在提交前补充，提交正文记录最终树和各实际命令、退出码、数量、耗时。

新增节点案例 24 项，首轮无失败；与守卫、扫描合计 119 项通过（0.77 秒），Ruff 和格式检查通过。覆盖实际移动/交换、确定性排序与重启、内部移动归期、供体断口和接收方两个接口、必要桥补足 680→700 吨/增加后超重、完整连续规则、旧虚拟保全、已有拆片期锁、取消和异常不发布部分结果。最终共享树与暂存树导出按上面相同的聚焦/累计范围验证，数量、耗时、退出码在本项提交正文记录。

独立只读复核从实际枚举器运行“只有交换才不欠重”的样本：700→300 mm，33 次检查、1 次完整评价、自然结束。主代理对步骤 0 五节点样本运行同一实际扫描：700→400→300→100 mm，48 次检查、3 次完整评价/接受，自然结束。直接 `python -c` 首次未设置源码路径，错误加载环境中旧包并报模块不存在；加 `PYTHONPATH=src:.` 后退出 0，未修改环境包。正式 pytest 已由 `pyproject.toml` 绑定 `src`，不受此试验命令遗漏影响。

本步的小样本通过不等于 GQGA4 收益或性能验收；完整接线在步骤 5，实际收益及规定性能另在步骤 6～7 核验。
