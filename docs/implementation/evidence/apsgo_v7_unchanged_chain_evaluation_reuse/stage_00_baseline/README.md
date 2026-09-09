# 阶段 0：冻结最新输入与完整无复用参考

实施前提交 `70c8494ad1fa5aff68ba4b502e03ce1f0c6b343d`；macOS ARM64，Conda `aps_3.10.18` / Python 3.10.18。用户已批准按专项计划持续实施。本阶段只改工具观察快照和测试，不改变生产计算。

## 1. 输入与源码身份

- 原归档：`/Users/miles/Desktop/02b74678-70d6-4bd4-be12-c6f7c944a13d.7z`。
- 归档 SHA-256：`8b855a6caa225cbeabb4c75aab27aa257b2faf699bcc7641eaee2ecf8f58d9cc`；目录项核验为一个目录及六个普通文件，无链接或越界路径。
- 原样冻结：`diagnostics/unchanged_chain_evaluation_reuse/frozen/`，内含归档副本及 `20260908_205419_4a73iqo7/`，属于忽略目录，不提交订单。
- 绑定请求字节 SHA-256：`11bdd096717414a25b14e7a219b860e6e84953f17e9c02bbf8e298da9266829c`。
- 请求指纹：`6609ba5853a20e3bcab137e57d51b5504659c4e7c2ea9dfc333545d13ca85a3f`；策略仍为 900 秒总时限 / 10 秒收尾 / 200000 次候选 / 种子 590531，无覆盖参数。
- 生产与 `590faed` 无差异；工具记录的生产源文件汇总 SHA-256：`7cd98513e1f1d0b1ea15b62b59a7fc25ae1644b40ef5576ac712f6d7c511c392`，逐文件表保存在 `input_identity.json`。
- `tools/profile_solver_search.py` SHA-256：`822239430ee9c06a4d0db87d5da9ea039b219b660737089fe819292ab08cc581`。测量时 Git 如实记录上述 HEAD 和仅工具/测试未提交修改；它们随本阶段提交，后续两侧使用同一工具字节。
- YAML 和 SQLite 的保护哈希与计划第 2.2 节相同；本阶段未读取现场库重新组装请求。

## 2. 命令与产物

在 V7 根目录执行，两个命令均退出 0，输出目录均为新目录：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/profile_solver_search.py \
  --prepared-request diagnostics/unchanged_chain_evaluation_reuse/frozen/20260908_205419_4a73iqo7/prepared_request.json \
  --output-dir diagnostics/unchanged_chain_evaluation_reuse/stage_00_check --scope check

/usr/bin/caffeinate -i /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -B tools/profile_solver_search.py \
  --prepared-request diagnostics/unchanged_chain_evaluation_reuse/frozen/20260908_205419_4a73iqo7/prepared_request.json \
  --output-dir diagnostics/unchanged_chain_evaluation_reuse/stage_00_full_baseline --scope full
```

临时防空闲休眠仅覆盖测量进程生命周期，没有永久修改电源设置。再次复现须换新输出目录，不覆盖本次文件。

`stage_00_full_baseline/measurement.json` SHA-256：`e892c421c687ebfbec0697651ffd2ae19fc6a449cdcf7f7b4a308f41dfc491d4`；同目录 `input_identity.json` SHA-256：`6be2dd3dcc63ab0348aa4766a9dca426820316c0e1a7f34d7afb816e54562bc7`。

## 3. 实测结果

| 范围 | 经过时间 / CPU（秒） | 候选检查 / 完整评价 / 采纳 | 七级质量 |
|---|---:|---:|---|
| 首轮 | 65.895878 / 65.841616 | 64226 / 2263 / 34 | `(2,170.3,2,354.87,11186,480,22)` |
| 完整观测调用 | 131.571310 / 131.422517 | 200000 / 3697 / 54 | `(0,0,0,0,11726,660,23)` |

完整调用内的阶段计时：构图 2.064084 秒，最小路径覆盖 0.431486 秒，初始构造 0.215089 秒，首轮阶段 66.001044 秒，拆单及重放 23.045428 秒，宽差优化 38.866075 秒。首轮独立计时不含前后快照；完整调用包含观察快照及封装开销，不等于真实 HTTP/前端延迟。

停止原因 `candidate_limit_reached`，未触及墙钟；两类拆单各 1 次，核心和契约审计均通过。正式业务计数不因增加观察字段改变。

| 身份 | 值 |
|---|---|
| 初始完整评价 | `b2476f565a7a9710ceb0a91b11223ee9d5c4ea67a15d8c29989bfc3f9b94ab4f` |
| 首轮完整评价 | `86b3596fad9e864b338ba916c9e093569245bd4558f823223e2db3721738c41c` |
| 最终完整评价 | `8856c354afc0da5a4cae3e27d7c4d6aa63bb131e2ea4bfa6f0e5030b0c5816ff` |
| 最终方案 | `9d5df3c86e53ba47bc9f58be7ab122c9146bcff826032d4dbfb18cdf17835dff` |
| 最终轨迹 | `1a15b6e4fd636b85bbc23f1794f9d2049a3d30d5e52497583dc6f31063ecdd22` |
| 公开求解结果 | `1c2820ae2fd5b19273636de8b7d33453db9089c3b600067958a79325e3c127bb` |

结果和轨迹指纹与最新现场诊断一致；现场 EXE 没有明确源码发布清单，故这里只确认业务重现，不将其当严格 Windows 源码性能对照。后续基于本机明确源码及完整有序对象逐字段比较，不能只比较上述指纹。

## 4. 验证与边界

`tests/app/test_solver_profile_tool.py`：8 项通过，0.45 秒；Ruff 通过，退出 0。新增测试直接检查观察到的真实状态、完整链评价顺序及指纹，不改变求解入口。开始前干净导出 `/tmp/apsgo-v7-reuse-stage0-base.yIyNZX` 残留检查 `clean_export` 通过。共享和最终精确暂存树累计命令按计划第 6 节执行，结果记录在本阶段提交正文，避免内容自引用。

只完成基线冻结与观察能力，不宣称未变链复用已实施或已有性能收益。下一项：阶段 1 共用评价组装。
