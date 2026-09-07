# APSGo V7 月计划求解接入与软硬钢数据准备详细设计

## 1. 状态、目标与边界

| 项目 | 内容 |
|---|---|
| 版本 / 日期 | v0.6 / 2026-09-07 |
| 状态 | 阶段 0～6 已实施；C# 求解服务/回写、真实数据完整联调和正式数据库迁移待实施 |
| 用户本轮授权 | 按实施计划持续实施；每阶段独立验证和提交，业务语义需要确认时暂停 |
| 适用范围 | `GQGA4/default/month`，C# 月计划前端与 V7 独立服务 |
| 专项初始基线 | `codex/rule-setting-api-integration@072a6d0`，写文档前工作树干净 |
| 配套计划 | [月计划求解接入与软硬钢数据准备实施计划](../implementation/apsgo_v7_month_scheduling_and_grade_preparation_implementation_plan.md) |

目标是让月计划原始订单通过 V7 专属接口完成求解并回写：服务端从已迁移的牌号字典补齐软硬钢分类，绑定同一版本的规则、字典和虚拟原型，再调用已有求解入口。本文中的“求解改造”指输入准备、任务绑定、HTTP 调用、结果转换和前端接入；现有构造、局部搜索、受控拆单、链间宽差精修及七级评分继续使用。

用户已确认：C# 使用 V7 专属配置、API 客户端和求解服务；保留用户将 V7 地址改为读取 `PipelineV7ApiBaseUrl` 的修改；V3 客户端、服务和其他产线路径继续保留。本文不替代原规则设置接口设计的业务语义，仅扩展其数据库快照和 V7 配置使用方式。旧文档中“V7 使用 `BACKEND_ALGORITHM_URL`”及“服务只有两个规则接口”是本次变更前的事实，目标态以本文为准。

阶段 0～6 已按配套计划实现并逐项验证；月计划传输已接入现有 FastAPI 宿主及 YAML 策略，C# 已建立 V7 专属配置和求解客户端。后续继续逐项创建中文 `#feat` 或 `#fix` 提交，正式数据库迁移仍留到阶段 9 的明确操作窗口。

## 2. 已核验的现状

### 2.1 代码与数据库

| 对象 | 当前事实 | 一手依据 |
|---|---|---|
| V3 字典 | 默认 SQLite 的 `aps_gqga4_grade_dictionary`；按产线和启用状态查询 | [SQLite 仓储](/Users/miles/dev/dev-py/apsgo-v3/rules_engine/db/sqlite_repository.py:126) |
| V3 拼接 | 按订单 `grade` 去空格、转大写精确匹配；补分类、辊型和 IF 标志 | [字典拼接](/Users/miles/dev/dev-py/apsgo-v3/rules_engine/grade_dictionary.py:18) |
| V3 连接判断 | 普通材同软硬分类可连接；分类缺失时按配置检查相同热轧牌号 | [连接规则](/Users/miles/dev/dev-py/apsgo-v3/rules_engine/dsl/runtime.py:961) |
| V7 存储 | 生产代码支持 schema v2 及版本化牌号字典；正式目标库尚保持 schema v1，待阶段 9 迁移 | [rule_store.py](../../src/apsgo_v7_service/rule_store.py) |
| V7 绑定 | `bind_gqga4_scheduling_task()` 在同一事务读取并核验活动规则、字典和原型，再补齐订单分类 | [scheduling.py](../../src/apsgo_v7_service/scheduling.py) |
| V7 输入 | 通用标准化器消费已补齐的 `rule_attributes.soft_hard_class`；数据库派生限定在 V7 服务绑定层 | [input_normalizer.py](../../src/apsgo_scheduler/app/input_normalizer.py)、[grade_dictionary.py](../../src/apsgo_v7_service/grade_dictionary.py) |
| V7 规则 / 缓存 | 已消费软硬分类；边语义声明包含分类、热轧牌号及材料角色 | [concrete.py](../../src/apsgo_scheduler/core/rules/concrete.py)、[compatibility.py](../../src/apsgo_scheduler/core/compatibility.py) |
| V7 HTTP | 同一宿主现提供两个规则设置接口和一个月计划求解接口；C# 传输客户端已建立，求解服务、页面回写与正式部署尚未完成 | [app.py](../../src/apsgo_v7_service/app.py) |
| 旧完整测试 | 从冻结 `optimization_problem.json` 预填软硬分类，不是原始订单在线拼接 | [输入夹具](../../tests/app/test_input_normalizer.py) |

外部 V3 证据根目录为 `/Users/miles/dev/dev-py/apsgo-v3`，核验提交 `e5bdcdfd3dd1ed880037d28159bfe8b6bef5d15f`。默认源库为该目录下 `data/aps_rule_dsl.sqlite3`，本轮只读核验 SHA-256 为 `2c4e44c4b4c2060cb54890217ea7097164b4c88e25a452796a931883df4cce7e`。这说明当前本地证据，不代表其他部署环境使用同一数据库。

