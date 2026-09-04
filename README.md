# APSGo Scheduler

基于规则驱动的最小路径覆盖与确定性局部搜索：先构造覆盖全部订单的路径，
再按固定顺序执行首次改善搜索。生产代码仅依赖 Python 标准库。

唯一计划公开入口为 `apsgo_scheduler.app.service.solve_request()`；目前只完成
工程边界，入口与排程功能尚未实现。实施顺序见
[实施计划](docs/implementation/apsgo_v6_solverpy_rule_driven_path_cover_local_search_implementation_plan.md)。

当前不实现 ALNS、滚动发布、交期评分、旧 V6 兼容入口、数据库、HTTP 或前端。

Python 3.10 及以上，开发环境安装与架构检查：

```sh
python -m pip install -e '.[dev]'
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider tests/architecture -q
python tools/check_workspace_residuals.py --verify
```

共享工作区存在其他分支残留时不要在其中安装或构建；请在干净检出中安装。
共享工作区可直接使用上述 `src` 布局测试命令。
