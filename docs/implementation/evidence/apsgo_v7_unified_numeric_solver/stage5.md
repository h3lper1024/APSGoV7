# 阶段 5：首轮四类搜索、两类拆单和唯一重放

## 5.1 首轮公共候选接线

实施前 `d7f9715`。整链调整（追加、前置、插入）、真实订单移动、虚拟材填充、整链调序保持原调用、枚举、直接边前检、扣额和首改善次序，统一进入 `compute_candidate_attempt()`、`consume_candidate_result()` 和唯一发布。阶段原最大链重/虚拟比例检查以政策输入保留，不统一收紧为零禁止违规。

链重/最早交期锚点、反向轮廓和直接可连性使用编译数值原语；反向许可仅在原先第一次会计算的位置按代次缓存，不提前产生潜在错误。链内容借用方案切片，不调用 `_layout()` 展开全方案；位置/原型仍按原 Python 控制循环推进，未声称全部控制都已编译。候选工作区按当前方案复用，容量不足重算同一描述，不另扣额度。拒绝不扩展正式任务或生成正式方案/指纹，接受走已验证公共发布边界。首轮整链调序保留空 `affected_rows`，不同于精修的移动行轨迹。

### 真实首轮对照

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_search_prefix.py \
  --source-root /tmp/apsgo-unified-stage0-V92qIO \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage51_reference_01

PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python tools/verify_unified_numeric_search_prefix.py \
  --source-root /Users/miles/dev/dev-py/APSGOV7 \
  --prepared-request diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json \
  --output-dir diagnostics/unified_numeric_solver/stage51_native_02

cmp diagnostics/unified_numeric_solver/stage51_reference_01/search_prefix.json \
    diagnostics/unified_numeric_solver/stage51_native_02/search_prefix.json
```

均退出 0；`search_prefix.json` SHA-256 都为 `df91e8c874be4bd1e6ab299d336154bc6e8e7361c19936a4c9a426bb45a97472`。对照包含从构造开始的身份、四类动作结束状态、所有接受记录、完整扣额流顺序摘要、最终节点/链序/资源身份与祖先。

| 阶段结束 | 累计候选检查 | 累计完整评价 | 累计接受 | 虚拟序号 |
|---|---:|---:|---:|---:|
| 整链调整 | 1927 | 206 | 9 | 6 |
| 真实订单移动 | 69723 | 229 | 24 | 6 |
| 虚拟填充 | 72580 | 521 | 34 | 16 |
| 整链调序 | 110964 | 848 | 361 | 16 |

桥接需求 1920 次、21 链；此时仍有 2 项禁止与 1 条欠重，**是冻结首轮相同的中间状态，不是最终发布结果或质量通过**。本项没有运行拆单、唯一重放、精修及双审计。

`stage51_native_01` 是首次数值接线诊断，结果也字节一致；后把原直接位置前检保留在数值描述准备前，避免对明显不可连接位置多做准备，以 `_02` 复验。冻结/新 `_02` 冷诊断经过时间为 33.427640 / 68.276595 秒，CPU 为 33.326179 / 67.942104 秒；均含本进程编译和追踪，且与专项测试并发，不能据此作正式性能结论。新内核冷启动成本保留到阶段 7 分开测量，不忽略、不改变 40 万额度。

### 必要验证

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -q -p no:cacheprovider \
  tests/core/test_numeric_first_common.py tests/core/test_numeric_search.py \
  tests/core/test_numeric_candidate_kernel.py tests/core/test_numeric_candidate_publication.py \
  tests/core/test_numeric_refinement.py tests/architecture
```

共享 175 项通过、114.76 秒、退出 0；精确暂存树同范围验证见提交正文。五种直接/资源动作在旧准备函数失败桩下仍正常接受；拒绝零正式物化、从零容量增长不额外扣额、小例拆后调用新首轮也通过。先期新边前检传错既有共享原语参数，实际编译测试拦截后按已声明签名修正，未改变规则；最终测试/真实对照均关闭该失败。

计划 v0.12，下一项 5.2 两类拆单及唯一重放；共享残留只保留原 9 项已登记差异，正式配置/SQLite、原请求和四份用户文件不变。没有完整性能、并行或历史交期改善结论。