### 2.2 字典与订单覆盖

| 检查 | 现场结果 |
|---|---|
| 源表总行数 | 288；GQGA4 为 230，GQGA5 为 58 |
| 本期迁移范围 | 仅 GQGA4 的 230 行，全部启用 |
| GQGA4 分类 | 软钢 131、硬钢 99 |
| 空牌号 / 空分类 / 规范化重复 | 均为 0 |
| 大小写 | `St04D+Z`、`St280D+Z` 需按 V3 方式转大写后匹配 |
| 正式 531 单 | 40 个牌号；529 单命中，2 单未命中 |
| 唯一未命中牌号 | `HC220YD+Z-GL`，两单热轧牌号均为 `H220Y` |

未命中订单为 `0030125170-000010` 和 `0030124824-000050`。原冻结输入中的分类也为空。字典有 `HC220YD+Z`，但 V3 的 GQGA4 逻辑不删除 `-GL` 后缀。首版沿用精确匹配和已有缺失兜底；增加别名属于后续业务数据维护，不在迁移时猜测。

本轮使用 Conda `aps_3.10.18` 调用 V3 原生产 `load_order_nodes → enrich_nodes_with_grade_dictionary`，对照冻结 `optimization_problem.json` 的逐单分类：531/531 一致，差异 0；实际订单分类为软钢 525、硬钢 4、空 2，其中 61 条实际过渡材均命中软钢。这是输入核验结果，不是新 V7 求解路径已经验收。

V3 单次拼接采用整表查询后内存字典查找，但在滚动全局准备和各期准备中会重复读库。V7 在一次任务绑定时取得一个固定快照，整个求解过程沿用它。

## 3. 整体职责与流程

| 顺序 | 执行位置 | 输入与输出 | 采用方式 |
|---:|---|---|---|
| 1 | C# 月计划页面 | 选中记录定位版本，读取该版本/产线“配置规则”步骤的全部记录及预设大辊期 | 保留原业务取数范围 |
| 2 | C# V7 求解服务 | 构造原始订单请求与明确期序 | 参考现有 GQGA4 字段映射，使用 V7 专属类 |
| 3 | V7 HTTP 入口 | 校验协议和字段，进入任务绑定 | 新增薄适配层 |
| 4 | V7 数据准备 | 同一事务读取活动规则、字典与原型；补软硬分类和材料角色 | 迁移 V3 字典数据，V7 重新实现拼接 |
| 5 | V7 公共求解入口 | 完整 `SchedulingRequest` → 求解结果 | 复用 `solve_request()` |
| 6 | V7 结果适配 | 仅从已审计的 `release` 生成完整结果行 | 新增传输映射，保持发布顺序 |
| 7 | C# V7 求解服务 | 核对来源、重量、状态，原子回写 `SchedRecord` | 新服务沿用现有实体和后续统计 |

数据库和 HTTP 依赖均位于 `apsgo_v7_service`。`apsgo_scheduler.api/core/app` 继续只接收完整内存值，不引入数据库路径、连接、产线字典查询或 C# 字段名。

## 4. 字典保存与版本设计

### 4.1 物理保存

继续使用 YAML `database_path` 指向的 V7 SQLite；本机默认解析到 `/Users/miles/dev/dev-py/APSGOV7/data/apsgo_v7_rules.sqlite3`。新增 `v7_grade_dictionary_entry`，以规则集版本为所属快照：

| 字段 | 类型 / 约束 | 用途 |
|---|---|---|
| `rule_set_version_id` | INTEGER，外键 | 所属不可变规则版本；产线从父记录取得 |
| `source_grade` | 非空 TEXT | 保留 V3 字典原文牌号 |
| `normalized_grade` | 非空 TEXT | `source_grade.strip().upper()`；实际匹配键 |
| `soft_hard_class` | TEXT，仅 `软钢`、`硬钢` | 求解使用的分类 |
| `roll_type` | TEXT | 保留迁入的辊型信息，当前不改变 V7 求解约束 |
| `steel_classes` | TEXT | 原样保留多值类别，例如含 `|` 的文本 |
| `is_if_steel`、`enabled` | INTEGER，仅 0 或 1 | 源数据事实和启用标记 |
| `source_file`、`source_rows`、`remark` | TEXT | 原始来源说明；允许可选说明为空 |
| `source_row_count` | 非负 INTEGER | 原 Excel 来源行数量 |

主键或唯一键为 `(rule_set_version_id, normalized_grade)`。V3 自增 `id` 和历史建表时间不作为 V7 业务身份；原始导出证据保留它们。加载和导入两处均验证规范化一致性、重复、分类值域、布尔和来源结构；不接受 `软钢|硬钢` 作为第三种分类。

表遵循既有规则明细的不可变保护：禁止更新、删除和向已活动或历史版本追加行。新版本在激活前完整写入；求解只使用 `enabled=1` 的条目，但快照指纹覆盖包括停用项在内的完整字典。

