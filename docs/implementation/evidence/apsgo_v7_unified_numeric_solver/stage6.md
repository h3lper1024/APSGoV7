# 阶段 6：精修数值描述、公共候选计算与连接材回收

## 6.1 数值描述与结构归属

实施前 `d8de4dd`。新增 `_numeric_refinement_scan.py` 承载精修特有枚举，不新增规则/修复算法；直接借用权威方案的节点行、链偏移、原单片段索引及节点位置。重点原单/交期排序键、片段拥有者和位置/区间归属使用数值列；区间归属存平铺数组，不构造每节点 Python 字典。

链内移动、节点移动/交换、片段移动/交换、切链、调序和普通连接材回收由实际 Numba 编译生成器产生公共 13 列描述。编译生成器保存原生循环续算状态，深层跳过扫描每 256 步返回控制标记供取消检查；标记不是候选，不计费、不参与 4 次/64 次轮转。节点移动仍直接位置在前、待修复位置在后；节点/片段移动与交换仍交替；切链/调序共享链游标，多个原单不能重新展开同一链的已推进部分。

Python 仅协调原来的原单轮转、通道/动作族轮转、额度与消费。**本项候选准备仍使用此前已验证路径**：临时把 13 列描述的元数据映射到原 7 项描述，既不展开链节点也不重新枚举；原准备所用索引暂保留。该过渡边界在紧接的 6.2 撤除，不称精修修复已经统一。旧生成函数仅作专项对照，不在主流程调用；保护测试将旧生成函数替换为失败桩，主流程仍可推进。

### 真实有序窗口

使用 `tools/verify_unified_numeric_search_prefix.py`，两侧均传入原 `diagnostics/critical_delivery_search_and_bridge_reclamation/combined_01/prepared_request.json`，并加 `--through-split --refinement-checks 20000`。解释器为 Conda `aps_3.10.18`、`PYTHONDONTWRITEBYTECODE=1`。

| 运行 | source-root | output-dir |
|---|---|---|
| 冻结 | `/tmp/apsgo-unified-stage0-V92qIO` | `diagnostics/unified_numeric_solver/stage61_reference_01` |
| 首次新接线 | `/Users/miles/dev/dev-py/APSGOV7` | `diagnostics/unified_numeric_solver/stage61_native_01` |
| 最终边界复验 | `/Users/miles/dev/dev-py/APSGOV7` | `diagnostics/unified_numeric_solver/stage61_native_02` |

从正常前缀 134226 次进入，再消费 20000 次；不清零、不改正式预算。第一次新接线诊断 `_01` 与冻结 JSON 字节一致，摘要 `da2f5a753580bfcab57c258cf31a940e2d11aa7d417e89d0823d662b1d1e5f1d`。19564 个生成提案的动作/拥有者/位置/通道全流摘要为 `040db57df1be371dbf0501ff1477fe72bf7b159a2f6ae29252cbcefb70be16c6`；全部扣额、接受、资源及方案身份一致。

窗口末累计 154226 次检查、6688 完整评价、460 接受，即精修新增 5560 次完整评价/18 次接受；22 链、360 吨虚拟、九级整数键 `[0,0,0,0,1951028,426211618665,11611,36000,22]`。这是人工窗口截断的中间结果，**不是完整 40 万次排程或双审计结论**。旧/新 `_01` 冷诊断经过时间 59.022869 / 121.139408 秒、CPU 58.690071 / 120.519639 秒；包含编译和追踪，存在测试并发，不作为性能样本。

### 必要验证

使用项目解释器执行 `python -m pytest -q -p no:cacheprovider`，保持 `PYTHONDONTWRITEBYTECODE=1`，范围为 `tests/core/test_numeric_refinement_scan.py`、`test_numeric_refinement.py`、`test_numeric_candidate_kernel.py`、`test_numeric_batch.py` 和 `tests/architecture`。

先期 185 项通过、118.91 秒。新增数值边界保护后共享最终 187 项通过、120.56 秒、退出 0；精确树同范围结果见提交正文。测试包括混合重点区间、原游标轮转、共享切链归属、无重点深扫描取消、原提案顺序、批量消费、公共候选和精确依赖边界。开发中规则表传参及边状态数组长度错误已被实际编译测试拦截修正；没有修改规则公式。后续小例补上未使用负交期的减法保护及无切点时不提前触发新链编号越界；最终真实 `_02` 及 `cmp` 均退出 0，仍与冻结 JSON 字节一致，保留两次记录。

正式 YAML/SQLite、原始输入、四份用户文件不变；残留保护及 Ponytail 关闭保持。下一项 6.2 接通精修与回收公共候选计算，尚未进行新并行或完整性能验收。
