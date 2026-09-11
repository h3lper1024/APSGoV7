# 临期订单定向搜索执行证据

## 阶段 0：基线冻结

2026-09-11，实施前 `8942d18`，macOS，Conda `aps_3.10.18`。用户授权持续实施，目标优先级、禁止规则、期锁和预算不变。

复用原交期专项已有输出，不重复求解。531 原订单 / 29333.91 吨；本月中上旬 31 单 / 1412.22 吨。直接基线为当前九级：11 单晚交 / 288.89 吨、欠重 1 条 / 41.01 吨，质量 `(0,0,1,41.01,288.89,1297489.840267975863706520110,12151,540,23)`；旧七级保留为零欠重质量参考。两次九级输出一致，核心审计均通过。

| 项目根目录相对路径 | SHA-256 |
|---|---|
| `diagnostics/delivery_objective/timing_source.json` | `8444fa93449790cdddac864c91644d6c246ace7947f735b3a51f25e999dbc124` |
| `diagnostics/delivery_objective/delivery_01/prepared_request.json` | `887943f2a5ec281c29a963eeb0a0c1b7caebac54fcd2bb124095cffc070800c4` |
| `diagnostics/delivery_objective/old_01/measurement.json` | `c10fac7ea3e775d13dc86e47b33cb031dcb15c0bb1a0325408e12360062ee1fd` |
| `diagnostics/delivery_objective/delivery_01/measurement.json` | `a486c1d970d5c259b0026d13b3f87f62a1073d8566dc52a4f585ab9d5b12e427` |
| `diagnostics/delivery_objective/delivery_02/measurement.json` | `bfbd6b8fb7947427a6c4e01f954ef7135eafa5b93f151f5c426dc56dd41c6233` |
| `data/apsgo_v7_rules.sqlite3` | `72a60e32fea0d2d55f883a8b743175aa5e8fd94e779388db6ae8c8c26ec2bd6a` |
| `config/apsgo_v7_service.yaml` | `dc8f113ef85689fdc5ff2fecb0579f68c6892a8d8316c764568946bf341dd4c8` |

参数固定为种子 590531、200000 次候选、310 秒总时间 / 10 秒收尾、六月一日 0 点 +08:00、截止当天 24 点、虚拟速度 100 米/分钟。执行命令复用[计划第 6 节](../../apsgo_v7_urgent_order_search_implementation_plan.md)，新结果使用独立目录，不覆盖任何旧结果。

实际检查：`git status --short --branch`、`git log -2 --oneline`、`uname -s`、项目 Python `-B -` 只读 JSON/哈希核对，均退出 0。没有业务测试、求解、数据库写入或服务操作。后续阶段追加记录，不能把本阶段称为算法收益。

## 阶段 1～2：排序与准入检查点

阶段 0 `19173fe`，阶段 1 `238e7c3`；后者仅排序辅助和用例，未改变前置局部搜索的链序。阶段 2 改动准入、单链重量预筛及完整候选接受五处，共用判断只允许规则明示的链重下限偏差，按质量声明名称分别保护欠重数和两位缺口；最终审计未改。

实际命令（项目环境、无字节码和 pytest 缓存）：

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider \
  tests/core/test_urgent_order_search.py tests/core/test_delivery_search_audit.py \
  tests/core/search tests/core/audit tests/architecture -q --tb=short
```

首次 1019 通过 / 4 失败，31.50 秒：新增用例错误修改截止小时却未同步日期；旧合成夹具默认没有允许欠重的代码清单。修正夹具而非放松生产校验，最终同范围 **1023 通过，31.41 秒，退出 0**。原七级测试继续通过；没有真实排程或收益结论。独立用例同时验证不增加数量和缺口、其他偏差拒绝、已有欠重可执行精修。