在 `v7_rule_set_version` 增加可空的 `grade_dictionary_fingerprint`。空值仅表示迁移前历史版本没有记录字典，不意味着空字典有效。新建 GQGA4 版本必须拥有完整字典及非空指纹。

### 4.2 指纹口径

“指纹”是规范化内容的稳定摘要，用于检查求解前后是否使用了同一份数据；它不是签名或数据库主键。复用现有 `fingerprint()`，按 `normalized_grade` 排序，对以下内容计算字典指纹：

- 身份：`product_line_code`，当前为 `GQGA4`。
- 每行：上表除 `rule_set_version_id` 外的全部字段；布尔转为约定的 bool 值、行数为 int、文本保留已验证值。

数据库自增编号、导入时间不参与；复制到新规则版本而字典内容未变时，字典指纹不变。`rule_set_fingerprint` 继续按原 `RuleSetSpec` 公式计算；规则版本字符串本身在公式内，因此建立新版本即使规则参数未变，规则指纹也可能改变，不能承诺保持旧指纹。

### 4.3 保存、读取、恢复

| 操作 | 字典处理 |
|---|---|
| 新库初始化 | 显式提供经核验的 V3 SQLite 源库，230 行与初始规则、原型同事务写入；不内置 JSON 副本 |
| 页面保存并启用 | 在并发检查成功后，复制当前活动版本字典到新版本；页面不用往返字典 |
| 同一保存操作重试 | 先查已有操作，保留原有幂等语义；不重新复制，不将旧保存重新激活 |
| GET 活动规则 | 原公共响应字段保持；内部验证已具备字典的版本一致性 |
| 开始求解 | 同一个只读事务获取并核对活动版本、规则、字典、原型；事务结束后冻结使用 |
| 恢复历史规则 | 仍恢复历史规则与原型为向前新版本；字典沿用恢复操作前的活动字典，避免恢复规则时隐式回滚主数据 |

规则恢复命令可读取迁移前历史规则，但必须在目标库已具备有效活动字典后才能创建新版本。恢复日志明确记录“来源规则版本”和“沿用字典指纹”。若以后要回滚字典，单独设计受控数据变更入口；本期没有字典编辑页面或通用维护 API。

## 5. 从 schema v1 迁入的设计

schema v1 旧版本永久保留空字典身份，不回填历史行。显式迁移命令先只读解析并验证源字典，再在备份保护下升级目标 schema 为 v2，并在同一写事务内复制目标库当前活动规则和原型、附上字典、建立向前新版本、最后激活。新版本必须使用目标库当前实际规则参数，不能用默认种子覆盖用户已经保存的配置。

迁移边界：

1. 源路径和目标路径必须不同，源库只读；生产运行不依赖 V3 路径。
2. 对目标库使用 SQLite 一致性备份，记录准确绝对路径、源库摘要、230 行导出摘要、备份完整性及操作标识。不能在有 WAL 的情况下只复制主文件作备份。
3. 验证旧 schema、触发器和历史数据后执行升级；旧规则、原型、请求摘要、操作标识及历史行内容保持。
4. 迁移用独立操作 UUID 和预期活动版本控制重试、并发；同 UUID 重试还要核对源字典指纹。已完成操作仅回报原结果；当前版本被替代时不重新激活。
5. 任一字典校验、编译、写入或回读失败，整个 schema 与新版本事务回滚；服务普通启动不自动执行迁移。
6. 新库初始化和已有 v1 库升级都必须显式提供只读 V3 SQLite 源库；生产包不保存第二份 JSON 字典，普通初始化器和服务启动不能悄悄导入或升级。

`RuleStore` 当前会严格验证 schema，实施时必须同时更新 schema 版本、完整表/列/索引/触发器验证、历史读取和初始化路径。不能只新增一张表就宣布升级完成。

迁移命令使用专用旧版本打开路径，复用底层连接但完整核验 v1 签名，不调用已切换为 v2 校验的普通 `open()` 或会单独提交的 `initialize()`。由一个 `BEGIN IMMEDIATE` 覆盖 schema 变更、新版本及字典写入、激活与回读，最后写 `user_version=2` 并提交；任何失败均回滚。新建 v2 库与迁入 v2 库都须通过同一当前 schema 验证。

重入已为 v2 的目标库时，先按迁移操作 UUID 查原记录，核对原预期版本和字典指纹；一致则返回原保存版本及其当前是否仍活动，不重新激活。不一致报操作冲突；查无同一操作则拒绝“已由其他操作迁移”。普通规则保存不可复用迁移操作身份。迁移时保留当前规则、虚拟原型、备注和页面快照，以新业务版本号重新编译。

## 6. 求解前数据准备

### 6.1 唯一拼接位置

在 `apsgo_v7_service/scheduling.py` 的任务绑定中，取得规则与字典快照后、构造正式 `SchedulingRequest` 前调用纯函数完成补齐。字典读取由服务仓储承担；纯函数只接收订单和不可变字典映射，不自行查库。

