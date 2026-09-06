# V7 规则设置接口：阶段 1 规则管理契约验证

## 1. 结论

阶段 1 在不引入 HTTP 框架、数据库或 GQGA4 专属模板的前提下，建立了框架无关的规则管理请求、响应、错误和精确 JSON 边界：

- POST 请求只接受操作标识、期望活动版本、完整规则可编辑项、虚拟材料原型和备注。
- GET 与 POST 成功响应均输出平铺的完整活动版本视图；幂等重放可同时表达原保存版本和当前活动版本。
- 小数字面量直接解析为 `Decimal`，整数暂保留为 `int`；具体规则参数类型留给阶段 2 固定模板归一。
- 顶层、规则项和虚拟材料原型的未知字段严格拒绝；重复 JSON 键、重复业务标识、非法 UUID、布尔值和版本号均返回可定位诊断。
- 序列化输出使用稳定字段顺序与十进制表示，并保证任意合法解析字符串都能编码为 UTF-8；规则参数以根对象为第 0 层，任一值距根最多 64 层。

本阶段不表示 GQGA4 17 条规则编译、SQLite 存储、保存事务、HTTP 路由或 C# 页面已经实现。

## 2. 实施基线与范围

| 项目 | 实际值 |
|---|---|
| 平台 | macOS 27.0，Darwin 27.0.0 arm64 |
| Python | 3.10.18 |
| 分支 | `codex/rule-setting-api-integration` |
| 实施前提交 | `426bf49f9b956cb592570a83a768ad2b6f1dc814` |
| 生产范围 | `src/apsgo_scheduler/api/rule_management.py`、`src/apsgo_scheduler/api/__init__.py` |
| 测试范围 | `tests/api/test_rule_management_contracts.py` |

文档同步范围为本专项详细设计、实施计划、`AGENTS.md` 与本证据。没有修改求解算法、规则实现、正式 GQGA4 配置、数据库、HTTP 服务或 C# 工程。

## 3. 关键契约

1. `save_operation_id` 规范化为小写 UUID；同一人工保存的网络重试必须复用该标识。
2. `expected_active_version_id` 只接受正整数，不能用布尔、文本或小数替代。
3. 请求不接受服务端拥有的规则类型、名称、范围、顺序、版本、评分或指纹字段。
4. 普通新保存的成功响应必须令 `saved_version_is_active=true`；只有幂等重放才可能返回历史保存版本已被后续版本替代。
5. 请求解析与序列化不经过二进制浮点数；`20.0` 与 `20.00` 规范一致，规则参数的 `20` 与 `20.0` 暂时保留类型边界。
6. 阶段 4 的 `request_hash` 不得直接取本阶段原始请求或未归一对象；必须等待阶段 2 按 GQGA4 模板完成参数类型和规则顺序归一。

## 4. 审查修正

独立复审发现并已关闭以下阶段内问题：

- 将字符串 JSON 编码改为 ASCII 转义形式，避免合法的孤立代理字符在后续 HTTP 或 SQLite UTF-8 编码时抛出 `UnicodeEncodeError`。
- 拒绝“非幂等新保存但新版本不是活动版本”的不可能成功状态。
- 顶层缺失与未知字段路径去除不一致的 `body.` 前缀；只有请求体本身不是对象时使用 `body`。
- 删除只锁定 Python 数据类内部字段顺序的测试，改为精确验证 GET、POST 和错误 JSON 的外部顶层字段集合。
- 明确规范化幂等摘要只能在阶段 2 之后生成，并补充后续数据库回读一致性要求。
- 捕获十进制解析异常并冻结 64 层参数边界；长尾零改为一次性清理，避免深层参数和极端数值泄漏内部异常或造成不必要的超线性开销。

## 5. 已完成验证

| 范围 | 命令 | 结果 |
|---|---|---|
| 专项契约 | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python -m pytest -p no:cacheprovider tests/api/test_rule_management_contracts.py -q` | 44 项通过，退出码 0。 |
| 接口与架构聚焦 | 同一解释器运行阶段 1 契约、既有请求契约及三组架构测试 | 164 项通过，退出码 0。 |
| 共享树最终累计回归 | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core -q` | 2991 项通过，179.01 秒，退出码 0。 |
| 静态格式 | `/Users/miles/anaconda3/bin/ruff format ...` 与 `ruff check ...` | 通过。 |
| 文本差异 | `git diff --check` | 通过。 |
| 预提交树残留 | 从暂存树 `2e05d45a045cdbac37ef2b6b8621ad9c52c2830b` 导出到新临时目录，运行 `/Users/miles/anaconda3/envs/apsgo_v6_3.10.18/bin/python tools/check_workspace_residuals.py --verify` | `clean_export` 模式通过，退出码 0。 |
| 预提交树累计回归 | 在同一干净导出中、构建前运行累计测试 | 2991 项通过，172.17 秒，退出码 0。 |
| 预提交树编译与构建 | 在同一干净导出中依次运行 `python -m compileall -q src`、`python -m build --no-isolation` | sdist 与 wheel 构建通过，退出码 0。 |

补记上述结果会改变暂存树，因此提交前还需重新取得最终暂存树并在新的干净导出中执行同一顺序；最终树身份和结果写入提交记录，不在提交内容中自引用。

## 6. 下一阶段

下一阶段是“实现 GQGA4 完整规则编译”：按固定 17 条模板归一参数类型与规则顺序，注入七级评分和允许偏差，并复用现有指纹与权威规则加载器完成双重回读验证。

阶段 1 复审同时核验了后续初始化的开发来源：冻结输入 `tests/baselines/gqga4/inputs/optimization_problem.json` 的 SHA-256 为 `8edb7f4110384af14d58e9bdc7caec490f3de053401173db952854275966abdc`，其中按原顺序包含 27 个虚拟材料原型，首尾标识为 `virtual_sphc:1000x0.4`、`virtual_sphc:1500x2.5`。按现有 V7 值契约解码后的原型有序元组指纹为 `33cea496fa6909ec53e6048f142776eba25d2bdca19fee9b565f775b64df965c`。阶段 2 与阶段 6 应把等价内容固化为服务包内生产种子并做等价测试；这项核验不表示生产种子或初始化已完成。
