# 交期后端源码与集中回归记录

## 验证范围

实施前提交 `3dfbee8`。本阶段只修正三个测试的已批准契约预期与专项文档，不修改生产算法；真实对照使用的生产源码逐文件哈希与最终生产源码全部一致，整体 SHA-256 为 `ee4443a4eae011e62a96cd444583dacaf445fbe20fee2537a43ab510b7265ac0`。

- 两个核心测试自己的问题签名投影保留旧版空计时省略；没有复用生产函数来制造同源自证。
- API 字段清单加入已实现的可选 `delivery_timing`，原订单/原型字段不变。
- 不删除有效业务测试，不调整正式门槛；用户授权删除的两个失效并行文件由 `a141b03` 独立记录，可从 Git 恢复。

## 实际执行记录

平台 macOS ARM64，运行环境 `/Users/miles/anaconda3/envs/aps_3.10.18/bin/python`，测试使用 `PYTHONDONTWRITEBYTECODE=1`、`-p no:cacheprovider`。

| 检查 | 精确范围 / 命令 | 结果 |
|---|---|---|
| 首次导出累计 | 暂存树 `071044966ab7662bb3ad13d7689c97777b4db110`，`/tmp/apsgov7-delivery-check.UFMvsp`，architecture/api/app/core/service | 3868 通过、1 失败，55.23 秒；唯一失败是旧请求字段清单没有新增计时块 |
| 字段清单补正 | `pytest -p no:cacheprovider tests/api/test_request_contracts.py -q --tb=short` | 55 通过，0.14 秒，退出 0 |
| 最终精确导出累计 | 暂存树 `79275e7cd7a7522513b112971c3ea8c67e3aa4ab`，`/tmp/apsgov7-delivery-verified.jdh4fr` | **3869 通过，54.27 秒，退出 0** |
| 干净导出残留检查 | `python tools/check_workspace_residuals.py --verify` | `status=pass, mode=clean_export`，退出 0 |
| 语法 | 导出目录 `python -m compileall -q src` | 退出 0，仅导出目录产生字节码 |
| Python 包 | 导出目录 `python -m pip wheel --no-deps --no-build-isolation --no-index --wheel-dir /tmp/apsgov7-delivery-verified.jdh4fr/wheels .` | 退出 0，无联网/安装新依赖，包 192195 字节 |
| 包内容 | 标准库 ZipFile 只读检查三个交期模块及旧并行文件 | 三个新模块齐备，旧 worker 不在包内，共 59 个条目 |
| 数据保护 | 原表、正式 SQLite、YAML SHA-256 | 与阶段 0 一致，无更改 |

最终累计命令：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python \
  -m pytest -p no:cacheprovider \
  tests/architecture tests/api tests/app tests/core tests/service -q --tb=short
```

最终提交在上述已验证代码/测试树之外只补执行记录和状态文案，不再重复累计。`ruff` 在项目环境未安装，未新增依赖，不能宣称已执行格式工具检查；`git diff --check`、语法与测试已执行。Python wheel 不是 Windows EXE，Windows、HTTP/前端适配与正式库激活均未实施。

## 原始对照文件索引

以下相对路径均位于项目根目录，文件保留在忽略目录，不覆盖旧证据。

| 文件 | SHA-256 |
|---|---|
| `diagnostics/delivery_objective/timing_source.json` | `8444fa93449790cdddac864c91644d6c246ace7947f735b3a51f25e999dbc124` |
| `diagnostics/delivery_objective/old_01/measurement.json` | `c10fac7ea3e775d13dc86e47b33cb031dcb15c0bb1a0325408e12360062ee1fd` |
| `diagnostics/delivery_objective/delivery_01/measurement.json` | `a486c1d970d5c259b0026d13b3f87f62a1073d8566dc52a4f585ab9d5b12e427` |
| `diagnostics/delivery_objective/delivery_02/measurement.json` | `bfbd6b8fb7947427a6c4e01f954ef7135eafa5b93f151f5c426dc56dd41c6233` |
| `diagnostics/delivery_objective/comparison.json` | `ba70ebc2aa8a3a23be5a7fdf4e1a18d538fa8b120b2d8b155bcc5f853ef0347b` |

## 最终边界和待决问题

后端计时、目标、搜索接线、独立审计、数据准备和重放均已实现且验证，但本轮真实整体验收为 **BEST_EFFORT**：新方案有 1 条欠重链 / 41.01 吨缺口，运行约 208 秒，不能替换旧七级正式方案。交期改善本身不等于前四级质量合格。

后续需要讨论修复优先与交期移位的阶段边界，以及减少整链移位反复完整评价；这是下一轮流程/性能修订。当前不增加额度、不降低规则门槛、不偷偷改为旧方案兜底、不声称全量排程验收完成。临时导出和 Python wheel 仅为本机验证保留；没有部署、合并、推送或启动服务。