对 HTTP 原始订单，先做类型与身份校验，再映射为 `OrderInput`；对已有服务层 `SchedulingTaskInput` 调用，也必须经过同一补齐函数，防止 Python 调用绕过字典。直接调用通用 `apsgo_scheduler.app.service.solve_request()` 仍属于“调用者已经提供完整输入”的公共契约，不在通用求解器里查字典。

### 6.2 精确匹配与冲突

| 情形 | 处理 |
|---|---|
| 订单 `grade` 有效 | 去前后空白、转大写，按规范化键精确匹配 |
| 命中启用记录 | 写入 `rule_attributes.soft_hard_class` |
| 未命中 / 对应条目停用 | 写入 `None`；按牌号记录受影响订单，不删除订单、不默认软钢 |
| HTTP 传 `soft_hard_class` / 材料角色等派生字段 | 作为未知或禁止字段拒绝，避免客户端替代服务端分类 |
| 服务层 typed 输入已有非空分类 | 与字典派生结果相同可通过；不一致或字典未命中却预填非空时，返回冲突诊断 |
| 字典整份缺失、为空或损坏 | 返回配置错误，求解不开始；不同于单个牌号未命中 |
| 空牌号 / 非文本牌号 | 输入错误，不进入字典匹配 |

普通真实材和真实过渡材都补齐分类。真实过渡材由原始客户等级、热轧牌号、执行标准按已有 GQGA4 三条件判定，与软硬分类分开。虚拟原型来自绑定的规则版本，不按订单字典二次分类；生成型虚拟材依现有虚拟角色参与规则。拆单片段继承父订单已经冻结的属性，不重新查字典。

三条件精确定义：客户等级去空白后不等于 `战略客户`、热轧牌号去空白转大写后等于 `SPHC`、执行标准去空白后等于 `Q/TB 305-2017`，须同时满足；客户等级空值按既有判定视为空文本，其他条件仍须满足。其余订单为普通真实材。实际过渡材当前不可受控拆单；生成型虚拟材包括拆单分隔材，只继承原型属性，不继承相邻真实单的软硬分类。

V3 还填入 `roll_type/is_if_steel`；本期仅保存这些源字段用于追溯，不据此改变 V7 材料角色、钢种大类、窄钢判定或连接顺序。`steel_classes` 也不覆盖原订单的 `grade_class`。

### 6.3 连接规则继续使用现有语义

按当前规则实现的优先顺序：先看生成型虚拟材桥接开关，再看真实过渡材桥接开关；普通真实材两侧均有分类时比较分类是否相同；任一分类缺失时遵循规则参数 `missing_grade_policy`。当前 GQGA4 配置为 `fallback_same_hot_roll_grade`，要求双方热轧牌号非空且规范化后相同。软硬分类不同且完整时，不能再以相同热轧牌号兜底。

停用软硬连接规则时，字典补齐仍用于数据准备和输出，但该规则不产生连接限制；其他规则照常执行。

### 6.4 准备报告和任务身份

新增服务层成功准备报告，包含字典指纹、输入真实订单数、命中数、未命中数、按规范化牌号排序的缺失明细和对应 `source_order_id`。已有非空分类冲突、非法字段和生成型虚拟输入不产生“成功报告”，统一由 `GradePreparationError.issues` 返回定位诊断并阻止求解。同一原始订单只统计一次，拆片和虚拟产出不增加准备阶段数量。

`BoundSchedulingTask` / `BoundSchedulingResult` 新增字典指纹和准备报告；`binding_fingerprint` 同时覆盖活动版本 ID、规则指纹、字典指纹、准备报告指纹和最终请求指纹。输入属性影响已有请求/问题指纹。核心 `RunManifest` 保持现有职责；外层响应和验收产物保存字典身份，不能把规则指纹冒充字典指纹。

## 7. 月计划求解 HTTP 契约（本次设计）

### 7.1 路由与行为

已新增 `POST /api/v1/scheduling/GQGA4/default/month/solve`，由当前 V7 服务宿主提供。两个规则 GET/POST 地址保持已确认契约。新路由按 V7 月计划语义命名；V3 的 `/api/v1/planning/rolling-strict-productline` 仍由 V3 原服务处理。

首期采用一次同步 HTTP 请求返回完整结果，C# 异步等待。后端线程执行同步求解以避免阻塞事件循环；每个服务进程使用一个非阻塞占用，同一进程最多执行一个求解，忙时返回 `429`，不建立排队任务系统。规则查询与保存不使用该占用，求解期间仍可受理。取消通过请求断开设置线程安全事件并传给现有 `is_cancelled()` 接口；后台求解真正结束前由工作线程持有占用。网络失败不自动重新提交求解，避免重复执行；部署保持单 worker，否则各 worker 会各自允许一个求解。

### 7.2 请求字段

| 字段 | 类型 / 语义 |
|---|---|
| `contract_version` | 固定文本 `v7-month-solve-v1`，与内部通用契约版本分别管理 |
| `request_id` | 非空唯一任务标识，由 C# 生成 UUID；用于核对响应和日志，不承诺持久化幂等 |
| `expected_active_version_id` | 正整数，C# 在本次 POST 前即时调用既有规则 GET 获取；绑定事务内比较，变化返回 `409` |
| `periods` | `{period_id, sequence}` 数组，sequence 从 0 连续；期数不限于 4 |
| `orders` | 下表原始订单数组，保持前端提交相对顺序 |

产线、工序、场景由路由固定为 `GQGA4/default/month`。请求不传规则 JSON、字典、虚拟原型、评分或搜索开关。期序来自预设大辊号的数值升序，不能按 `BR_...` 字符串或列表偶然顺序推断。现有配置规则步骤记录不保存规则版本，不能从 `SchedRecord` 猜测活动版本；GET 与 POST 之间版本变化时保留旧结果，由用户重新点击求解。

| 订单字段 | C# 原始来源 / V7 语义 |
|---|---|
| `source_order_id` | 合同完全号；缺失时沿用现有记录 Id 文本兜底，服务层保持可逆来源映射；重复拒绝 |
| `source_period` | 预设大辊号对应 `BR_{number:08d}` |
| `is_virtual` | 布尔值，由原始记录虚拟标记规范化得到；首期必须为 false，true 明确拒绝而非当真实单接收 |
| `weight` | 当前 GQGA4 的 `CoatingShortage`（连镀欠交吨数）；不回退酸轧欠交或订单欠交 |
| `grade`、`grade_class`、`hot_roll_grade` | 牌号、钢种大类、热轧牌号；只有 grade 用作软硬字典键 |
| `width`、`thickness` | 宽度、厚度，毫米 |
| `min_temperature`、`max_temperature` | 均热段允许温区，摄氏度；缺失传 null，由启用规则检查必需性 |
| `customer_grade`、`customer_name` | 客户等级，以及现有终端/战略/客户名称取值顺序 |
| `execution_standard` | 执行标准原文去前后空白 |
| `surface_grade` | 表面等级，可空；沿用 `SurfaceGrade` 后 `SurfaceQuality` 取值 |

`node_id`、`source_resource_id` 首期由服务端使用唯一 `source_order_id` 派生为同值，禁止静默合并同合同的多条记录。所有实数字段通过现有精确 JSON 编解码转换为 Decimal；拒绝 NaN、无穷、布尔充当数值和重复 JSON 键。未知字段、非对象订单、重复来源和错误期序均定位返回。请求体上限在新求解入口单独设为 8 MiB，并用实际 531 单请求验证；原规则接口 256 KiB 上限保持。

根对象、计划期对象和订单对象都采用严格字段集合：表中字段必须出现；其中 `grade_class`、`hot_roll_grade`、`width`、`thickness`、温区、客户等级、客户名称、执行标准和表面等级允许值为 null，文本空白统一转为 null。`periods` 与 `orders` 都不能为空，计划期标识和序号分别唯一，序号必须从 0 连续，订单来源期必须存在于计划期目录。服务端按显式序号排列计划期，但保持订单提交的相对顺序。

传输转换分别记录三层身份：`raw_request_fingerprint` 对已完成精确 JSON 解析的外部语义对象取指纹，包含 `expected_active_version_id`；`typed_request_fingerprint` 对字典补齐前、已映射且含求解策略的 `SchedulingTaskInput` 取指纹；`request_fingerprint` 继续表示规则、字典和原型绑定后交给核心的完整请求。三者用途不同，不能互相替代。

交货日期、前端指定开始日期 `CalcDay`、连镀用时不进入本轮请求，继续用于既有结果后处理，不新增交期评分。完整原订单属性保留于 C# 来源记录，真实结果按来源克隆，避免为传输引入第二份全量业务实体。

### 7.3 返回字段

| 字段 | 含义 |
|---|---|
| `contract_version`、`request_id` | 与本次请求对应 |
| `status`、`stop_reason`、`publishable` | 原样表达 V7 求解状态、停止原因；可发布需要双审计通过且有 release |
| `active_rule_set_version_id`、`rule_set_version` | 数据库版本 ID 与业务版本号分别返回，不能混用 |
| `rule_set_fingerprint`、`grade_dictionary_fingerprint` | 本次冻结的规则和软硬钢字典身份 |
| `raw_request_fingerprint`、`typed_request_fingerprint`、`request_fingerprint` | 外部语义请求、字典补齐前服务输入、绑定后核心请求三层身份 |
| `binding_fingerprint`、`result_fingerprint`、`bound_result_fingerprint` | 活动快照绑定、核心公开结果及外层绑定结果身份 |
| `preparation_report` | 命中、未命中及来源诊断 |
| `quality`、`metrics`、`issues`、`audit_summary`、`run_manifest` | 评分名称与值、规则/资源统计、诊断、双审计和运行计数 |
| `violations` | 从 `release.evaluation.violations` 原序输出的权威规则违规；`issues` 是流程诊断，不能代替它 |
| `rows` | 仅发布结果才有的完整有序节点数组；不截断，不用服务端 CSV 路径替代 |

每个结果行固定包含：

- 身份与顺序：`node_id`、`source_order_id`、`source_resource_id`、`source_period`、`assigned_period`、`chain_id`、`chain_sequence`、`node_sequence`；
- 工艺与分类：`weight`、`width`、`thickness`、`min_temperature`、`max_temperature`、`grade`、`grade_class`、`hot_roll_grade`、`soft_hard_class`、`material_role`；
- 来源谱系：`split_lineage`、`virtual_lineage`；
- 展示告警：`width_warning`、`thickness_warning`、`temperature_warning`、`chain_warning`。

数组顺序与 release 发布顺序一致；`chain_sequence` 在各自 `assigned_period` 内从 1 开始，`node_sequence` 在链内从 1 开始，HTTP 和 C# 都不二次按链号字符串重排。虚拟原型标识已经完整包含在 `virtual_lineage.prototype_id`，不再增加重复的顶层 `virtual_prototype_id`。

协议细节：`material_role` 仅为现有 `normal_real`、`actual_transition`、`virtual_sphc`；真实行保留来源字段，虚拟行的 `source_order_id`、`source_resource_id`、`source_period` 全部为 null。`split_lineage`、`virtual_lineage` 按 [现有核心模型](../../src/apsgo_scheduler/core/model.py) 的全部字段原名输出为对象或 null，枚举用字符串值，不丢掉授权指纹、分片序号/数量和虚拟用途。`quality` 为按正式优先级排列的 `{criterion_id, metric_key, value}` 数组，`metrics` 为指标名到值的对象；`issues` 复用诊断字段，`audit_summary` 分别记录核心审计、结果审计状态及 passed，不能只用一个 HTTP 成功标记代替。

每条 `violations` 保留 `rule_id/scope/subject_id/reason_code/message/disposition/severity` 全部字段。节点、边、链、方案级主体不相互替换；逐行展示只能引用或投影已有违规，不能用逐行提示重算违规数量。无 release 时 `violations` 为空，诊断方案如需附带评价须单独标识为不可发布诊断，不能混入发布违规。允许欠重的链级记录须完整保留，C# 不依据重量阈值重新生成它。

行级告警投影增加可空的 `width_warning/thickness_warning/temperature_warning`，从上述权威违规生成，不重新执行规则；链级允许欠重在该链首行提供明确标注链主体的 `chain_warning`。其他方案级/准备阶段提示保留在整单响应，不伪造成每个节点违规。投影测试以权威违规主体和已发布链映射为依据，不能靠截取 `subject_id` 猜订单。当前正式 release 只可能无违规或包含允许的欠重违规，因此宽度、厚度和温度告警仅在纯转换测试覆盖其定位契约；禁止违规不能包装进可发布结果。

`success` 和 `publishable_with_allowed_deviation` 且双审计通过、有 release 时允许回写；后者展示允许偏差。当前允许偏差只有链重低于下限，GQGA4 冻结基准验收仍要求零欠重。`complete_not_publishable`、`no_complete_plan`、`cancelled`、`failed` 不生成可回写 rows；诊断候选只作报告，不能包装为成功方案。预算用尽不等于失败，是否发布取决于现有审计结果。核心模型不允许空链或空计划；请求中的某个计划期没有产出时不生成占位行，也不占用该期链序。

### 7.4 失败响应

| HTTP 状态 | 条件 | 前端行为 |
|---:|---|---|
| 400 / 415 / 413 | JSON 或版本格式错误 / 非 JSON / 体积越界 | 提示，保持旧结果 |
| 422 | 订单类型、生成型虚拟输入、物理值、来源、期序或分类冲突 | 展示订单定位错误，不过滤后继续 |
| 409 | 规则活动版本已变化 | 重新加载规则后由用户重新提交 |
| 429 | 同进程已有求解 | 显示忙，不后台自动重试 |
| 503 | 未初始化、无可用字典、数据库暂不可用 | 提示准备状态，无替代默认字典 |
| 500 | 快照损坏、程序异常或结果映射不一致 | 给可追踪错误码，服务端保存异常，不回写 |
| 200 | 求解已返回明确状态，且不是 `failed/input_invalid` | 仍检查 publishable、审计和 rows，不能只看 HTTP 成功 |

单个牌号未命中写入准备报告，不返回 503；该场景依已有缺失分类策略求解。

协议错误使用固定 `error` 对象，包含 `code`、`message`、`request_id`、`expected_active_version_id`、`current_active_version_id` 和 `issues`；无法从请求安全取得的身份字段为 null。`issues` 保持既有 `code/phase/field_path/subject_id/message/severity` 定位结构。

启用规则要求的字段由已有标准化器判断；若公共求解入口返回 `status=failed` 且 `stop_reason=input_invalid`，HTTP 统一映射为 422，并保留其输入诊断、任务身份和空 rows。其他正常返回的求解状态按 200 表达，未捕获异常按 500 处理；不能在适配层复制一套规则必填字段校验。

## 8. 求解预算、配置与缓存

YAML 原四项继续保留，已新增 `monthly_solve` 映射，用严格字段校验加载以下生产策略：

| 项 | 初始值 |
|---|---:|
| `seed` | 590531 |
| `total_time_limit_seconds` | 180 |
| `finalization_reserve_seconds` | 10 |
| `candidate_check_limit` | 200000 |
| `whole_chain_pair_scan_slack_weight` | 40 |
| `maximum_virtual_bridge_nodes` | 2 |

构造排序与数值语义标识复用核心常量。配置在启动时完整冻结，不运行时读取测试文件。阶段 5 已同步扩展 YAML、加载器和初始化器测试；缺少 `monthly_solve` 的旧配置会在启动前明确报错，须按本节补齐。

加载器要求根级和 `monthly_solve` 六项字段集合精确匹配，拒绝布尔冒充整数/实数、非有限值及非法预算，再由既有 `SolverPolicy` 做统一语义校验。HTTP 层不复制求解策略默认值；直接构造应用但未提供已加载策略时，规则接口可用，求解接口明确返回 `503 monthly_solve_not_configured`。

180 秒是当前公共求解入口预算；新报告另记请求解析、字典绑定、结果编码和总服务耗时，不能把网络等待或准备耗时藏进“求解耗时”。集成验收同时观察外层总耗时是否仍符合既定 180 秒标准，超出应定位准备/传输开销，不自动放宽门槛。客户端建议超时 240 秒以接收最终审计和网络传输，客户端超时不是求解器的新预算。

任务内字典只查询一次，不逐订单 SQL。边缓存和候选索引沿用已有 `RuleEdgeDecisionCache`、构造图及其语义声明；分类补齐发生在缓存创建前。规则或字典改变后的新任务从新问题创建缓存，进行中的任务继续使用原快照。无需新增进程全局字典缓存或第二套连接矩阵。

## 9. C# 专属配置、客户端与回写

新增 `ApsgoV7Configuration`、`ApsgoV7SchedulingApiClient` 和 `ApsgoV7SchedRecordSolveService`。复用现有 V7 规则客户端 `ApsgoV7RuleApiClient`：它与求解客户端统一从配置类读取 `PipelineV7ApiBaseUrl`。建议新增 `PipelineV7MonthlySolvePath`（本文新路由）和 `PipelineV7MonthlySolveTimeoutSeconds`（240）。原 V3 配置键及服务保持原路径，其他产线不随 GQGA4 切换。

只在 `SchedApp/Forms/SchedPage/Test/GQGA4/CalcRollPosCommandGQGA4RequestSolution.cs` 中切换求解服务。保持“选中记录定位版本，再求解该 Version/ProductLine/配置规则步骤的全部记录”，不能变成只求解选中的几行。前端继续负责预设大辊号、记录来源保存、结果显示、日期计算与图表；不查询或同步 V7 字典，不计算软硬钢关系。

当前“计算最晚日期”是 `CalcRollPosCommandGQGA4DeliveryTime` 的独立下一步，从 `Step=求解` 读取，按 `RollPos/RollSeq` 排序，以 `CalcDay.Date` 开始累计连镀小时，再写入 `计算最晚日期`；本次不改该文件、步骤或公式。重量大于零的选择沿用现有 GQGA4 连镀欠交口径；其中 `VirtualBelongs` 非空或 `IsVirtual` 规范化后为 `是/1/TRUE/T/虚拟` 的输入明确报错，整批不提交，不静默删除。`ApsgoV7RuleApiClient` 当前两处仍误提 `BACKEND_ALGORITHM_URL` 的提示文字随 V7 配置抽取更正，用户地址值保持。

回写前先构造全部新记录并验证：真实来源都可查、节点 ID 唯一、真实父订单重量守恒、期和链序存在、所有 rows 均已消费。未知来源不能像旧代码一样静默跳过。真实记录克隆源属性，拆片保持父合同号并用节点标识填分片号；请求不发送 `SoftOrHard`，响应只接受 `软钢/硬钢/null`，同父单拆片分类须一致。C# 仅校验后写入 `SoftOrHard`，空分类存空，不按牌号重新判定。生成型虚拟材由返回原型/规格创建，归属 `GQGA4`，按已有月计划方式填充需求重量。

`RollPos` 使用前端原大辊号和返回该期链序，`RollSeq` 使用链内顺序；保持既有保留段含义。借入单用 `assigned_period` 决定产出大辊位置，不能按 `source_period` 放回原期；`source_period` 保留追溯。

删除旧本步骤结果与插入新结果必须在同一前端数据库事务中执行，网络请求期间不持有写事务。复用 SqlSugar 现有事务方式：`db.Ado.BeginTran()` 后删除旧 `Step=求解` 行、插入全部新行，核对插入数量等于已验证记录数后 `CommitTran()`，异常 `RollbackTran()`。事务内不联网、不重新读取规则。取消、HTTP 错误、不可发布结果、来源冲突、重量不守恒或插入失败均保留旧结果。成功后沿用现有后处理；“排程结果”按钮仍由用户手动查询，不擅自调整页面操作流程。

GQGA4 命令显式设置 `RunInBackground => true`，`ChangesSchedRecords` 在开始设为 false，仅在完整写回事务成功后改为 true，参考已有配置规则命令做法；空选择、校验提示后返回和取消不能误触发步骤推进。现有页面没有用户主动取消通道，本期不增加取消按钮或重构命令框架；客户端方法保留 `CancellationToken` 和超时，后台命令可等待异步方法完成，不能阻塞 UI 线程。HTTP 断开只发出取消请求，后端仍等待核心真正退出。写入的 `Warnings` 保留现有 JSON 外形并承载后端逐行诊断；规则/字典版本及任务标识保留在结果元数据中，不能靠下次 GET 的当前规则解释上次排程。

当前 `RollingStrictAnalysisRuleChecker.ResolveProcessCode()` 只选择二连退和六镀锌，并未接管 GQGA4；本期不扩展或修改它。GQGA4 图表/汇总继续读取发布记录，违规说明使用 V7 结果诊断；新界面读取不得临时采用最新规则重判历史结果。其他产线沿用原分析器。

结果身份使用现有 `SchedRecord.Warnings` JSON 容器：根级保留 `Warnings` 告警对象，新增同级 `V7Metadata`。后者仅保存 `contract_version/request_id/status/active_rule_set_version_id/rule_set_version/grade_dictionary_fingerprint/binding_fingerprint/bound_result_fingerprint`，每行一致，不重复存整份响应。行级四类告警分别映射为 `WidthWarning/ThicknessWarning/TemperatureWarning/ChainWarning`；无告警时 `Warnings={}`，元数据不算告警。仅对 GQGA4 汇总复用现有 `HasWarningMessages()`，替换字符串非空即报警的计数；不改变其他产线分支，也不增加第二个 JSON 解析器。现有图表继续读取三个物理告警键，链级提示供汇总显示。

该方案不计划修改 C# 业务库表结构；阶段 0 必须核验实际 `Warnings` 列容量，阶段 7 用完整序列化文本验证可无损保存。若实际字段不足，停止该写回步骤并提出明确扩容方案，不静默截断元数据，也不宣称已可在现有表无损保存。

## 10. 验收口径

| 层次 | 必须证明 |
|---|---|
| 迁移 | 230 条逐字段一致；源库不变；旧版本不改；新版本原子激活；失败回滚、重复执行安全 |
| 拼接 | 按 grade 精确匹配；529 命中、2 缺失；分类与冻结参考逐单一致；无别名猜测 |
| 请求 | 字段、顺序、类型、材料角色、身份和来源重量正确；生产路径不读取测试输入 |
| 绑定 | 活动版本并发检查与全部快照同一事务；求解中规则保存不影响已绑定任务；搜索阶段零数据库访问 |
| 求解 | 原始订单转换路径与已补齐参考路径在同订单顺序、同规则/策略下得到相同规范化输入；再执行真实求解与双审计 |
| 回写 | 完整节点覆盖；拆片、借用、虚拟和空分类可追溯；失败无部分覆盖 |
| 端到端 | 真实 HTTP、临时 SQLite、C# JSON 对照；Windows 构建及页面实测分别记录 |

保持七级评分顺序：禁止违规数、禁止严重度、欠重链数、欠重缺口、相邻链首尾宽差、虚拟重量、非空链数。跨大辊期宽差继续计算。冻结 GQGA4 门槛仍为零禁止、零欠重、覆盖守恒、虚拟比例不超过 5%、无链数上限；不把普通月计划允许欠重发布与基准零欠重验收混为一谈。

本专项不重做算法性能研究。真实集成先完成逐字段输入一致性、一次完整 GQGA4 运行与外层耗时测量；只有实际开销超标、算法路径变化或结论需要时，再决定针对性性能复测，既有 20 对结果保持历史证据。

## 11. 决策与后续

本设计采用：独立字典表关联规则版本、显式 SQLite 到 SQLite 的一次性导入、一次性任务绑定、精确匹配、保留单牌号缺失兜底、V7 专属求解路由和 C# 服务、复用现有核心规则与求解器。生产包不保存字典 JSON；运行时只读 V7 数据库。新路由、YAML 配置键和 8 MiB 请求上限已在阶段 5 实现；240 秒客户端超时及 C# 传输客户端已在阶段 6 实现，C# 求解服务、页面回写和正式部署仍待后续阶段完成。

`HC220YD+Z-GL` 暂沿用当前缺失语义，不阻塞实施；其分类补录需要业务依据。首期对正重量已生成虚拟材和重复合同来源明确拒绝；若实际月计划前置步骤必须包含它们，则先记录真实案例并设计来源还原规则，不能静默删除或扩展核心输入材料类型。

后续按配套计划实施；阶段 5 的临时库 HTTP 回环已经通过，但不表示正式数据库迁移、C#/Windows 联调或正式 GQGA4 完整验收已完成。
